# Goal ID 设计

> 目标：把「按目标文本精确匹配」改成「按稳定 ID 引用目标」。
> 适用范围：`SessionState` / `StateDelta` / `StateService` / `StateExtractor` / 第 17 课 `StateRenderer`。

---

## 1. 现在的问题

当前完成/放弃目标靠文本：

```python
StateDelta(goals_complete=["买两瓶干型酒"])
```

`apply_delta()` 在 `SessionState.goals` 里找 `goal.text == "买两瓶干型酒"`。

只要模型改写成：

```text
购买两瓶干型葡萄酒
```

就找不到目标，抛 `StateApplyError`。

自然语言本来就有同义改写、标点差异、空格差异。用文本当主键，边界很脆。

---

## 2. 目标与非目标

### 目标

- 每个 `Goal` 有稳定、唯一的 ID；
- 完成/放弃目标用 ID，不用文本；
- 新增目标仍然用文本，ID 由服务端分配；
- 旧状态没有 ID 时能平滑读取，不需要删库；
- ID 能安全放进 prompt，模型只需要复制短字符串；
- 不在模型层用文本 hash 生成 ID。

### 非目标

- 本设计不实现“修改目标文本”（当前 Delta 没有 update goal text 操作）；
- 不实现目标排序/优先级；
- 不实现跨 session 的全局 Goal 表；
- 不改变状态版本号语义。

---

## 3. 关键设计决策

### 3.1 统一使用 UUID，不使用文本 hash

```python
def new_goal_id() -> str:
    return "g_" + uuid4().hex[:12]
```

格式：

```text
g_ + 12 位小写十六进制
例如：g_4f2a9c1b7d3e
```

为什么不用：

```python
id = "g_" + sha1(text)[:12]
```

- ID 会依赖文本；以后支持改目标文本时，ID 会变；
- 文本相同会得到相同 ID；
- 两个不同文本理论上可能 hash 碰撞；
- 把“身份”绑定到“内容”，长期不稳定。

UUID 的 ID 与文本解耦，目标改名不会改变身份。

### 3.2 ID 不在 `Goal.model_validate` 里生成

模型层只负责数据形状，不负责分配身份。

不要在 `Goal` 的 `mode="before"` 校验器里自动补 ID：

```python
# 不推荐
if not data.get("id"):
    data["id"] = sha1(text)
```

原因：

- 读旧 JSON 时会自动生成，生命周期不可控；
- 生成规则和业务层耦合；
- 想换生成策略/做迁移时，模型层会变成隐藏逻辑；
- 测试很难区分“模型补的”还是“服务补的”。

ID 分配放在 `StateService` 的应用边界。

### 3.3 `Goal.id` 允许暂时为空

为了兼容旧 JSON：

```python
class Goal(BaseModel):
    id: str = ""
    text: str
    status: GoalStatus = GoalStatus.ACTIVE
```

- 旧数据没有 `id`，可以正常加载；
- `id` 非空时校验格式；
- `id` 为空只表示“尚未迁移”；
- 只有经过 `StateService` 后，才保证所有 Goal 都有 ID。

### 3.4 在 Service 边界补 ID，而不是只在 `apply_delta` 补

如果只在 `apply_delta` 里补：

```python
if not goal.id:
    goal.id = uuid4().hex[:12]
```

会留下一个洞：

```text
StateService.get()
  -> 返回的旧 Goal 仍然 id=""
  -> StateExtractor 拿不到 ID
  -> StateRenderer 渲染不出 ID
  -> 模型无法生成 goals_complete=["g_..."]
```

所以补 ID 必须发生在**状态被读出来之后、暴露给上层之前**：

```text
StateService.get
StateService.apply
```

两个入口都调用 `ensure_goal_ids()`。

### 3.5 补 ID 后要持久化

只在内存里补 UUID 不够：

```text
第一次 get -> 生成 UUID-A
第二次 get -> 又生成 UUID-B
```

每次读都变，等于没有稳定 ID。

所以：

- `ensure_goal_ids()` 发现旧 Goal 缺 ID；
- 立即用当前 `version` 保存一次；
- 下一次读到的就是持久化后的 UUID。

这是“读时迁移”，版本会 +1；只发生一次。

---

## 4. 数据模型

### 4.1 `Goal`

```python
_GOAL_ID_RE = re.compile(r"^g_[0-9a-f]{12}$")


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = ""
    text: str
    status: GoalStatus = GoalStatus.ACTIVE

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if value and not _GOAL_ID_RE.fullmatch(value):
            raise ValueError("goal.id 格式必须是 g_ + 12 位十六进制")
        return value
```

注意：

- `id=""` 是合法的“未迁移”状态；
- 非空 ID 必须符合格式；
- 不要在 `Goal` 里自动生成 ID。

### 4.2 `SessionState`

在现有校验之外，增加：

```python
goal_ids = [goal.id for goal in self.goals if goal.id]
if len(goal_ids) != len(set(goal_ids)):
    raise ValueError("goals 中存在重复目标 ID")
```

只检查非空 ID，允许旧数据暂时有空 ID。

### 4.3 `new_goal_id`

```python
def new_goal_id() -> str:
    return "g_" + uuid.uuid4().hex[:12]
```

12 位十六进制是 48 bit；session 内目标数量很少，碰撞概率极低。`ensure_goal_ids` 还会做一次去重检查。

### 4.4 `ensure_goal_ids`

```python
def ensure_goal_ids(state: SessionState) -> tuple[SessionState, bool]:
    """给缺失 ID 的 Goal 补 UUID；不修改传入的 state。"""
    goals = [goal.model_copy(deep=True) for goal in state.goals]
    seen = {goal.id for goal in goals if goal.id}
    changed = False

    for index, goal in enumerate(goals):
        if goal.id:
            continue

        new_id = new_goal_id()
        while new_id in seen:
            new_id = new_goal_id()

        seen.add(new_id)
        goals[index] = goal.model_copy(update={"id": new_id})
        changed = True

    if not changed:
        return state, False

    return state.model_copy(update={"goals": goals}), True
```

特点：

- 纯函数，不修改输入；
- 幂等：已经有 ID 的 Goal 不会再变；
- 冲突检查：新 UUID 不会和已有 ID 重复。

---

## 5. Service 边界

### 5.1 `StateService.get`

```python
async def get(self, session_id: str) -> StateSnapshot:
    async with self.registry.acquire(session_id):
        snapshot = self.registry.load_snapshot(session_id)

        if snapshot is None:
            return StateSnapshot(state=SessionState(), version=0)

        state, changed = ensure_goal_ids(snapshot.state)

        if not changed:
            return snapshot

        # 旧数据迁移：把补好的 ID 写回，版本 +1
        version = self.registry.save_snapshot(
            session_id,
            state,
            expected_version=snapshot.version,
        )
        return StateSnapshot(state=state, version=version)
```

效果：

```text
旧状态 id="" -> get() 补 UUID -> 保存 -> 返回带 ID 的快照
下一次 get() -> 已经都有 ID -> 不写库
```

### 5.2 `StateService.apply`

```python
async with self.registry.acquire(session_id):
    snapshot = self.registry.load_snapshot(session_id)
    current = snapshot or StateSnapshot(SessionState(), 0)

    # 先补 ID，保证 delta 里的 ID 有东西可匹配
    state, migrated = ensure_goal_ids(current.state)

    if expected_version is not None and expected_version != current.version:
        raise StateConflictError(...)

    if delta.is_empty():
        if not migrated:
            return current

        version = self.registry.save_snapshot(
            session_id,
            state,
            expected_version=current.version,
        )
        return StateSnapshot(state=state, version=version)

    updated = apply_delta(state, delta)

    if updated == state:
        if not migrated:
            return current

        version = self.registry.save_snapshot(
            session_id,
            state,
            expected_version=current.version,
        )
        return StateSnapshot(state=state, version=version)

    version = self.registry.save_snapshot(
        session_id,
        updated,
        expected_version=current.version,
    )
    return StateSnapshot(state=updated, version=version)
```

要点：

- 旧数据迁移和 delta 应用可以在**同一次保存**里完成；
- `expected_version` 检查放在写库之前；
- 迁移本身也会消耗一个版本；
- 迁移失败/冲突仍然抛 `StateConflictError`，由调用方重读。

### 5.3 为什么不在 `StateStore` 里补 ID

`StateStore` 是持久化层：

- 它不应该知道“Goal 的业务身份”；
- 它只负责 JSON 读写和版本；
- ID 分配属于 `StateService` 的领域逻辑。

---

## 6. Delta 协议变化

### 6.1 字段语义

| 字段 | 现在 | 改后 |
|---|---|---|
| `goals_add` | 目标文本 | 不变：目标文本 |
| `goals_complete` | 目标文本 | **目标 ID** |
| `goals_abandon` | 目标文本 | **目标 ID** |

新增目标仍用文本：

```python
StateDelta(goals_add=["买两瓶干型酒"])
```

完成目标用 ID：

```python
StateDelta(goals_complete=["g_8f14e45fceea"])
```

### 6.2 校验规则

- `goals_add`：非空、去重、无空白；
- `goals_complete` / `goals_abandon`：非空、去重、符合 `g_` 格式；
- 同一个 ID 不能同时 complete 和 abandon；
- 不再检查 `goals_add` 与 complete/abandon 的“文本重叠”，因为 add 是文本，complete 是 ID，不在一个命名空间。

### 6.3 显式失败

旧格式：

```python
StateDelta(goals_complete=["买两瓶干型酒"])
```

应该直接校验失败：

```text
goals_complete 必须是目标 ID（g_ + 12 位十六进制）
```

不要做“看起来像文本就按文本匹配”的兼容：

- 会让协议出现两个命名空间；
- 模型会继续输出文本；
- 长期无法稳定。

旧 Delta 没有持久化，直接改协议成本最低。

---

## 7. 应用逻辑变化

当前：

```python
for text in delta.goals_complete:
    index = _find_goal(goals, text)  # 按 goal.text 找
```

改后：

```python
for goal_id in delta.goals_complete:
    index = _find_goal_by_id(goals, goal_id)
```

```python
def _find_goal_by_id(goals: list[Goal], goal_id: str) -> int:
    for index, goal in enumerate(goals):
        if goal.id == goal_id:
            return index
    raise StateApplyError(f"目标不存在：{goal_id}")
```

新增目标：

```python
for text in delta.goals_add:
    if not any(goal.text == text for goal in goals):
        goals.append(Goal(id=new_goal_id(), text=text))
```

注意：

- 新增目标仍然按文本去重，避免重复目标；
- 完成/放弃必须按 ID，不再回退文本；
- 找不到 ID 就报错，不静默 no-op。

---

## 8. Prompt / Tool Schema 变化

`StateExtractor` 的当前状态 JSON 会自动带出 `id`：

```json
{
  "goals": [
    {"id": "g_8f14e45fceea", "text": "买酒", "status": "active"}
  ]
}
```

Prompt 规则：

```text
goals_add：填写目标文本，不要填写 ID。
goals_complete / goals_abandon：必须复制当前状态中的 goal.id，
不要复制 goal.text。
```

Tool Schema 描述：

```json
"goals_complete": {
  "type": "array",
  "items": {"type": "string"},
  "description": "已完成目标的 ID，必须复制当前状态中的 goal.id"
}
```

不要只说“目标”，否则模型仍然会输出文本。

---

## 9. 兼容性

### 不需要改数据库 schema

`Goal.id` 只是 `state_json` 里的字段：

```json
{
  "goals": [
    {"id": "g_...", "text": "买酒", "status": "active"}
  ]
}
```

旧 JSON 没有 `id`：

1. `Goal.model_validate` 允许 `id=""`；
2. `StateService.get/apply` 调用 `ensure_goal_ids()`；
3. 补 UUID 后用当前 version 保存；
4. 下一次读就是稳定 ID；
5. 不需要 `ALTER TABLE`，不需要迁移脚本。

### 并发下的迁移

两个进程同时第一次读旧状态：

```text
P1: load version=1, 补 UUID-A, save expected=1 -> version=2
P2: load version=1, 补 UUID-B, save expected=1 -> conflict
```

P2 会收到 `StateConflictError`，重读后拿到 P1 的 UUID-A。

不会出现“两个 UUID 都写进去”的情况，因为版本检查会挡住。

### 直接读 `StateStore`

`StateStore.load()` 仍然可能返回 `id=""` 的旧状态。  
这是内部接口；上层应该使用：

```python
StateService.get(...)
```

`StateRenderer`、`StateExtractor` 都应该从 `StateService` 拿快照，而不是直接读 store。

---

## 10. 测试计划

### 10.1 模型层

- `Goal(text="买酒")` 可以加载，`id=""`；
- 非空 ID 不符合格式被拒绝；
- `SessionState` 拒绝重复的非空 ID；
- 允许旧状态中多个空 ID（迁移前）；
- `ensure_goal_ids` 不修改输入；
- `ensure_goal_ids` 幂等；
- 补出来的 ID 唯一。

### 10.2 Service 层

- 旧状态第一次 `get()` 补 UUID，版本 +1；
- 第二次 `get()` 不再涨版本；
- 旧状态 `apply()` 时补 ID，并应用 delta，只保存一次；
- 补 ID 后 `StateExtractor` / `StateRenderer` 能看到 ID；
- 并发迁移时一个成功、一个冲突。

### 10.3 Delta 层

- `goals_complete=["g_..."]` 合法；
- `goals_complete=["买酒"]` 被拒绝；
- 同一个 ID 同时 complete/abandon 被拒绝；
- `goals_add` 仍然是文本，重复文本去重。

### 10.4 应用层

- 新增目标后能拿到 ID；
- 用 ID 完成目标成功；
- 用旧文本完成目标失败；
- 用不存在的 ID 完成目标抛 `StateApplyError`；
- 完成/放弃的状态转换规则保持不变；
- 版本递增和冲突检测保持不变。

### 10.5 回归

- 第 12 课状态序列化往返；
- 第 13 课 delta 应用/拒绝；
- 第 16 课版本冲突/并发 apply；
- 旧 SQLite 数据读取。

---

## 11. 代码改动清单

| 文件 | 改动 |
|---|---|
| `state/models.py` | `Goal.id`、ID 格式校验、SessionState 重复 ID 校验 |
| `state/service.py` | `new_goal_id()`、`ensure_goal_ids()`、get/apply 迁移 |
| `state/extractor.py` | Tool Schema 描述、Prompt 规则、Delta 校验 |
| `state/store.py` | 不需要改 schema；JSON 自然带 id |
| `tests/unit/test_lesson12_session_state.py` | 增加 ID 格式/唯一性测试 |
| `tests/unit/test_lesson13_state_extractor.py` | complete/abandon 改用 ID |
| `tests/unit/test_lesson16_state_service.py` | 完成/放弃改用 ID，迁移测试 |
| `docs/lessons/lesson12.md` | `Goal` 增加 id 说明 |
| `docs/lessons/lesson13.md` | Delta 协议变化 |
| `docs/lessons/lesson16.md` | 完成/放弃用 ID |
| 第 17 课 `StateRenderer` | 渲染 `id + text + status` |

---

## 12. 推荐落地顺序

1. 改 `Goal`：允许空 ID，非空校验格式
2. 改 `SessionState`：重复非空 ID 校验
3. 实现 `new_goal_id()` / `ensure_goal_ids()`
4. 改 `StateService.get/apply`：先补 ID，再应用 delta
5. 改 `StateDelta`：complete/abandon 只收 ID
6. 改 `apply_delta`：按 ID 查找
7. 改 `StateExtractor` prompt / Tool Schema
8. 改第 12/13/16 课测试和文档
9. 第 17 课渲染时把 ID 显示给模型

不要先改 prompt 再改校验：否则模型会输出 ID，但 `StateDelta` 还按文本校验，排查会很乱。

---

## 13. 风险与开放问题

1. **读时迁移的副作用**：`get()` 可能写库、涨版本；这是为了只迁移一次。不能只补内存不保存，否则每次读都生成新 UUID。
2. **直接读 `StateStore` 仍可能空 ID**：`StateService` 是公开边界；`StateRenderer`/`StateExtractor` 必须走 service。
3. **UUID 碰撞**：12 位十六进制是 48 bit；`ensure_goal_ids` 会检查重复并重试。
4. **模型不复制 ID**：靠 prompt + `StateDelta` 格式校验；失败时 `StateExtractor` 目前 fail fast，后续可加一次修复。
5. **旧目标文本变更**：UUID 不受文本影响；这是选择 UUID 而不是 hash 的主要原因。
6. **是否给 Goal 加 `created_at`/`updated_at`**：本设计不做；需要时再加。

---

## 14. 一句话

> **新目标用 UUID；旧目标在 `StateService` 读到/应用时补 UUID 并保存一次；完成/放弃只用 ID；不改数据库 schema。**
