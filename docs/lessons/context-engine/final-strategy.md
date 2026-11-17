# ContextEngine 最终策略（冻结版 v2）

> 状态：决策记录，后续实现以此为准
> 日期：2026-11-17
> 关系：本文回答“做什么、为什么”；具体接口、事务和错误码见
> [context-engine-protocol.md](../context-engine-protocol.md)。
> 两者冲突时，先以本文的决策为准修正 protocol，再开始实现。

---

## 1. 范围

本期只做 **ContextEngine + turn 级历史存储 + 摘要压缩 + 归档线 + 动态工具选择**。

不在本期：

- AgentLoop；
- 三种执行模式；
- 工具执行循环；
- 归档恢复 / 检索 / 长期记忆；
- 每轮摘要。

已删除、不再回到旧设计：

```text
ContextBuilder
ContextPlanner
BuiltContext
PrepareContext
请求级 SummaryCoordinator
state_floor
min_recent_turns
min_history_tokens
commit_trim 作为压缩主路径
```

---

## 2. Prompt 消息布局

system 必须保持稳定，方便命中前缀缓存。state / summary 不再进 system，
而是各走一条 `user` 消息：

```text
system: 基础 instructions + 固定的“上下文说明”
user:   <state>...</state>
user:   <summary>...</summary>
...history messages...
user:   最新用户输入
```

规则：

- `state` 和 `summary` 各一条 user 消息，不合并；
- 空组件直接跳过，不发送空消息；
- 最新的真实用户输入放在最后，不加标签；
- system 中固定说明：`<state>` / `<summary>` 是会话上下文，不是用户的新请求；
- state / summary 的内容里如果出现 `<state>`、`</state>`、`<summary>`、
  `</summary>`，渲染时必须转义，避免标签注入；
- 这套顺序只缓存 system 前缀；state / summary 放在 history 前面，
  history 前缀不会命中缓存。这是接受的取舍；
- history 里的消息保持原样，不额外包上下文标签。

---

## 3. 预算与容量

### 3.1 模型窗口

```text
window      = 256K
reserve     = 8K
available   = 248K
```

### 3.2 组件硬上限

| 组件 | cap |
|---|---:|
| system | 4K |
| user | 4K |
| tools | 8K |
| state | 4K |
| summary | 8K |
| history | 80K |
| safety | 512 |

```text
静态 configured_capacity = 4K + 4K + 8K + 4K + 8K + 80K + 512 = 108,512
```

组件之间**不互相借预算**。每个组件最多用到自己的 ceiling，用不完的额度
不回收给其他组件。所有 ceiling 之和必须小于等于 `available`，否则启动失败。

### 3.3 报告分母

预算按固定份额分配，所有组件恒定计入，没有 per-turn 开关。分母就是静态的
`configured_capacity`：

```text
configured_capacity    = 4K + 4K + 8K + 4K + 8K + 80K + 512 = 108,512
configured_utilization = total_tokens / configured_capacity
window_utilization     = total_tokens / available_tokens
total_tokens           = message_tokens + tool_tokens
```

- 分母不随本轮是否使用工具变化；tools 的 8K 是预留份额，不用也不会分给
  其他组件；
- `safety` 只进分母，不重复进分子；
- 不再有 `active_capacity`：它恒等于 `configured_capacity`，没有信息量；
- 某个部署确实不要 tools 时，在构造 `ContextLimits` 时把 `tools_ceiling`
  设为 0，`configured_capacity` 自然变成 100,512。这是配置级开关，不是
  轮次级判断。

history 压缩触发**不用** `configured_utilization`，而用独立的
`history_utilization`，见第 5 节。

---

## 4. Turn 级存储

历史存储粒度从“每条消息”改为“每一轮”。

### 4.1 主表

`session_turns`：

```text
session_id
seq              # 每 session 从 1 递增，范围查询和覆盖水位用
turn_id          # 幂等键
messages_json    # 完整一轮消息
token_count      # 写入时算好
created_at
PRIMARY KEY(session_id, seq)
UNIQUE(session_id, turn_id)
```

### 4.2 归档表

`session_turns_archive` 与主表同构，多一个 `archived_at`。

`load` **只读主表**。归档表是冷数据，不自动重新进入 prompt。

### 4.3 摘要表

`session_summaries`：

```text
session_id
summary_json
covered_through_seq   # 0 表示未覆盖任何 turn
updated_at
```

`covered_through_seq` 是唯一覆盖真相源。不再使用消息级
`covered_through_id`。

### 4.4 TurnRecord

```python
@dataclass(frozen=True)
class TurnRecord:
    turn_id: str
    seq: int
    messages: tuple[dict, ...]
    token_count: int
    created_at: str
```

`load` 返回 `turns: tuple[TurnRecord, ...]`，只包含
`seq > covered_through_seq` 的轮次。

### 4.5 写入与校验

- `append_turn_once(session_id, turn_id, messages)` 一轮一个事务；
- `turn_id` 已存在则返回 `False`，不重复写；
- `seq` 分配必须事务化，且不能复用已归档的序号；
- 写入时校验：
  - 消息非空；
  - 第一条是 `user`；
  - 一轮只有一个 `user`；
  - 最后一条是无 `tool_calls` 的 `assistant`；
  - 不允许 `system`；
  - `assistant(tool_calls)` 与后续 `tool` 结果一一配对；
  - `tool` 结果顺序与 `tool_calls` 一致；
  - 每个 `tool` 都有 `tool_call_id`；
- 校验通过后计算 `token_count` 并落库；
- turn 落库后不可变；
- 本轮不做 content 占位。未来可以把大 tool 结果移走，只在 content 里保留
  token 数和摘要，但那是后续 schema 变更。

`session_messages` 彻底删除，不保留旧表。

### 4.6 元数据表

两个支撑表，都不进 prompt 路径：

`session_turn_counters(session_id, next_seq)`：每 session 一行，和
`session_turns` 插入在同一个写事务里更新，保证 `seq` 事务化分配。没有它就只能
`MAX(seq) + 1`，并发下会重号，归档后也可能复用序号。

`history_schema_meta(version)`：只存 schema 版本（当前 `2`）。`initialize()`
必须在 `CREATE TABLE IF NOT EXISTS` 之前读它，才能区分“新建库 / 版本兼容 /
旧 schema”。旧 schema 一律 fail fast，不做静默迁移。

### 4.7 load 的预览参数

```python
def load(
    self,
    session_id: str,
    *,
    limit_turns: int | None = None,
) -> HistorySnapshot: ...
```

`limit_turns` 只用于展示 / API 预览：先取最新 N 轮，返回时仍按 seq 升序。

硬约束：`ContextEngine` 和 `HistoryCompactor` 必须用**全量** active turns，
不得传 `limit_turns`。否则 `active_turns`、`history_tokens`、`last6_tokens`
全部失真，压缩触发和硬上限判断都会错。压缩路径不暴露这个参数。

---

## 5. 摘要与压缩

### 5.1 只有 RollingSummary

不为每轮生成摘要。全程只维护一个 `RollingSummary`。

压缩 = 旧 RollingSummary + 所有待压缩 turn → 一次 LLM 调用 → 新 RollingSummary。

同一 session 内串行；不同 session 可以并行。

### 5.2 触发指标

```text
active_turns        = load 到的 turn 数
history_tokens      = 所有未覆盖 turn 的 token_count 之和
last6_tokens        = 最近 6 个完整 turn 的 token_count 之和
history_utilization = history_tokens / history_ceiling   # 80K
```

如果 `active_turns < 6`，`last6_tokens` 就是全部 turn 的 token 和。

### 5.3 判断顺序

必须按这个顺序：

```text
1. if last6_tokens > history_ceiling:
       HISTORY_FLOOR_UNMET

2. elif active_turns > 6 and history_utilization >= 0.60:
       compact

3. elif active_turns == 6 and history_utilization >= 0.60:
       HISTORY_OVER_TARGET（WARNING issue，不压缩）

4. else:
       no-op
```

`HISTORY_FLOOR_UNMET` 只看最近 6 轮，不看总 history。

`HISTORY_OVER_TARGET` 是 `WARNING` issue，不是异常，不阻塞回答。

当前口径是 `active_turns == 6`；`active_turns < 6` 且超过 60% 不产生
`HISTORY_OVER_TARGET`。这是已知边界，后续可改成 `<= 6`，只影响 WARNING
issue，不影响压缩行为。

### 5.4 压缩范围

正常情况：覆盖“最后 6 轮之前”的全部 turn。

```text
target_turns = turns[:-6]
target_seq   = target_turns[-1].seq
```

压缩后 `active_turns` 应为 6；如果最后 6 轮仍然超过 60%，下一轮按
`HISTORY_OVER_TARGET` 报告，不再重复压缩。

### 5.5 摘要输入保护

摘要输入使用独立策略上限：

```text
SUMMARY_INPUT_CEILING = 100_000
effective_input_ceiling = min(
    SUMMARY_INPUT_CEILING,
    available_tokens - max(model_max_tokens),
)
```

若“旧摘要 + 待压缩 turn + prompt 开销”超过
`effective_input_ceiling`，只取最老的连续 turn 前缀，让本次摘要请求放得下：

```text
target_turns = 能放下的最老连续前缀
```

下一轮继续压缩剩余 turn。若一个 turn 都放不下，本轮跳过压缩并告警，
不推进 `covered_through_seq`；到达硬上限时按硬上限失败。

`SUMMARY_INPUT_CEILING` 是配置常量，放在 `history/summarizer.py`。
100K 足以覆盖“8K 旧摘要 + 80K history + prompt 开销”；history 超过 100K
时允许分多轮压缩。

这是输入保护，不改变正常路径的“一次调用”。

### 5.6 事务 A

```text
生成摘要
→ save_summary_cas(
      summary,
      covered_through_seq=target_seq,
      expected_covered_through_seq=snapshot.covered_through_seq,
  )
→ 不删除 turn
```

失败：

```text
CAS 冲突：丢弃本次摘要，不重试，下一轮重新判断
摘要失败：保留旧摘要和旧水位，不压缩，告警，下一轮重试
```

同一 session 用一个 `asyncio.Lock` 串行压缩；CAS 负责多进程冲突。

### 5.7 摘要生成重试

`LLM` 增加：

```python
@dataclass(frozen=True)
class LLMResponse:
    message: Any
    finish_reason: str | None
    usage: dict | None
```

`LLM.chat_with_meta(...)` 返回 `LLMResponse`；`LLM.chat(...)` 保留旧签名。

档位：

```text
local_max_tokens = [2024, 4096, 8192]
model_max_tokens = int(local_max_tokens * 0.7)
default local    = 2024
```

规则：

```text
local_truncated -> local 升一档，model 不变
model_truncated -> local + model 同时升一档
```

`model_truncated` 用 `finish_reason == "length"` 判断。
异常或空摘要不升级档位，只消耗一次尝试；三次都失败则保留旧摘要和旧水位。

记录字段：

```text
attempt
local_max_tokens
model_max_tokens
local_truncated
model_truncated
finish_reason
usage
```

---

## 6. 归档线

### 6.1 阈值

```text
soft_limit = 600
hard_limit = 1000
```

按 session 的主表 turn 数计算，不按字节、不按消息数。

### 6.2 软归档（>= 600）

```text
archive_covered(session_id)
```

归档**所有** `seq <= covered_through_seq` 的 turn：

```text
事务 B：
  INSERT INTO session_turns_archive
  DELETE FROM session_turns WHERE seq <= covered_through_seq
```

- 不保留最近 200 轮缓冲；
- 失败只 warning，不影响本轮回答；
- 没有已覆盖 turn 就跳过，等压缩推进；
- 归档后主表只剩未覆盖 turn；
- `load` 仍然只读主表。

### 6.3 硬归档（>= 1000）

```text
1. 先做一次软归档
2. 如果主表仍 >= 1000：
       强制压缩一次（忽略 60% 条件，但仍保留最后 6 轮 floor）
       再归档所有已覆盖 turn
3. 如果仍 >= 1000：
       HISTORY_HARD_CAP_EXCEEDED
```

强制压缩也失败：

- 最近 6 轮超过 80K：`HISTORY_FLOOR_UNMET`；
- 摘要失败：硬上限失败。

`finish_turn` 记录硬上限错误；下一轮 `begin_turn` 再尝试一次
`ensure_hard_cap()`，仍失败才阻塞下一轮。

### 6.4 归档恢复

恢复是数据层，重新进 prompt 是装配层，两者不自动绑定。

没有 200 轮缓冲后，任何已归档 turn 的恢复都要读 `session_turns_archive`。
当前不实现恢复/回退/检索。

---

## 7. 工具

### 7.1 动态选择

`ToolComponent` 不再按输入顺序直接裁剪，而是走 `ToolSelector`：

```text
候选过滤 → 相关性打分 → 预算裁剪
```

候选过滤：

- 白名单 `allowed_tools` 是静态配置，同时传给 `ToolSelector` 和 runtime；
- selector 先过滤候选，runtime 执行前再拒绝一次，属于双层保护；
- `required_tools` 必须存在、在白名单内，并且放得进
  `tools_ceiling` / `max_tools`，否则抛 `TOOLS_REQUIRED_MISSING`；
- optional 才允许被丢弃。

v1 打分：

```text
required 固定最高
+ 用户输入命中工具 name / description
+ state 的 goals / constraints / preferences 命中
+ 后续 tags / priority
```

不额外调用 LLM，中文用稳定子串匹配，同分按 registry 顺序。
后续演进：

```text
v1 关键词匹配
v2 tags / priority
v3 embedding / 语义检索
```

选择规则：

```text
1. required 先放
2. optional 按 score 降序，同分按 registry 顺序
3. 从高到低取，直到 max_tools / tools_ceiling 放不下
4. score=0 的工具，预算还有空间时仍然可以入选
5. required 放不下 -> TOOLS_REQUIRED_MISSING
6. 被预算/max_tools 丢弃的 optional -> dropped_names
```

打分只用于排序，不作为排除条件。预算和 max_tools 都足够时，所有 allowed
tools 都会进入上下文；预算/max_tools 不够时才从分数最低的工具开始丢。

选择结果是 `ToolSelection`：

```text
tools
selected_names
dropped_names
scores
reason
tokens
```

`ContextReport.tool_selection` 直接引用这个 `ToolSelection`。没有启用 tools
时为 `None`。

`dropped_names` 包含被白名单过滤、以及被预算/max_tools 丢弃的工具。

### 7.2 工具结果

- 模型调用不在白名单里的工具时，runtime 合成一条失败 tool 消息并继续；
- 工具结果统一格式：

```json
{"success": true, "result": "..."}
{"success": false, "error": "..."}
```

- 每条 tool 消息必须带 `tool_call_id`；
- 并发执行时先按 `tool_call_id` 建结果集，再按 assistant
  `tool_calls` 的顺序写回；
- 超时、异常、白名单拒绝都要补失败 tool 消息，不能缺结果。

### 7.3 执行侧 middleware

`ContextResult.tools` 就是本轮实际暴露给模型的 `exposed_tools`。
middleware 校验顺序：

```text
1. tool name 在 registry 中存在
2. tool name 在 exposed_tools 中
3. tool name 在 allowed_tools 中
4. arguments 通过 schema 校验
5. 执行
```

失败都合成 tool 消息继续，不抛异常：

```text
TOOL_NOT_FOUND       registry 里没有
TOOL_NOT_EXPOSED     本轮 context 没暴露
TOOL_NOT_ALLOWED     白名单不允许
INVALID_ARGUMENTS    参数校验失败
```

这些是 tool 消息里的错误码，不进入 `ContextIssue`。

只执行 `exposed_tools`。虽然在白名单里、但被 Context 因预算/max_tools
丢弃的工具，不允许执行，统一返回 `TOOL_NOT_EXPOSED`。

---

## 8. 报告与 Issue

`ContextReport` 只放装配指标：

```text
各组件 used / grant / ceiling / truncated / dropped
message_tokens / tool_tokens / total_tokens
available_tokens / configured_capacity
window_utilization / configured_utilization
history_tokens / history_rendered_tokens / history_utilization
active_turns / last6_tokens
summary_render_truncated
tool_selection (ToolSelection | None)
issues
```

- `history_tokens`：所有未覆盖 turn 的 token 和，用于触发判断；
- `history_rendered_tokens`：实际进入 prompt 的 history token；
- `last6_tokens`：完整最近 6 轮；
- 压缩次数、摘要重试、covered lag 写日志，不进 `ContextReport`；
- `ContextReport.issues` 只放非阻塞 issue；
- 阻塞错误放 `ContextError.issue`，不混进正常报告。

### 8.1 ContextIssue

错误和警告统一用 `ContextIssue`，不再维护两套 code：

```python
class IssueLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class ContextIssue:
    code: ContextIssueCode
    level: IssueLevel
    message: str
    component: str | None = None
    details: dict = field(default_factory=dict)

    @property
    def title(self) -> str: ...        # 从 ISSUE_CATALOG 查
    @property
    def description(self) -> str: ...  # 从 ISSUE_CATALOG 查
```

- `code`：稳定机器码，控制流和测试用；
- `level`：`INFO` / `WARNING` / `ERROR`；
- `message`：本次实例的动态描述；
- `component`：来源组件；
- `details`：JSON 可序列化的结构化数据；
- `title` / `description`：静态文案，放 `IssueSpec` catalog，不在每个实例重复。

### 8.2 默认级别

| code | default level | 场景 |
|---|---|---|
| `SYSTEM_OVER_BUDGET` | ERROR | system 启动校验失败 |
| `USER_INPUT_TOO_LONG` | ERROR | 最新 user 超过 `user_ceiling` |
| `TOOLS_REQUIRED_MISSING` | ERROR | required 工具缺失/被白名单过滤/放不下 |
| `HISTORY_FLOOR_UNMET` | ERROR | 最近 6 turn 超过 `history_ceiling` |
| `HISTORY_OVER_TARGET` | WARNING | 6 turn 未超 ceiling 但 >60%，只报告 |
| `HISTORY_HARD_CAP_EXCEEDED` | ERROR | 硬归档/强制压缩后主表仍 >= 1000 |
| `HISTORY_ARCHIVE_FAILED` | WARNING | 软归档失败 |
| `SUMMARY_GENERATION_FAILED` | WARNING | 摘要生成/保存失败 |
| `SUMMARY_INPUT_TOO_LONG` | WARNING | 摘要输入连一个 turn 都放不下 |
| `SUMMARY_CAS_CONFLICT` | INFO | 并发压缩 CAS 冲突 |
| `BUDGET_INFEASIBLE` | ERROR | 固定成本无法放入 available |
| `COMPONENT_CONTRACT_VIOLATION` | ERROR | 组件返回非法结果 |
| `FINAL_OVER_BUDGET` | ERROR | 最终 messages + tools 超限 |
| `REPORT_MISMATCH` | ERROR | 报告与实际账不一致 |

`HISTORY_OVER_TARGET` 是 `WARNING` issue，不是异常。
`ContextError` 携带 `issue`，且 `issue.level == ERROR`。

### 8.3 报告格式化

`ContextReport` 提供三个纯格式化方法：

```python
to_log() -> dict
format() -> tuple[DisplaySection, ...]
to_display() -> str
```

- `to_log()`：给日志，结构化 dict，不含 tool schema，不含长 description；
- `format()`：给 UI 的结构化 sections，可渲染成表格/卡片；
- `to_display()`：给终端/文本，把 `format()` 渲染成表格文本。

数据结构：

```python
@dataclass(frozen=True)
class DisplayRow:
    cells: tuple[str, ...]
    level: IssueLevel | None = None
    description: str | None = None


@dataclass(frozen=True)
class DisplaySection:
    title: str
    columns: tuple[str, ...]
    rows: tuple[DisplayRow, ...]
```

`format()` 至少包含：

```text
Budget
Components
History
Tools
Issues
```

`to_display()` 的预算表：

```text
=== Context Budget Report ===
Model Window:                 256,000
Reserved for Output:            8,000
Available for Input:          248,000
Safety (denominator only):        512

Configured Capacity:          108,512
Message Tokens:                45,825
Tool Tokens:                    2,100
Total Used:                    47,925
Window Utilization:             19.3%

--------------------------------------------------------------------------------
Component        Used       Cap      Used%    Status       Detail
--------------------------------------------------------------------------------
System            120      4,000      3.0%    OK
State             345      4,000      8.6%    OK
Summary           280      8,000      3.5%    OK
History        45,000     80,000     56.3%    TRUNCATED    loaded 52K, rendered 45K, dropped 3 turns
Tools           2,100      8,000     26.3%    TRIMMED      selected 4, dropped 2
User               80      4,000      2.0%    OK
--------------------------------------------------------------------------------
Total          47,925    108,512     44.2%    -
```

规则：

- `History.Used` 用 `history_rendered_tokens`；
- `History.Detail` 显示 loaded / rendered / active_turns / last6_tokens；
- `Tools.Detail` 显示 selected / dropped / reason，不显示 schema；
- `Summary.Detail` 显示 render_truncated；
- `State.Detail` 显示 dropped_sections；
- 状态值：`OK` / `TRUNCATED` / `TRIMMED` / `EMPTY`；
- 当前 `grant = cap`，表里先不单列 `Grant`。

Issues 部分必须包含 `ContextIssue.description`：

```text
=== Issues ===
WARNING  HISTORY_OVER_TARGET
  message:     未覆盖历史 52K / 80K（65%），当前 6 轮不能压缩
  description: 历史超过 60% 目标但受 6 轮 floor 限制...
```

日志与展示都不得输出完整 tool schema。

---

## 9. 旧实现处置

删除/替换：

```text
ContextBuilder
ContextPlanner
BuiltContext
PrepareContext
请求级 SummaryCoordinator
session_messages
session_messages_archive
TrimPlan / plan_trim / commit_trim
消息级 covered_through_id
```

保留/修改：

```text
TokenBudget
StateRenderer
SummaryRenderer
HistorySummarizer
HistoryStore（重写为 turn 级）
StateSnapshot（移到 state/models.py）
```

旧库不兼容新表结构，`initialize()` 检测到旧 schema 时必须 fail fast，
提示删除或迁移数据库，不做静默破坏性迁移。

---

## 10. 实现顺序

1. 数据模型：`TurnRecord` / `HistorySnapshot` / `RollingSummary` /
   `ContextLimits` / `RenderedComponent` / `ContextReport` /
   `ContextIssue` / `IssueSpec` / `ToolSelection` /
   `DisplayRow` / `DisplaySection`；
   `history/metrics.py` 提供共享的 `HistoryMetrics` / `analyze` / `decide`；
2. `StateSnapshot` 移到 `state/models.py`；
3. `HistoryStore` 新 schema、`append_turn_once`、`load`、
   `save_summary_cas`、`archive_covered`、`count_turns`；
4. `LLMResponse` + `chat_with_meta`；
5. `HistorySummarizer` 档位重试 + 输入保护；
6. `SummaryRenderer` 句子前缀截断；
7. `ContextEngine` + 组件 + `ToolSelector` + 消息布局 + 报告；
8. `HistoryCompactor`：触发顺序、per-session lock、CAS、软/硬归档；
9. `TurnOrchestrator` 接入 `begin_turn` / `finish_turn`；
10. 删除旧 Builder/Planner，补测试和旧库 fail-fast 测试。

---

## 11. 评审后补入的硬约束

这些是设计评审时发现、必须写进实现的边界：

- 摘要输入必须做窗口保护，否则一次压缩可能超过模型窗口；
- `seq` 分配必须事务化，且归档后不能复用序号；
- state / summary 标签必须转义，防止内容里的伪标签破坏上下文边界；
- 旧 schema 必须 fail fast，不能和新的 `session_turns` 静默混用；
- 硬上限判断用“归档后主表仍 >= 1000”，不是“完全没有已覆盖 turn”；
- 归档只处理 `seq <= covered_through_seq` 的 turn，绝不删除未摘要内容；
- 工具选择必须先过白名单，required 工具永远不能被丢掉；
- `limit_turns` 只能用于预览，engine / compactor 必须读全量 active turns；
- 错误和警告统一用 `ContextIssue`，用 `level` 分层，不再维护两套 code。

已知边界（当前接受，不在本期修）：

- `active_turns < 6` 且超过 60% 时没有 `HISTORY_OVER_TARGET` 告警；
- 连续多条 user 消息依赖 system 说明和标签区分，真实模型需要回归测试；
- 动态工具选择 v1 只做关键词排序，tags / priority / embedding 后续接入；
- `SUMMARY_INPUT_CEILING=100K`，history 超过 100K 时分多轮压缩；
- 没有 200 轮保留缓冲，归档恢复会读取 archive。
