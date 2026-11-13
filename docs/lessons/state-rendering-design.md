# State 元信息与渲染裁剪设计

> 状态：设计稿，尚未实现
> 适用范围：`SessionState` / `StateDelta` / `StateExtractor` / `StateRenderer` / `ContextBuilder`
> 目标：让状态渲染从“按 section 固定截断”升级为“按可比较元信息做可解释裁剪”

---

## 1. 当前问题

当前 [context/renderer.py](../../context/renderer.py) 的规则是：

```text
Preferences
Goals
Constraints
Progress
Facts
```

逐个 section 尝试加入；某个 section 放不下时：

```python
truncated = True
break
```

这会带来两个问题：

1. **整段丢弃**：Constraints 里可能同时有“预算 800”和“对亚硫酸盐过敏”，
   只要整段超预算，两条都丢；
2. **安全信息排在后面**：Constraints 在 Goals 之后，Goals 可能先吃掉预算，
   导致安全约束根本没有渲染机会。

结论：

> 默认 section 顺序可以作为“普通信息”的顺序，
> 但不能作为“安全信息必须保留”的保证。

---

## 2. 设计原则

### 2.1 模型层提供“可比较的元信息”

不要在渲染层写死：

```text
preferences > goals > constraints
```

而是让条目自己带少量可比较信息。

### 2.2 渲染层做“可解释的裁剪”

被保留/被裁掉的每一条，都应该能回答：

```text
为什么它被保留？
为什么它被裁掉？
```

### 2.3 `critical` 是硬层级，不是高权重

“过敏”“禁酒”“法律限制”不能参与普通打分后被别的分数压过去。

### 2.4 选择可以动态，展示顺序要稳定

- 选择：按优先级动态选；
- 渲染：仍按固定 section 顺序输出，避免 prompt 结构每轮跳动。

---

## 3. 目标模型

### 3.1 第一阶段：只给 constraints 加元信息

```python
class ConstraintKind(str, Enum):
    BUDGET = "budget"
    ALLERGY = "allergy"
    DIET = "diet"
    RELIGION = "religion"
    LEGAL = "legal"
    OTHER = "other"


class ConstraintSeverity(str, Enum):
    NORMAL = "normal"
    IMPORTANT = "important"
    CRITICAL = "critical"


class Constraint(BaseModel):
    text: str
    kind: ConstraintKind = ConstraintKind.OTHER
    severity: ConstraintSeverity = ConstraintSeverity.NORMAL
```

`SessionState.constraints` 从：

```python
constraints: list[str]
```

升级为：

```python
constraints: list[Constraint]
```

`StateDelta` 对应改为：

```python
constraints_add: list[Constraint]
constraints_remove: list[str]  # 仍按原文删除
```

第一版可以先不做 key；等出现“按 key 更新约束”的真实需求，再升级为
`dict[str, Constraint]`。

### 3.2 第二阶段：给 facts 加来源和置信度

```python
class FactSource(str, Enum):
    USER = "user"
    TOOL = "tool"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class FactConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Fact(BaseModel):
    value: Any
    source: FactSource = FactSource.UNKNOWN
    confidence: FactConfidence = FactConfidence.MEDIUM
```

注意：

- `source` 应该由**管线**根据消息角色决定，不要完全让模型猜；
- `confidence` 先用粗粒度枚举，不要用看起来精确的浮点数；
- `preferences` 暂时保持 `dict[str, Any]`，不急着包装。

### 3.3 不要放进 `SessionState` 的元信息

以下字段不建议进 `SessionState`：

```text
last_used
reference_count
```

它们是运行时统计，不是领域事实。每次渲染都更新会导致：

```text
读上下文 → 写状态 → version +1
```

把只读的 `build` 变成写操作。

如果需要，后续单独建：

```text
StateUsageStore
  session_id
  item_key
  last_used_at
  reference_count
```

`updated_at` 可以留在状态里，但只在状态真正变化时更新。

---

## 4. 渲染算法

### 4.1 目标结果

```python
@dataclass(frozen=True)
class RenderedState:
    text: str
    tokens: int
    truncated: bool
    critical_tokens: int
    dropped_items: tuple[str, ...]
    critical_truncated: bool
```

### 4.2 条目化

把状态拆成可排序的 `RenderItem`：

```python
@dataclass(frozen=True)
class RenderItem:
    section: str
    text: str
    tokens: int
    safety_rank: int
    source_rank: int
    confidence_rank: int
    section_rank: int
    original_index: int
```

### 4.3 优先级使用字典序，不用乘法

```python
priority = (
    safety_rank,       # critical=3, important=2, normal=1
    source_rank,       # user=3, tool=2, inferred=1, unknown=0
    confidence_rank,   # high=3, medium=2, low=1
    section_rank,      # 默认顺序只做 tie-break
    original_index,
)
```

不要再写：

```text
score = safety_weight * source_weight * confidence * time_decay
```

乘法公式的问题：

- 各项含义混在一起，难以解释；
- `source` 和 `confidence` 重复表达“可信度”；
- `critical=1000` 是魔法数；
- 时间衰减会让测试依赖真实日期。

### 4.4 选择顺序

```text
1. 先选出所有 critical constraints
2. 计算 critical_tokens
3. 如果 critical_tokens > state_max_tokens：
       raise StateRenderError
4. 保留全部 critical
5. 普通条目按 priority 从高到低选择
6. 直到预算用完
7. 选不上的进入 dropped_items
```

### 4.5 渲染顺序

选择完成后，按固定 section 顺序输出：

```text
critical constraints（单独块，可选）
Preferences
Goals
Constraints
Progress
Facts
```

只渲染被选中的条目。

如果某个 section 一条都没选中：

```text
不输出空标题
```

---

## 5. critical 超预算怎么办

“critical 永远保留，哪怕超预算”在硬 token 上限下不成立。

必须显式定义：

```python
class StateRenderError(RuntimeError):
    """状态无法在预算内安全渲染。"""
```

策略：

```text
critical 能放下：
    全部保留

critical 放不下：
    raise StateRenderError("critical 状态超过预算")
```

不要让 `StateRenderer` 静默超预算，也不要静默截断 `critical`。

`ContextBuilder` 捕获到 `StateRenderError` 后，可以选择：

- 直接失败；
- 或降级为“状态不完整”的明确告警事件。

但不要假装状态完整。

---

## 6. 分阶段落地

### Phase 0：先修当前截断 bug

不改模型：

- `StateRenderer` 不再整段 `break`；
- constraints 获得最低保留预算；
- constraints 至少排在 goals 前面；
- 增加 `dropped_sections` / `truncated` 的可观测性。

目标：先避免“goals 把 constraints 全挤掉”。

### Phase 1：Constraint 元信息

- 新增 `Constraint` / `ConstraintKind` / `ConstraintSeverity`；
- `SessionState.constraints` 改为 `list[Constraint]`；
- `StateDelta.constraints_add` 改为 `list[Constraint]`；
- `StateExtractor` 工具 schema 输出对象；
- `StateRenderer` 先保留全部 `critical`；
- 补 critical 保留/超预算测试。

这是最值得先做的一步。

### Phase 2：渲染选择器

- 引入 `RenderItem`、`RenderPolicy`、`StateRenderError`；
- 用字典序优先级替代 section 整段截断；
- 渲染顺序固定，选择顺序动态；
- 补“部分条目被裁”“section 不空标题”等测试。

### Phase 3：Fact 来源与置信度

- 新增 `Fact` / `FactSource` / `FactConfidence`；
- `StateExtractor` 输出 facts 对象；
- `source` 优先由管线根据消息角色设置；
- `StateRenderer` 在普通条目排序中使用 source/confidence。

### Phase 4：可选的相关性元信息

只有在确实需要时再加：

- `updated_at`
- 时间衰减
- `last_used`
- `reference_count`
- 显式权重

这些应该优先放在独立 usage store，而不是 `SessionState`。

---

## 7. 测试计划

### Phase 0

- 预算很紧时，constraints 不会因为 goals 太大而完全消失；
- 一个 section 放不下时，不再导致后续 section 全部消失；
- `truncated` 能反映实际裁剪。

### Phase 1

- `critical` 过敏约束在预算紧张时仍保留；
- `critical` 条目本身超预算时抛 `StateRenderError`；
- `normal` 约束可以按普通规则裁剪；
- 旧的 `list[str]` 数据如果需要兼容，转换后默认
  `severity=normal`，不能假装 critical。

### Phase 2

- 同一 section 内可以保留部分条目；
- 所有条目都被裁时，不输出空 section；
- critical 块始终先于普通 section；
- 排序稳定，不依赖字典遍历顺序；
- 相同优先级时按原始顺序稳定输出。

### Phase 3

- `user` 来源优先于 `inferred`；
- `high` 置信度优先于 `low`；
- 测试使用固定元信息，不依赖模型输出。

### Phase 4

- 时间衰减测试必须注入固定时钟；
- reference count 更新不修改 `SessionState.version`。

---

## 8. 明确不做

- 不在第一版给所有条目加完整元信息；
- 不使用自由浮点权重；
- 不让 `critical` 参与普通打分；
- 不把 `last_used` / `reference_count` 放进 `SessionState`；
- 不在渲染时写状态；
- 不为了“未来可能有用”提前实现完整打分系统。

---

## 9. 验收标准

Phase 1 完成后应满足：

```text
用户：我对亚硫酸盐过敏，预算不超过 800。
预算很紧时：
  → “对亚硫酸盐过敏”必须出现
  → “预算不超过 800”可以按普通优先级处理
  → 如果 critical 自己放不下，明确报错，不静默丢弃
```

Phase 2 完成后应满足：

```text
一个 section 内可以保留部分条目；
被裁条目可解释；
空 section 不输出；
prompt 结构稳定。
```

最终目标：

> 模型层提供可比较的元信息，渲染层做可解释的裁剪；
> 安全信息是硬底线，不是高权重。
