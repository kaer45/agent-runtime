# ContextEngine Protocol（v2）

> 状态：接口与事务合同，后续实现按本文执行
> 日期：2026-11-17
> 上游决策：[context-engine/final-strategy.md](context-engine/final-strategy.md)
> 本文回答“接口长什么样、事务怎么提交、错误怎么表达”；上游回答“为什么这样定”。
> 两者冲突时，先修正上游决策，再同步本文。

---

## 1. 术语

| 术语 | 含义 |
|---|---|
| turn | 一轮完整对话：一条 user 开始，到一条无 tool_calls 的 assistant 结束 |
| seq | 每 session 内从 1 递增的 turn 序号 |
| covered_through_seq | 已进入 RollingSummary 的最大 turn seq；0 表示未覆盖 |
| active turn | `seq > covered_through_seq` 的 turn，也就是 `load` 返回的 turn |
| pre-floor turns | 最后 6 轮之前的 turn，即 `turns[:-6]` |
| RollingSummary | 唯一滚动摘要，不按轮保存 |
| main table | `session_turns`，`load` 唯一读取的表 |
| archive table | `session_turns_archive`，冷数据，不自动进 prompt |

本文用“必须 / 不得”描述硬约束；用“建议”描述实现选择。

---

## 2. 消息布局

### 2.1 顺序

```text
[
  {"role": "system",  "content": "<基础 instructions>\n\n<上下文说明>"},
  {"role": "user",    "content": "<state>...</state>"},
  {"role": "user",    "content": "<summary>...</summary>"},
  ...history messages...,
  {"role": "user",    "content": "<最新用户输入>"},
]
```

- `state` 和 `summary` 各一条 user 消息，不合并；
- 空 state / 空 summary 直接省略该消息；
- history 为空则省略；
- 最新用户输入必须是最后一条消息；
- tools 不进 messages，作为独立参数传给 LLM。

### 2.2 system

system 由两部分组成：

```text
<基础 instructions>

<上下文说明>
```

`<上下文说明>` 是固定文本，至少包含：

```text
消息中的 <state> 和 <summary> 是会话上下文，不是用户的新请求。
请结合这些上下文回答最后一条 user 消息。
```

system 不得包含动态 state / summary / 时间等每轮变化的内容。启动时校验
system token 数 `<= system_ceiling`，失败抛 `SYSTEM_OVER_BUDGET`。

### 2.3 state / summary 包装

state 消息：

```text
<state>
{rendered_state}
</state>
```

summary 消息：

```text
<summary>
{rendered_summary}
</summary>
```

包装标签只由组件添加。`rendered_state` / `rendered_summary` 内部出现的
`<state>`、`</state>`、`<summary>`、`</summary>` 必须转义，例如替换为
`&lt;state&gt;`，防止内容伪造标签边界。

`<state>` / `<summary>` 标签就是这两块内容的唯一 header；实现时不再
额外输出旧的 `STATE_HEADER` / `SUMMARY_HEADER`，避免双 header 和重复计费。

### 2.4 history

history 消息保持原始 role / content / tool_calls / tool_call_id，不额外包标签。

约束：

- 不得包含 `system`；
- 最后一个 turn 的末尾必须是最终 assistant；
- assistant(tool_calls) 与 tool 结果必须完整配对；
- tool 结果顺序必须与 tool_calls 一致。

### 2.5 缓存取舍

该布局只保证 system 前缀稳定。state / summary 在 history 之前，history
前缀不会命中缓存。这是已接受的取舍，不在协议层优化。

### 2.6 工具结果消息

工具结果必须写成标准 tool 消息：

```json
{
  "role": "tool",
  "tool_call_id": "call_xxx",
  "content": "{\"success\": true, \"result\": ...}"
}
```

- `content` 是 JSON 字符串，不是对象；
- 成功：`{"success": true, "result": ...}`；
- 失败：`{"success": false, "error": "..."}`；
- 每条 tool 消息必须带 `tool_call_id`；
- 每个 assistant `tool_calls[i].id` 必须有且只有一条对应 tool 消息；
- 并发执行时先按 `tool_call_id` 建结果映射，再按 assistant `tool_calls`
  的顺序写回；
- 超时、异常、白名单拒绝都必须补失败 tool 消息，不能缺结果。

---

## 3. 数据模型

### 3.1 ContextLimits

```python
@dataclass(frozen=True)
class ContextLimits:
    window_tokens: int = 256_000
    reserve_output: int = 8_000

    system_ceiling: int = 4_000
    user_ceiling: int = 4_000
    tools_ceiling: int = 8_000
    state_ceiling: int = 4_000
    summary_ceiling: int = 8_000
    history_ceiling: int = 80_000
    safety_tokens: int = 512
```

派生值：

```text
available_tokens          = window_tokens - reserve_output
configured_capacity       = sum(all ceilings) + safety_tokens
```

`configured_capacity` 是静态值：所有组件恒定计入，没有 per-turn 开关，
也不派生 `active_capacity`。报告的分母直接用它。

约束：

- 所有 ceiling / safety 非负；
- `reserve_output < window_tokens`；
- `configured_capacity <= available_tokens`，否则启动抛 `BUDGET_INFEASIBLE`；
- 组件之间不借预算，`grant = ceiling`。

所有 message 组件的 ceiling 都按 `TokenBudget.count_message` 计算，包含
消息开销，不是纯 content token；`tools_ceiling` 按 schema 文本
`count_text` 计算。history 的 `token_count` 也使用同一口径，否则
`last6_tokens` 和 `history_rendered_tokens` 会对不上。

`ContextLimits` 只负责静态上限；报告分母直接用 `configured_capacity`，
不在每轮重算。

### 3.2 TurnRecord

```python
@dataclass(frozen=True)
class TurnRecord:
    turn_id: str
    seq: int
    messages: tuple[dict, ...]
    token_count: int
    created_at: str
```

约束：

- `seq >= 1`；
- `token_count > 0`，由消息计数口径计算：`TokenBudget.count_messages` 与
  `core.tokens.count_messages` 是同一份实现，历史层不反向依赖 `context`；
- `token_count` 必须用和 engine / compactor 相同的 counter 计算；
  更换 tokenizer 时要重算历史 turn，不能混用旧计数；
- `messages` 非空且已通过 turn 校验；
- turn 落库后不可变。

### 3.3 RollingSummary

```python
@dataclass(frozen=True)
class RollingSummary:
    text: str = ""
```

不再有 `covered_messages` / `truncated` 字段。覆盖位置由
`covered_through_seq` 表达；生成层是否截断只记日志，不进入存储模型。

### 3.4 HistorySnapshot

```python
@dataclass(frozen=True)
class HistorySnapshot:
    turns: tuple[TurnRecord, ...]
    summary: RollingSummary
    covered_through_seq: int
```

`turns` 只包含 `seq > covered_through_seq` 的轮次，按 seq 升序。
`covered_through_seq=0` 表示没有摘要覆盖。

### 3.5 ContextRequest / ContextResult

```python
@dataclass(frozen=True)
class ContextRequest:
    session_id: str
    turn_id: str
    user_input: str
    state: StateSnapshot
    history: HistorySnapshot
    tools: tuple[dict, ...] = ()
    required_tools: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ContextResult:
    messages: list[dict]
    tools: list[dict]
    report: ContextReport
```

`required_tools` 由 `ToolSelector` 处理：required 工具必须存在、在白名单内，
并且能放进 `tools_ceiling` / `max_tools`；否则抛 `TOOLS_REQUIRED_MISSING`。
required 工具永远不会被当作 optional 丢掉。

### 3.6 RenderedComponent

```python
@dataclass(frozen=True)
class RenderedComponent:
    messages: tuple[dict, ...] = ()
    tools: tuple[dict, ...] = ()
    tokens: int = 0
    truncated: bool = False
    dropped: tuple[str, ...] = ()
```

合同：

- `tokens` 必须等于 engine 重算的 token 数；
- `tokens <= max_tokens`；
- messages 中不得出现空 content 的消息；
- `dropped` 放被丢弃的 section / turn / tool 名称；
- 每个组件只能输出自己负责的槽位：state/summary 不得往 tools 塞东西，
  tool 组件不得往 messages 塞东西。

### 3.7 ContextReport

```python
@dataclass(frozen=True)
class ComponentUsage:
    used: int
    grant: int
    ceiling: int
    truncated: bool = False
    dropped: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextReport:
    components: dict[str, ComponentUsage]
    limits: ContextLimits

    message_tokens: int
    tool_tokens: int
    total_tokens: int
    available_tokens: int

    configured_capacity: int
    window_utilization: float
    configured_utilization: float

    history_tokens: int
    history_rendered_tokens: int
    history_utilization: float
    active_turns: int
    last6_tokens: int

    summary_render_truncated: bool
    tool_selection: ToolSelection | None
    issues: tuple[ContextIssue, ...]
```

口径：

```text
message_tokens           = 所有最终 messages 的 count_message 之和
tool_tokens              = 所有最终 tools schema 的 count_text 之和
total_tokens             = message_tokens + tool_tokens
window_utilization       = total_tokens / available_tokens
configured_utilization   = total_tokens / configured_capacity
history_tokens           = sum(turn.token_count for turn in loaded turns)
history_rendered_tokens  = 实际进入 prompt 的 history messages 的 count_message 之和
history_utilization      = history_tokens / history_ceiling
last6_tokens             = 最近 6 个完整 turn 的 token_count 之和
```

`configured_capacity` 是静态 108,512，不随本轮是否使用工具变化。某个部署
不要 tools 时，把 `ContextLimits.tools_ceiling` 设为 0，它会变成 100,512。

`history_utilization` 不 clamp 到 1.0；大于 1.0 表示未覆盖历史已经超过
`history_ceiling`。`HISTORY_FLOOR_UNMET` 仍只看 `last6_tokens`。

### 3.8 ContextIssue

统一用 issue 表达错误、警告和状态。不要再维护两套
`ContextErrorCode` / `ContextWarningCode`。

```python
class IssueLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ContextIssueCode(str, Enum):
    SYSTEM_OVER_BUDGET = "SYSTEM_OVER_BUDGET"
    USER_INPUT_TOO_LONG = "USER_INPUT_TOO_LONG"
    TOOLS_REQUIRED_MISSING = "TOOLS_REQUIRED_MISSING"
    HISTORY_FLOOR_UNMET = "HISTORY_FLOOR_UNMET"
    HISTORY_OVER_TARGET = "HISTORY_OVER_TARGET"
    HISTORY_HARD_CAP_EXCEEDED = "HISTORY_HARD_CAP_EXCEEDED"
    HISTORY_ARCHIVE_FAILED = "HISTORY_ARCHIVE_FAILED"
    SUMMARY_GENERATION_FAILED = "SUMMARY_GENERATION_FAILED"
    SUMMARY_INPUT_TOO_LONG = "SUMMARY_INPUT_TOO_LONG"
    SUMMARY_CAS_CONFLICT = "SUMMARY_CAS_CONFLICT"
    BUDGET_INFEASIBLE = "BUDGET_INFEASIBLE"
    COMPONENT_CONTRACT_VIOLATION = "COMPONENT_CONTRACT_VIOLATION"
    FINAL_OVER_BUDGET = "FINAL_OVER_BUDGET"
    REPORT_MISMATCH = "REPORT_MISMATCH"


@dataclass(frozen=True)
class IssueSpec:
    code: ContextIssueCode
    default_level: IssueLevel
    title: str
    description: str


@dataclass(frozen=True)
class ContextIssue:
    code: ContextIssueCode
    level: IssueLevel
    message: str
    component: str | None = None
    details: dict = field(default_factory=dict)

    @classmethod
    def from_code(
        cls,
        code: ContextIssueCode,
        message: str,
        *,
        level: IssueLevel | None = None,
        component: str | None = None,
        details: dict | None = None,
    ) -> "ContextIssue": ...

    @property
    def spec(self) -> IssueSpec: ...

    @property
    def title(self) -> str: ...

    @property
    def description(self) -> str: ...
```

字段口径：

- `code`：稳定机器码，控制流和测试用它；
- `level`：`INFO` / `WARNING` / `ERROR`，用于报告分层；
- `message`：本次实例的动态描述，可带 token 数、seq、工具名；
- `component`：来源组件或子系统；
- `details`：JSON 可序列化的结构化数据；
- `title` / `description`：从 `ISSUE_CATALOG[code]` 查静态文案，不重复存。

默认级别：

| code | default level |
|---|---|
| `SYSTEM_OVER_BUDGET` | ERROR |
| `USER_INPUT_TOO_LONG` | ERROR |
| `TOOLS_REQUIRED_MISSING` | ERROR |
| `HISTORY_FLOOR_UNMET` | ERROR |
| `HISTORY_OVER_TARGET` | WARNING |
| `HISTORY_HARD_CAP_EXCEEDED` | ERROR |
| `HISTORY_ARCHIVE_FAILED` | WARNING |
| `SUMMARY_GENERATION_FAILED` | WARNING |
| `SUMMARY_INPUT_TOO_LONG` | WARNING |
| `SUMMARY_CAS_CONFLICT` | INFO |
| `BUDGET_INFEASIBLE` | ERROR |
| `COMPONENT_CONTRACT_VIOLATION` | ERROR |
| `FINAL_OVER_BUDGET` | ERROR |
| `REPORT_MISMATCH` | ERROR |

约束：

- `ContextReport.issues` 只放非阻塞 issue；
- 阻塞错误放 `ContextError.issue`，不混进正常报告；
- `HISTORY_OVER_TARGET` 是 `WARNING` issue，不是异常；
- 日志可以复用 `ContextIssue`，但不要要求 UI 解析日志；
- `details` 不得包含异常对象、函数、连接等不可序列化值。

`IssueLevel` / `ContextIssueCode` / `IssueSpec` / `ContextIssue` /
`ISSUE_CATALOG` 放 `core/issues.py`，context 和 history 共用，避免
`history` 反向依赖 `context`。

### 3.9 ToolSelection

```python
@dataclass(frozen=True)
class ToolSelection:
    tools: tuple[dict, ...]
    selected_names: tuple[str, ...]
    dropped_names: tuple[str, ...]
    scores: dict[str, float]
    reason: str
    tokens: int
```

- `tools` 就是最终 `ContextResult.tools`，也是本轮 `exposed_tools`；
- `tokens` 是这些 schema 的 token 和；
- `ContextReport.tool_selection` 直接引用同一个 `ToolSelection`；
- 没有启用 tools 时 `ContextReport.tool_selection = None`。

### 3.10 ContextReport 格式化

`ContextReport` 提供三个纯方法，不产生日志副作用：

```python
def to_log(self) -> dict: ...
def format(self) -> tuple[DisplaySection, ...]: ...
def to_display(self) -> str: ...
```

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

约束：

- `len(row.cells) == len(section.columns)`；
- `level` 只用于 UI 着色/分组；
- `description` 只用于展示，日志不输出；
- 三个方法都不修改 report。

#### to_log

`to_log()` 返回 JSON 可序列化的 dict：

```text
message_tokens
tool_tokens
total_tokens
available_tokens
configured_capacity
window_utilization
configured_utilization
history_tokens
history_rendered_tokens
history_utilization
active_turns
last6_tokens
summary_render_truncated

components:
    name -> used / grant / ceiling / truncated / dropped_count

tools:
    selected_names / dropped_names / reason / tokens

issues:
    code / level / message / component / details
```

约束：

- 不包含完整 tool schema；
- 不包含 `ContextIssue.description` / `title`；
- `components.dropped` 在日志里压成 `dropped_count`；
- tuple 转 list，保证 JSON 可序列化。

#### format

`format()` 返回结构化 sections，至少包含：

```text
Budget
Components
History
Tools
Issues
```

`DisplaySection.columns` 建议：

```text
Budget:      ("Item", "Value")
Components:  ("Component", "Used", "Cap", "Used%", "Status", "Detail")
History:     ("Metric", "Value")
Tools:       ("Item", "Value")
Issues:      ("Level", "Code", "Message")
```

`Budget`/`Components` 的显示字段与 3.7 的 report 字段一致；`History` 显示
loaded / rendered / active_turns / last6_tokens；`Tools` 只显示 names / reason /
tokens，不显示 schema；`Issues` 每个 issue 一行，`description` 放在
`DisplayRow.description`，不放进 cells。

#### to_display

`to_display()` 用 `format()` 渲染终端文本，预算表格式固定为：

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

状态值：

```text
OK         没有截断/丢弃
TRUNCATED  内容被截断
TRIMMED    工具/候选项被预算裁剪
EMPTY      组件为空
```

Issues 部分必须展示 `description`：

```text
=== Issues ===
WARNING  HISTORY_OVER_TARGET
  message:     未覆盖历史 52K / 80K（65%），当前 6 轮不能压缩
  description: 历史超过 60% 目标但受 6 轮 floor 限制...
```

`to_display()` 和 `to_log()` 都不得输出完整 tool schema。

---

## 4. 组件接口

### 4.1 BaseComponent

```python
class BaseComponent(Protocol):
    name: str

    def tokens(self, ctx: AssemblyContext) -> int: ...

    def render(
        self,
        ctx: AssemblyContext,
        max_tokens: int,
    ) -> RenderedComponent: ...
```

`AssemblyContext` 至少包含：

```text
request: ContextRequest
limits: ContextLimits
counter: TokenCounter
```

组件不得读数据库、不得调用 LLM、不得修改 request。

### 4.2 组件顺序

```text
SystemComponent
StateComponent
SummaryComponent
HistoryComponent
ToolComponent
UserComponent
```

engine 按这个顺序调用 `render`，但最终 messages 顺序固定为：

```text
system → state user → summary user → history → latest user
```

tools 独立合并。

### 4.3 SystemComponent

- 输出一条 system 消息；
- 内容 = 基础 instructions + 固定上下文说明；
- `tokens` 在 engine 构造时校验 `<= system_ceiling`；
- 超限抛 `SYSTEM_OVER_BUDGET`；
- 运行时不截断。

### 4.4 StateComponent

- 输出一条 user 消息，内容用 `<state>` 包装；
- 使用现有 `StateRenderer` 的 section 顺序；
- 不实现 critical tier；
- 放不下时按 section 截断，不失败；
- 报告 `truncated` / `dropped_sections`；
- 空 state 输出空 `RenderedComponent`。

### 4.5 SummaryComponent

- 输出一条 user 消息，内容用 `<summary>` 包装；
- 存储摘要保持完整；
- 渲染超过 `summary_ceiling` 时按句子前缀截断；
- 截断只影响 prompt，不修改 DB；
- 报告 `render_truncated=True`，不失败；
- 空 summary 输出空 `RenderedComponent`。

### 4.6 HistoryComponent

- 从最新 turn 往前选，保持连续后缀；
- 只选完整 turn，不切 turn、不切 tool 组；
- 遇到第一个放不下的 turn 就停止，不再尝试更老的 turn；
- `history_rendered_tokens` 是实际选中的 token 和；
- 如果丢弃了 turn，`truncated=True`，`dropped` 放被丢弃的 seq；
- 不读 archive。

正常路径下 `last6_tokens <= history_ceiling`，所以至少最近 6 轮能放进 prompt。
如果最近 6 轮本身超过 80K，engine 在渲染前就抛 `HISTORY_FLOOR_UNMET`。

### 4.7 ToolSelector + ToolComponent

`ToolComponent` 不再自己按输入顺序裁剪，而是调用 `ToolSelector`。

```python
class ToolSelector:
    def __init__(
        self,
        *,
        allowed_tools: frozenset[str] | None = None,
        max_tools: int = 8,
    ): ...

    def select(
        self,
        *,
        user_input: str,
        state: SessionState,
        tools: Sequence[dict],
        required_tools: frozenset[str],
        ceiling: int,
    ) -> ToolSelection: ...
```

`allowed_tools` 语义：

- `None`：不做白名单过滤；
- 空集合：一个工具都不允许；
- 非空集合：只允许其中的工具。

`ToolSelector` / `ToolSelection` 放 `context/tool_selector.py`，不读 DB、
不调 LLM，只做纯选择。

白名单是静态配置，同时传给 `ToolSelector` 和 runtime；selector 先过滤候选，
runtime 在执行前再拒绝一次，属于双层保护。

#### 候选过滤

1. 从完整 tools schema 建 `name -> schema` 映射；
2. 用 `allowed_tools` 过滤候选；
3. `required_tools` 必须全部在候选中：
   - 不存在；
   - 被白名单过滤；
   - 放不进 `tools_ceiling` / `max_tools`；
   任一情况都抛 `TOOLS_REQUIRED_MISSING`。

#### v1 关键词打分

不额外调用 LLM：

```text
score(tool) =
    required          -> 固定最高
    + 用户输入命中 name / description
    + state 的 goals / constraints / preferences 命中
    + 未来 tags / priority
```

要求：

- 大小写归一化；
- 中文用稳定的子串匹配，不引入额外分词依赖；
- 同分按输入 registry 顺序，保证确定性；
- `scores` 只记录白名单过滤后的候选，方便排查。

后续扩展顺序：

```text
v1: 关键词匹配
v2: tags / priority
v3: embedding / 语义检索
```

#### 选择规则

```text
1. required 先按输入顺序放
2. optional 按 score 降序，同分按输入顺序
3. 从高到低取，直到 max_tools / tools_ceiling 放不下
4. score=0 的工具，预算还有空间时仍然可以入选
5. required 放不下 -> TOOLS_REQUIRED_MISSING
6. 被预算/max_tools 丢弃的 optional -> dropped_names
```

打分只用于排序，不作为排除条件。预算和 max_tools 都足够时，所有 allowed
tools 都会进入上下文；预算/max_tools 不够时才从分数最低的工具开始丢。

`dropped_names` 包含被白名单过滤、以及被预算/max_tools 丢弃的工具。
`selected_names` 和 `dropped_names` 合起来覆盖本次注册的完整工具集。

`reason` 允许值：

```text
required_only  只有 required，没有 optional
ranked         按分数排序后全部放得下
budget_trim    有 optional 因预算/max_tools 被丢
empty          没有可用工具
```

`ToolComponent` 只负责把 `ToolSelection.tools` 包成 `RenderedComponent`，
并把 `ToolSelection` 本身放进 `ContextReport.tool_selection`。

### 4.8 UserComponent

- 输出最新 user 消息，不加标签；
- 超过 `user_ceiling` 抛 `USER_INPUT_TOO_LONG`；
- 不截断用户输入；
- 错误消息必须提示“缩短或拆分输入”。

### 4.9 执行侧 middleware

`ContextResult.tools` 就是本轮实际暴露给模型的 `exposed_tools`。

middleware 校验顺序：

```text
1. tool name 在 registry 中存在
2. tool name 在 exposed_tools 中
3. tool name 在 allowed_tools 中
4. arguments 通过 schema 校验
5. 执行
```

失败统一合成失败 tool 消息继续，不抛异常：

```text
TOOL_NOT_FOUND       registry 里没有
TOOL_NOT_EXPOSED     本轮 context 没暴露
TOOL_NOT_ALLOWED     白名单不允许
INVALID_ARGUMENTS    参数校验失败
```

这些是 tool 消息里的错误码，不进入 `ContextIssue`。

只执行 `exposed_tools`。虽然在白名单里、但被 Context 因预算/max_tools
丢弃的工具，不允许执行，统一返回 `TOOL_NOT_EXPOSED`。

多个 tool_calls 并发执行时，按 `tool_call_id` 收集结果，再按 assistant
`tool_calls` 的顺序写回；失败/超时也要补一条失败 tool 消息。

---

## 5. ContextEngine

### 5.1 构造

```python
class ContextEngine:
    def __init__(
        self,
        *,
        system_prompt: str,
        limits: ContextLimits = ContextLimits(),
        counter: TokenCounter = estimate_tokens,
        tool_selector: ToolSelector | None = None,
    ): ...
```

`tool_selector=None` 时使用默认 `ToolSelector()`，即不做白名单过滤。
运行时通常由 `AgentRuntime` 用静态 `allowed_tools` 构造后注入。

构造时：

1. 校验 limits；
2. 计算 system token；
3. `system > system_ceiling` → `SYSTEM_OVER_BUDGET`；
4. `configured_capacity > available_tokens` → `BUDGET_INFEASIBLE`。

### 5.2 assemble

```python
def assemble(self, request: ContextRequest) -> ContextResult: ...
```

同步、纯函数、不读 DB、不调 LLM。

流程：

```text
1. 校验 request
2. 校验 latest user，最多 user_ceiling；超限抛 USER_INPUT_TOO_LONG
3. metrics = analyze(request.history.turns, limits.history_ceiling)
4. decision = decide(metrics)
5. decision == FLOOR_UNMET：抛 HISTORY_FLOOR_UNMET
6. decision == OVER_TARGET：追加 HISTORY_OVER_TARGET WARNING issue
7. 渲染 system
8. 渲染 state，最多 state_ceiling
9. 渲染 summary，最多 summary_ceiling
10. 渲染 history，最多 history_ceiling
11. ToolSelector.select + ToolComponent.render，最多 tools_ceiling
12. 按固定顺序拼 messages
13. 重算 message_tokens / tool_tokens
14. 校验 total_tokens <= available_tokens，否则 FINAL_OVER_BUDGET
15. 校验组件报告账，不一致则 REPORT_MISMATCH
16. 生成 ContextReport
```

`HISTORY_OVER_TARGET` 在这里追加 `ContextIssue`，不抛异常。
`HISTORY_FLOOR_UNMET` 在渲染前抛出，避免先花时间渲染再失败。

`analyze` / `decide` 放 `history/metrics.py`，engine 和 compactor 共用；
不要在两边各写一套触发顺序。`ContextEngine` 只负责把
`FLOOR_UNMET` / `OVER_TARGET` 翻译成错误或 issue，不执行压缩。

### 5.3 report 组装

- `components` 必须包含全部 6 个组件，不因本轮内容为空而省略；
- `grant = ceiling`；
- 没有 per-turn 容量开关，报告分母恒为 `configured_capacity`；
- `tool_selection` 来自 `ToolSelector`，没有 tools 时为 `None`；
- `issues` 只放非阻塞 issue，例如 `HISTORY_OVER_TARGET`；
- 阻塞错误放 `ContextError.issue`，不混进 report；
- 不再有 `status` 和 `warnings` 字段。

---

## 6. HistoryStore

### 6.1 Schema

```sql
CREATE TABLE session_turns (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    turn_id TEXT NOT NULL,
    messages_json TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, seq),
    UNIQUE (session_id, turn_id)
);

CREATE TABLE session_turns_archive (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    turn_id TEXT NOT NULL,
    messages_json TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE session_summaries (
    session_id TEXT PRIMARY KEY,
    summary_json TEXT NOT NULL,
    covered_through_seq INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE session_turn_counters (
    session_id TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL
);

CREATE TABLE history_schema_meta (
    version INTEGER NOT NULL
);
```

索引：

```sql
CREATE INDEX idx_session_turns_session_seq
ON session_turns(session_id, seq);

CREATE INDEX idx_session_turns_archive_session_seq
ON session_turns_archive(session_id, seq);
```

### 6.2 Schema 版本与旧库

当前版本 `2`。

`initialize()`：

- 新建库：创建上述表，写入 version 2；
- 已有 `history_schema_meta.version == 2`：正常；
- 检测到旧 schema（`session_messages`、旧结构 `session_turns`、
  `covered_through_id` 等）：抛 `HistoryStoreError`，提示删除或迁移数据库；
- 检测必须发生在 `CREATE TABLE IF NOT EXISTS` 之前，不能先建新表再看；
- 不做静默 drop / 自动迁移。

### 6.3 append_turn_once

```python
def append_turn_once(
    self,
    session_id: str,
    turn_id: str,
    messages: Sequence[dict],
) -> bool: ...
```

行为：

1. 校验 session_id / turn_id / messages；
2. 开启写事务（`BEGIN IMMEDIATE` 或等价）；
3. 若 `(session_id, turn_id)` 已存在，返回 `False`；
4. 从 `session_turn_counters` 取 `next_seq`，不存在则初始化为 1；
5. `seq = next_seq`，计数器 +1；
6. 计算 `token_count`；
7. 插入 `session_turns`；
8. 提交。

并发要求：

- seq 分配和插入必须在同一事务；
- 只有 SQLite `locked` / `busy` 做有限重试；
- `(session_id, turn_id)` 唯一冲突按幂等返回 `False`；
- `(session_id, seq)` 唯一冲突直接抛 `HistoryStoreError`，不重试；
- 归档不得重置计数器，seq 不得复用。

### 6.4 load

```python
def load(
    self,
    session_id: str,
    *,
    limit_turns: int | None = None,
) -> HistorySnapshot: ...
```

行为：

1. 读 `session_summaries`，没有则 `RollingSummary()` + coverage 0；
2. 读 `session_turns WHERE seq > covered_through_seq ORDER BY seq ASC`；
3. `limit_turns` 必须是正整数或 None；不为空时先取最新 N 轮，再按
   seq 升序返回；
4. 解码 `messages_json`，构造 `TurnRecord`；解码失败抛
   `HistoryStoreError`，不返回半截数据；
5. 不读 archive。

`ContextEngine` 和 `HistoryCompactor` 必须使用**全量 active turns**。
`limit_turns` 只用于展示 / API 预览，不能用于压缩判断，否则
`active_turns` / `history_tokens` 会失真。

### 6.5 count_turns

```python
def count_turns(self, session_id: str) -> int: ...
```

统计主表全部 turn（包含已覆盖但尚未归档的 turn）。归档阈值用这个数。

### 6.6 save_summary_cas

```python
def save_summary_cas(
    self,
    session_id: str,
    summary: RollingSummary,
    *,
    covered_through_seq: int,
    expected_covered_through_seq: int,
) -> None: ...
```

行为：

1. 在写事务内读当前 `covered_through_seq`；
2. 当前值不等于 `expected_covered_through_seq`：
   抛 `HistoryCoverageConflict`，不写任何内容；
3. `covered_through_seq <= expected_covered_through_seq`：
   抛 `HistoryStoreError`，覆盖只能前进；
4. `covered_through_seq` 必须是主表存在的 turn seq；
5. 推进覆盖时 `summary.text` 不得为空；
6. 插入或更新摘要行，提交。

CAS 失败由 compactor 捕获 `HistoryCoverageConflict`，丢弃本次摘要，不重试。

### 6.7 archive_covered

```python
@dataclass(frozen=True)
class ArchiveResult:
    archived_count: int
    remaining_count: int


def archive_covered(self, session_id: str) -> ArchiveResult: ...
```

行为：

1. 写事务内读 `covered_through_seq`；
2. 若为 0，返回 `ArchiveResult(archived_count=0, remaining_count=count_turns())`；
3. `INSERT OR IGNORE INTO session_turns_archive SELECT ... WHERE seq <= coverage`；
4. `DELETE FROM session_turns WHERE seq <= coverage`；
5. 返回归档数和剩余主表数。

约束：

- 只删除 `seq <= covered_through_seq`；
- 不删除未覆盖 turn；
- 事务失败则主表不变；
- 可重复调用，幂等。

### 6.8 clear

删除该 session 的主表、归档表、摘要、计数器；返回是否删除任何内容。

---

## 7. HistoryCompactor

### 7.1 依赖

```python
class HistoryCompactor:
    def __init__(
        self,
        *,
        store: HistoryStore,
        summarizer: HistorySummarizer,
        limits: ContextLimits,
        counter: TokenCounter = estimate_tokens,
    ): ...
```

内部维护每 session 一个 `asyncio.Lock`。不同 session 可以并行；同 session
压缩和归档必须串行。

### 7.2 指标与决策

```python
@dataclass(frozen=True)
class HistoryMetrics:
    active_turns: int
    history_tokens: int
    last6_tokens: int
    history_utilization: float


class CompactionDecision(str, Enum):
    FLOOR_UNMET = "floor_unmet"
    COMPACT = "compact"
    OVER_TARGET = "over_target"
    NOOP = "noop"
```

```python
def analyze(turns: Sequence[TurnRecord], ceiling: int) -> HistoryMetrics: ...
def decide(metrics: HistoryMetrics, ceiling: int) -> CompactionDecision: ...
```

`HistoryMetrics` / `analyze` / `decide` 放 `history/metrics.py`，engine 和
compactor 共用；不要在两边各写一套触发顺序。

决策顺序必须与 final-strategy 第 5.3 节一致。

```python
@dataclass(frozen=True)
class CompactionResult:
    decision: CompactionDecision
    compacted: bool
    covered_through_seq: int
    metrics: HistoryMetrics
    issues: tuple[ContextIssue, ...]
```

### 7.3 after_turn

```python
async def after_turn(self, session_id: str) -> CompactionResult: ...
```

`input_ceiling` 的计算见 8.3。

流程：

```text
1. 取 per-session lock
2. snapshot = store.load(session_id)
3. metrics = analyze(snapshot.turns, limits.history_ceiling)
4. decision = decide(metrics)
5. FLOOR_UNMET:
       log warning
       不压缩
6. COMPACT:
       target_turns = summarizer.fit_turns(
           snapshot.summary,
           snapshot.turns[:-6],
           input_ceiling,
       )
       如果 target_turns 为空：
           追加 SUMMARY_INPUT_TOO_LONG WARNING issue
           跳过
       try:
           summary = await summarizer.update(snapshot.summary, target_turns)
           target_seq = target_turns[-1].seq
           store.save_summary_cas(
               summary,
               covered_through_seq=target_seq,
               expected_covered_through_seq=snapshot.covered_through_seq,
           )
       except HistoryCoverageConflict:
           追加 SUMMARY_CAS_CONFLICT INFO issue
           不重试
       except SummaryError:
           追加 SUMMARY_GENERATION_FAILED WARNING issue
           不推进 coverage
       except HistoryStoreError:
           追加 SUMMARY_GENERATION_FAILED WARNING issue
           details.stage = "save"
           不推进 coverage
7. OVER_TARGET:
       追加 HISTORY_OVER_TARGET WARNING issue
8. NOOP:
       跳过
9. archive 检查（见 7.5）
```

`after_turn` 不抛软归档失败；只追加 issue。`COMPACT` 失败也不阻塞本轮，
下一轮重新判断。

`after_turn` 已持有 per-session lock；它调用内部的无锁版本
`_ensure_hard_cap_locked()`，不得再次获取同一把锁。

### 7.4 压缩目标

正常：

```text
target_turns = snapshot.turns[:-6]
target_seq   = target_turns[-1].seq
```

如果摘要输入超过窗口：

```text
target_turns = summarizer.fit_turns(
    snapshot.summary,
    snapshot.turns[:-6],
    input_ceiling,
)
target_seq   = target_turns[-1].seq
```

`input_ceiling` 的计算见 8.3。下一轮继续压缩剩余 turn。

如果前缀为空：

- 不压缩；
- 不推进 coverage；
- 记录 warning；
- 下一轮重试；
- 硬上限时按硬上限失败。

### 7.5 归档检查

```text
count = store.count_turns(session_id)

if count >= 600:
    try:
        store.archive_covered(session_id)
    except HistoryStoreError:
        追加 HISTORY_ARCHIVE_FAILED WARNING issue
        # 软归档失败不影响本轮

if count >= 1000:
    _ensure_hard_cap_locked(session_id)
```

### 7.6 ensure_hard_cap

```python
async def ensure_hard_cap(self, session_id: str) -> None: ...
```

公开方法只负责获取 per-session lock，然后调用
`_ensure_hard_cap_locked()`；真正的维护流程在无锁版本里。

流程：

```text
1. count = store.count_turns(session_id)
2. if count < 1000: return
3. 尝试 archive_covered
4. count = store.count_turns(session_id)
5. 如果 count 仍 >= 1000:
       强制压缩（忽略 60%，仍保留最后 6 轮 floor）
       再 archive_covered
       count = store.count_turns(session_id)
6. 如果 count 仍 >= 1000:
       抛 HistoryHardCapExceeded
```

失败映射：

- 最近 6 轮超过 80K：抛 `HistoryFloorUnmet`；
- 摘要失败 / 归档失败 / 仍超限：抛 `HistoryHardCapExceeded`。

`TurnOrchestrator` 负责把这两个异常映射为 `ContextError`：

```text
HistoryFloorUnmet
  -> ContextError(ContextIssue.from_code(HISTORY_FLOOR_UNMET, ..., level=ERROR))

HistoryHardCapExceeded
  -> ContextError(ContextIssue.from_code(HISTORY_HARD_CAP_EXCEEDED, ..., level=ERROR))
```

`begin_turn` 在 `count >= 1000` 时调用一次 `ensure_hard_cap`；失败则抛
`ContextError`。`finish_turn` 已经尝试过一次，`begin_turn` 只再尝试一次，
不做无限重试。

锁约定：

- 公开的 `ensure_hard_cap()` 自己获取 per-session lock；
- `after_turn()` 内部调用 `_ensure_hard_cap_locked()`，因为已经持锁；
- 不要在持锁路径里再次调用公开的 `ensure_hard_cap()`，否则死锁。

---

## 8. LLM 与摘要器

### 8.1 LLMResponse

```python
@dataclass(frozen=True)
class LLMResponse:
    message: Any
    finish_reason: str | None
    usage: dict | None
```

```python
async def chat_with_meta(
    self,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    thinking: bool | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> LLMResponse: ...
```

`LLM.chat(...)` 保留旧签名，内部调用 `chat_with_meta(...).message`。

### 8.2 重试档位

```text
local_max_tokens = [2024, 4096, 8192]
model_max_tokens = int(local_max_tokens * 0.7)
default local    = 2024
```

每次尝试记录：

```text
attempt
local_max_tokens
model_max_tokens
local_truncated
model_truncated
finish_reason
usage
```

判定：

- `local_truncated = count_text(content) > local_max_tokens`；
- `model_truncated = finish_reason == "length"`；
- 两者可以同时为真。

重试：

```text
local_truncated only  -> local 升一档，model 不变
model_truncated       -> local + model 同时升一档
异常 / 空摘要         -> 消耗一次尝试，不升档
```

三次都失败：

- 抛 `SummaryError`；
- compactor 不保存摘要、不推进 coverage；
- 下一轮重试。

### 8.3 输入保护

`HistorySummarizer.fit_turns(previous, turns, input_ceiling)` 是纯函数：

```python
def fit_turns(
    self,
    previous: RollingSummary,
    turns: Sequence[TurnRecord],
    input_ceiling: int,
) -> tuple[TurnRecord, ...]: ...
```

它从最老的 turn 开始累加，返回能放进 `input_ceiling` 的最长连续前缀。
compactor 用这个结果作为 `target_turns`，然后调用：

```python
async def update(
    self,
    previous: RollingSummary,
    turns: Sequence[TurnRecord],
) -> RollingSummary: ...
```

`update` 自己再校验一次：

```text
prompt_tokens <= available_tokens - model_max_tokens
```

超过时抛 `SummaryError`。summarizer 负责拒绝超限请求，不负责选择 turn。

`input_ceiling` 建议按最大输出档位预留：

```text
input_ceiling = available_tokens - max(model_max_tokens)
```

### 8.4 SummaryRenderer

- 存储摘要始终完整；
- 渲染超过 `summary_ceiling` 时按句子前缀截断；
- 句子边界：`。！？.!?\n`；
- 第一句本身放不下时，退化为字符前缀 + `…`；
- header 本身放不下时，返回空文本 + `truncated=True`；
- 不修改数据库摘要。

---

## 9. Issue 与异常

### 9.1 统一 Code

错误和警告共用 3.8 的 `ContextIssueCode`，不再维护单独的
`ContextErrorCode` / `ContextWarningCode`。

级别由 `ContextIssue.level` 决定：

```text
ERROR    -> 阻塞，走 ContextError
WARNING  -> 不阻塞，走 report / compaction result
INFO     -> 记录，不阻塞
```

### 9.2 异常

```python
class ContextError(Exception):
    issue: ContextIssue

    @property
    def code(self) -> ContextIssueCode: ...

    @property
    def message(self) -> str: ...


class HistoryCoverageConflict(HistoryStoreError):
    """save_summary_cas 的 expected coverage 不匹配。"""


class HistoryFloorUnmet(HistoryStoreError):
    """最近 6 轮超过 history_ceiling，压缩无法修复。"""


class HistoryHardCapExceeded(HistoryStoreError):
    """硬上限维护后主表仍 >= 1000。"""
```

`ContextEngine` 只抛 `ContextError`，且其中 `issue.level == ERROR`。
`HistoryStore` 抛 `HistoryStoreError` 及其子类。
`TurnOrchestrator` 负责把 `HistoryFloorUnmet` /
`HistoryHardCapExceeded` 映射为对应的 `ContextError`。

### 9.3 表达方式

| 场景 | 表达 |
|---|---|
| 最近 6 轮 > 80K | `ContextError` + `HISTORY_FLOOR_UNMET` ERROR issue |
| active_turns == 6 且 >=60% | `ContextReport.issues` + `HISTORY_OVER_TARGET` WARNING |
| 软归档失败 | `CompactionResult.issues` + `HISTORY_ARCHIVE_FAILED` WARNING |
| 硬归档失败 | `ContextError` + `HISTORY_HARD_CAP_EXCEEDED` ERROR |
| CAS 冲突 | `CompactionResult.issues` + `SUMMARY_CAS_CONFLICT` INFO |
| 摘要失败 | `CompactionResult.issues` + `SUMMARY_GENERATION_FAILED` WARNING |
| 摘要输入超窗 | `CompactionResult.issues` + `SUMMARY_INPUT_TOO_LONG` WARNING |
| required tool 缺失 | `ContextError` + `TOOLS_REQUIRED_MISSING` ERROR |

`ContextReport.issues` 只放非阻塞 issue；`ContextError.issue` 只放阻塞错误。

---

## 10. 不变量

1. system 内容不随轮次变化。
2. state / summary 各一条 user 消息，标签由组件添加并转义。
3. 一轮 turn 必须完整：一条 user 开始，最终 assistant 结束。
4. `seq` 每 session 唯一、递增、不复用。
5. `covered_through_seq` 只能由 CAS 推进，且必须指向存在的 turn。
6. `load` 只返回 `seq > covered_through_seq` 的 turn，只读主表。
7. 事务 A 只保存 summary + coverage，不删除 turn。
8. 事务 B 只删除 `seq <= covered_through_seq` 的 turn。
9. 历史渲染只选完整 turn，不切 turn / tool 组。
10. 压缩保留最近 6 轮；最近 6 轮超过 80K 时失败。
11. 归档是幂等的；软归档失败不影响本轮，硬归档失败阻塞下一轮。
12. 报告账必须闭合：组件 token 和 = message_tokens + tool_tokens。
13. `HISTORY_OVER_TARGET` 是 WARNING issue，不是异常。
14. 归档表不自动进入 prompt。

---

## 11. 测试清单

### 11.1 消息布局

- system 稳定，包含固定上下文说明；
- state / summary 各一条 user，顺序正确；
- 空 state / summary 被跳过；
- state / summary 内容中的标签被转义；
- 最新 user 在最后；
- tools 不出现在 messages。

### 11.2 预算与报告

- system 超限启动失败；
- user 超限 `USER_INPUT_TOO_LONG`；
- state / summary 截断只影响 prompt；
- history 按完整 turn 裁剪；
- tools 按 `ToolSelector` 选择后再做 token 裁剪；
- `configured_capacity` 恒为 108,512；把 `tools_ceiling` 设为 0 时为 100,512；
- total_tokens / utilization 账闭合；
- `HISTORY_OVER_TARGET` 只进 `ContextReport.issues`，level 为 WARNING；
- `to_log()` 是 JSON 可序列化 dict，不含 schema / description；
- `format()` 返回 Budget / Components / History / Tools / Issues sections；
- `to_display()` 显示 256K 窗口、组件表、Status 和 issue description；
- `to_log()` / `to_display()` 都不出现完整 tool schema。

### 11.3 Turn 存储

- `append_turn_once` 幂等；
- seq 连续、并发不重复；
- 非法 turn 拒绝（首条非 user、多条 user、末尾非 assistant、tool 不配对）；
- `load` 过滤 coverage；
- `limit_turns` 返回最新 N 轮且升序；
- `session_messages` 不存在。

### 11.4 摘要与压缩

- 触发顺序 floor → compact → over target → no-op；
- 20 轮总 90K、最近 6 轮 30K 时会压缩；
- 压缩目标为 `turns[:-6]`；
- 压缩后 active_turns=6；
- CAS 冲突不写入；
- 摘要失败不推进 coverage；
- 档位重试和日志字段；
- 摘要输入超限时只压前缀；
- SummaryRenderer 句子截断 / 字符 fallback。

### 11.5 归档与硬上限

- >=600 归档所有已覆盖 turn；
- 软归档失败追加 `HISTORY_ARCHIVE_FAILED` WARNING issue；
- >=1000 先归档，仍超限则强制压缩；
- 强制压缩后仍 >=1000 抛硬上限错误；
- 最近 6 轮 >80K 时 `HISTORY_FLOOR_UNMET`；
- 下一轮 `begin_turn` 再尝试一次；
- archive 幂等、只删覆盖 turn；
- 旧 schema fail fast。

### 11.6 工具

- 工具结果按 tool_calls 顺序；
- 并发结果用 tool_call_id 建映射；
- 超时 / 异常 / 白名单拒绝补失败消息；
- 成功 / 失败 content 格式；
- 每个 tool 消息都有 tool_call_id；
- `ToolSelector` 候选过滤、白名单、required；
- 按 score 降序取前 N；预算和 max_tools 足够时全部 allowed tools 都发送；
- score=0 的工具预算有余时仍然入选；
- `max_tools` / `tools_ceiling` 裁剪和 `dropped_names`；
- middleware 只执行 exposed_tools；
- 白名单内但未暴露的工具返回 `TOOL_NOT_EXPOSED`；
- 编造工具名、参数非法、非白名单都返回失败 tool 消息；
- 后续 tags / priority / embedding 不改接口，只改打分器。

---

## 12. 实现顺序

与 final-strategy 第 10 节一致，按依赖顺序：

1. 数据模型、issue 模型、`DisplayRow` / `DisplaySection` 和 `history/metrics.py`；
2. `StateSnapshot` 移位；
3. `HistoryStore` 新 schema 和 turn API；
4. `LLMResponse` / `chat_with_meta`；
5. `HistorySummarizer`；
6. `SummaryRenderer`；
7. `ContextEngine` + 组件 + `ToolSelector`；
8. `HistoryCompactor`；
9. `TurnOrchestrator`；
10. 删除旧 Builder/Planner，补测试。

---

## 13. 评审结论与已知边界

评审结论：当前设计没有未决的 P0 接口问题；摘要输入保护、旧 schema fail-fast、
seq 事务化、标签转义、硬上限维护和锁重入都已写入合同。

以下边界当前接受，不在本期修：

- `active_turns < 6` 且超过 60% 不产生 `HISTORY_OVER_TARGET`；
- 连续多条 user 消息依赖 system 说明和标签，真实模型需要回归；
- 工具选择 v1 只做关键词匹配，tags / priority / embedding 后续接入；
- 没有 200 轮保留缓冲，归档恢复要读 archive；
- 归档恢复、检索、长期记忆不在本期。
