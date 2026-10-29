# 第 10 课：上下文预算管理

> 模块三 · 上下文与状态（第 1/11 课）
> 前置：第 9 课（工具参数已经能在执行前校验）
> 下一课：第 11 课把旧历史压成滚动摘要
> 预计用时：2-3 小时
> 本课分支：`dev`

---

## 0. 开工前检查

先确认第 9 课是干净的：

```powershell
git switch dev
git status --short
.venv\Scripts\python.exe -m pytest -q
```

期望：`94 passed`。

第 10 课不新增依赖；token 计数先用可注入的估算器，避免把 `tiktoken` 绑进主流程。

---

## 1. 本课目标

给消息列表加一层 **token 预算**：

```text
messages
   │
   ▼
TokenBudget.count_messages()     统计
   │
   ▼
TokenBudget.fit()                保留 + 裁剪
   ├─ 必保留：system、最新 user
   ├─ 次优先：较新的历史
   └─ 丢弃：最旧的可选消息
   │
   ▼
BudgetResult
   ├─ messages
   ├─ original_tokens / final_tokens
   └─ dropped_indices
```

学完你应该能回答：

1. 为什么 `max_tokens` 不能全部拿来放输入？
2. 为什么 system 和最新 user 必须保护，而旧工具结果可以先丢？
3. 为什么 token 计数要允许注入计数器，而不是写死 `len(text)`？
4. 为什么消息裁剪不能简单按字符截断？

**本课产出**

| 交付物 | 说明 |
|---|---|
| `context/budget.py` | `TokenBudget`、`BudgetResult`、`BudgetExceededError` |
| `context/messages.py` | `validate_message_sequence`、`MessageSequenceError` |
| `tests/unit/test_lesson10_token_budget.py` | 18 个测试 |
| `scripts/lesson10_smoke.py` | 不调用模型的预算冒烟脚本 |
| `docs/lessons/README.md` | 第 10 课标为 ✅ |

**本课明确不做**

- 不做摘要压缩（第 11 课）
- 不把预算接进 `AgentRuntime` / `ContextBuilder`（第 18 课）
- 不做向量检索 / 长期记忆（第 34 课）
- 不做精确 tokenizer 计费（第 33 课）
- 不做完整的按轮次 / 按用户消息分组（第 18 课处理）
- 但本课会把 tool_call 与连续 tool 结果做成原子组，并校验工具序列
- 不做完整 OpenAI 消息 Schema 校验（`content` 类型、图片 part、developer 角色等留给后续）

---

## 2. 问题：消息只会越来越长

到目前为止，一次运行的消息是临时的：

```text
system prompt
user input
assistant tool_call
tool result
...
```

第 11 课之后，历史会跨轮保留，工具结果也会进入上下文。一个 `get_weather` 返回几十字没问题，但：

- 多次工具调用会累积结果；
- 长文档 / 日志会被塞进 tool 消息；
- 模型上下文窗口是有限的；
- 一旦超过窗口，API 直接返回 `context_length_exceeded`，而不是自动截断。

现在没有地方回答这几个问题：

```text
当前消息一共有多少 token？
还能放多少？
超了以后先丢哪条？
system 和最新 user 能不能丢？
丢了哪些消息，怎么观测？
```

第 10 课先把「计数 + 裁剪」做成一个独立、可测试的模块。

---

## 3. 设计决策

### 3.1 不引入精确 tokenizer，先用可注入估算器

| 方案 | 优点 | 缺点 |
|---|---|---|
| `tiktoken` / 厂商 tokenizer | 接近真实 | 多一个依赖，不同模型不同编码 |
| 纯字符数 `len(text)` | 简单 | 中文和英文差异巨大，容易低估 |
| 可注入的估算器 | 无依赖、可测试、可替换 | 只是估算，需要留安全余量 |

**选可注入估算器。** 默认实现：

```python
def estimate_tokens(text: str) -> int:
    cjk = 统计中日韩字符
    other = 其他字符
    return cjk + ceil(other / 4)
```

它不追求精确，只用于预算规划。真正接模型时，应该：

- 留 10%–20% 安全余量；
- 或者在生产环境注入厂商 tokenizer；
- 不要把估算值当计费依据。

测试里注入 `counter=len`，这样预算数字完全可预测。

### 3.2 `max_tokens` 是总窗口，`reserve_output` 是输出预留

```text
可用输入预算 = max_tokens - reserve_output
```

例如：

```text
max_tokens=8192
reserve_output=1024
=> 输入最多 7168
```

如果只按输入算，模型可能生成到一半被截断。输出预留必须在裁剪前扣掉。

### 3.3 保护 system 和最新 user

裁剪算法：

1. 所有 `system` 消息必须保留；
2. 最后一条 `user` 消息必须保留；
3. 其余消息从**最新到最旧**尝试放入预算；
4. 放不下的旧消息丢弃；
5. 保持原始顺序输出。

为什么不是「保留最早的历史」？

- 最新 user 是当前问题，丢了就没法回答；
- system 是行为约束，丢了会改变人格和安全边界；
- 较新的历史比更旧的历史更相关；
- 旧工具结果通常最占空间，也最可牺牲。

### 3.4 消息有固定开销

一条消息不只是 `content`：

```text
role + 分隔符 + content + tool_calls
```

本课用固定 `MESSAGE_OVERHEAD = 4` 近似角色和格式开销，再把 `content` 与 `tool_calls` 的 `name` / `arguments` 计入。

工具调用的 `arguments` 可能很长，不能只数 `content`。

### 3.5 返回报告，而不是静默裁剪

`fit()` 返回：

```python
BudgetResult(
    messages=[...],
    original_tokens=...,
    final_tokens=...,
    dropped_indices=(...),
)
```

这样上层可以：

- 日志记录「本轮裁掉了多少条」；
- 前端提示「历史被压缩」；
- 测试断言具体丢了哪条；
- 第 11 课决定是否对丢弃内容做摘要。

### 3.6 tool_call 和 tool 结果必须成组

按单条消息裁剪有一个严重问题：

```text
assistant(tool_calls=[...])
tool(tool_call_id=...)
```

如果只丢掉 assistant 而保留 tool，会留下孤立的 `tool` 消息；如果只丢掉 tool 而保留 assistant，会留下没有结果的 `tool_calls`。两种都可能被 API 拒绝。

所以本课把「assistant(tool_calls) + 紧随其后的连续 tool 消息」合并成一个**原子组**：

- 组内消息要么全部保留，要么全部丢弃；
- 组的成本 = 组内消息 token 之和；
- 普通消息仍然是单条一组；
- `system` / 最新 user 所在组受保护。

更完整的「按轮次 / 用户消息」分组仍然留给第 18 课 `ContextBuilder`；本课只保证不会产生半个工具调用。

在分组之前，`fit()` 会先调用 `validate_message_sequence()`：

- assistant 的 `tool_calls` 后面必须紧接对应的 `tool` 消息；
- tool 消息不能是孤儿；
- 多个 tool 结果必须按 `tool_calls` 的顺序返回；
- tool 结果之后必须是 assistant（或消息到此结束）。

这样非法输入会明确报 `MessageSequenceError`，而不是被预算逻辑悄悄裁成半截。

---

## 4. 接口与数据结构

### 4.1 `estimate_tokens`

```python
def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 字符按 1，其他字符按约 4 字符 1 token。"""
```

### 4.2 `BudgetResult`

```python
@dataclass(frozen=True)
class BudgetResult:
    messages: list[dict]
    original_tokens: int
    final_tokens: int
    dropped_indices: tuple[int, ...]

    @property
    def truncated(self) -> bool: ...
    @property
    def dropped_count(self) -> int: ...
```

### 4.3 `TokenBudget`

```python
class TokenBudget:
    def __init__(
        self,
        max_tokens: int,
        reserve_output: int = 0,
        counter: Callable[[str], int] = estimate_tokens,
    ): ...

    @property
    def available_tokens(self) -> int: ...

    def count_text(self, text: str) -> int: ...
    def count_message(self, message: dict) -> int: ...
    def count_messages(self, messages: Sequence[dict]) -> int: ...

    def fit(self, messages: Sequence[dict]) -> BudgetResult: ...
```

`fit()` 不修改传入列表，始终返回新的 `messages` 列表。

### 4.4 `BudgetExceededError`

```python
class BudgetExceededError(ValueError):
    """必须保留的 system / 最新 user 已经超过输入预算。"""
```

如果连必保留消息都放不下，不要静默返回空上下文；抛错让上层决定「换更大窗口 / 摘要 / 拒绝请求」。

### 4.5 消息序列校验

```python
class MessageSequenceError(ValueError): ...

def validate_message_sequence(messages: Sequence[dict]) -> None:
    """校验 assistant tool_calls / tool 结果的顺序和配对。"""
```

`TokenBudget.fit()` 在计数前调用它；非法消息序列直接报错，不进入裁剪。

---

## 5. 实现步骤

### 第 1 步：建 `context/` 包

```text
context/
├── __init__.py
└── budget.py
```

`context/__init__.py` 先导出：

```python
from .budget import BudgetExceededError, BudgetResult, TokenBudget, estimate_tokens
from .messages import MessageSequenceError, validate_message_sequence

__all__ = [
    "BudgetExceededError",
    "BudgetResult",
    "MessageSequenceError",
    "TokenBudget",
    "estimate_tokens",
    "validate_message_sequence",
]
```

✅ 完成标志：`from context.budget import TokenBudget` 可用。

### 第 2 步：实现计数

- `estimate_tokens()`：CJK 1 字符 1 token，其他 4 字符 1 token；
- `count_text()`：调用注入的 counter；
- `count_message()`：`MESSAGE_OVERHEAD + content + tool_calls`；
- `count_messages()`：逐条求和。

✅ 完成标志：含 `tool_calls` 的消息比纯 content 消息计得更多。

### 第 3 步：实现消息序列校验

- 新建 `context/messages.py`；
- 检查 role 是否合法；
- 检查 assistant tool_calls 的 id / function.name / arguments；
- 检查后面的连续 tool 消息是否按 id 顺序匹配；
- tool 结果之后必须是 assistant，或者消息结束。

✅ 完成标志：
`test_validate_message_sequence_accepts_tool_group` 和
`test_validate_message_sequence_rejects_orphan_tool` 通过。

### 第 4 步：实现 `fit()`

- 先 `validate_message_sequence()`；
- 再算总 token；
- 未超预算直接返回副本；
- 找出 system 和最后一条 user；
- 必保留消息超预算则抛 `BudgetExceededError`；
- 从后往前尝试加入可选组；
- 保持原顺序，返回报告。

✅ 完成标志：`test_fit_drops_oldest_and_preserves_order` 通过。

### 第 5 步：把 tool 组变成原子组

- 扫描消息，遇到带 `tool_calls` 的 assistant，就把紧随其后的连续 `tool` 消息并入同一组；
- 组的成本 = 组内 token 之和；
- 组放不下就整组丢弃，不能只丢一半；
- 普通消息仍然单条成组。

✅ 完成标志：
`test_fit_keeps_tool_call_and_tool_result_together` 和
`test_fit_does_not_leave_orphan_tool_messages` 通过。

### 第 6 步：处理边界

- 空消息列表；
- 没有 user；
- `reserve_output` 非法；
- 输入列表不被修改；
- 必保留消息超预算。

✅ 完成标志：18 个测试全绿。

### 第 7 步：写冒烟

`scripts/lesson10_smoke.py` 打印：

- 可用输入预算；
- 裁剪前后 token；
- 丢弃下标；
- 超预算错误。

✅ 完成标志：不调用模型也能跑。

---

## 6. 测试代码

> 当前测试是「先红后绿」：实现 `context/budget.py` 之前，会因 `ModuleNotFoundError: No module named 'context'` 失败。

### 6.1 `tests/unit/test_lesson10_token_budget.py`

```python
import pytest

from context.budget import BudgetExceededError, TokenBudget, estimate_tokens
from context.messages import MessageSequenceError, validate_message_sequence


def msg(role: str, content: str, **extra) -> dict:
    return {"role": role, "content": content, **extra}


def test_estimate_tokens_for_empty_cjk_and_ascii():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("hello") == 2


def test_count_message_includes_overhead_and_tool_calls():
    budget = TokenBudget(max_tokens=100, counter=len)

    assert budget.count_message(msg("user", "你好")) == 6

    tool_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city":"成都"}',
                }
            }
        ],
    }
    assert budget.count_message(tool_call) == (
        4 + len("get_weather") + 1 + len('{"city":"成都"}')
    )


def test_fit_keeps_all_when_under_budget():
    budget = TokenBudget(max_tokens=100, counter=len)
    messages = [msg("system", "sys"), msg("user", "hi")]

    result = budget.fit(messages)

    assert result.messages == messages
    assert result.dropped_indices == ()
    assert result.truncated is False
    assert result.original_tokens == result.final_tokens


def test_fit_drops_oldest_and_preserves_order():
    budget = TokenBudget(max_tokens=25, counter=len)
    messages = [
        msg("system", "sys"),
        msg("user", "old"),
        msg("assistant", "answer"),
        msg("user", "new"),
    ]

    result = budget.fit(messages)

    assert [m["role"] for m in result.messages] == ["system", "assistant", "user"]
    assert result.messages[-1]["content"] == "new"
    assert result.dropped_indices == (1,)
    assert result.final_tokens == 24
    assert result.truncated is True


def test_fit_respects_reserved_output():
    budget = TokenBudget(max_tokens=30, reserve_output=10, counter=len)
    messages = [
        msg("system", "sys"),
        msg("user", "old"),
        msg("assistant", "answer"),
    ]

    result = budget.fit(messages)

    assert budget.available_tokens == 20
    assert [m["role"] for m in result.messages] == ["system", "user"]
    assert result.dropped_indices == (2,)


def test_fit_raises_when_protected_messages_exceed_budget():
    budget = TokenBudget(max_tokens=20, counter=len)
    messages = [
        msg("system", "1234567890"),
        msg("user", "1234567890"),
    ]

    with pytest.raises(BudgetExceededError, match="超过"):
        budget.fit(messages)


def test_invalid_budget_configuration():
    with pytest.raises(ValueError):
        TokenBudget(max_tokens=0)
    with pytest.raises(ValueError):
        TokenBudget(max_tokens=10, reserve_output=-1)
    with pytest.raises(ValueError):
        TokenBudget(max_tokens=10, reserve_output=10)


def test_fit_empty_messages():
    budget = TokenBudget(max_tokens=20, counter=len)

    result = budget.fit([])

    assert result.messages == []
    assert result.original_tokens == 0
    assert result.final_tokens == 0
    assert result.dropped_indices == ()
    assert result.truncated is False


def test_fit_does_not_mutate_input():
    budget = TokenBudget(max_tokens=100, counter=len)
    messages = [msg("system", "sys"), msg("user", "hi")]
    before = [dict(message) for message in messages]

    result = budget.fit(messages)

    assert messages == before
    assert result.messages is not messages


def test_fit_without_user_keeps_system_and_newest_optional():
    budget = TokenBudget(max_tokens=15, counter=len)
    messages = [
        msg("system", "sys"),
        msg("assistant", "old"),
        msg("assistant", "new"),
    ]

    result = budget.fit(messages)

    assert [m["role"] for m in result.messages] == ["system", "assistant"]
    assert result.messages[-1]["content"] == "new"
    assert result.dropped_indices == (1,)


def tool_messages() -> list[dict]:
    return [
        msg("system", "sys"),
        msg("user", "old"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "get_weather",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "ok",
        },
        msg("assistant", "done"),
        msg("user", "new"),
    ]


def test_fit_keeps_tool_call_and_tool_result_together():
    budget = TokenBudget(max_tokens=50, counter=len)

    result = budget.fit(tool_messages())

    assert [m["role"] for m in result.messages] == [
        "system",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert result.dropped_indices == (1,)
    assert result.final_tokens == 46


def test_fit_does_not_leave_orphan_tool_messages():
    budget = TokenBudget(max_tokens=40, counter=len)

    result = budget.fit(tool_messages())

    roles = [m["role"] for m in result.messages]
    assert "tool" not in roles
    assert not any(m.get("tool_calls") for m in result.messages)
    assert result.dropped_indices == (2, 3)


def test_validate_message_sequence_accepts_tool_group():
    validate_message_sequence(tool_messages())


def test_validate_message_sequence_rejects_orphan_tool():
    messages = [
        msg("system", "sys"),
        msg("user", "hi"),
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]

    with pytest.raises(MessageSequenceError, match="没有 assistant"):
        validate_message_sequence(messages)


def test_validate_message_sequence_rejects_missing_tool_result():
    messages = [
        msg("system", "sys"),
        msg("user", "hi"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        msg("user", "new"),
    ]

    with pytest.raises(MessageSequenceError, match="不匹配"):
        validate_message_sequence(messages)


def test_validate_message_sequence_rejects_wrong_tool_order():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "get_weather", "arguments": "{}"},
                },
                {
                    "id": "call_2",
                    "function": {"name": "get_current_time", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "time"},
        {"role": "tool", "tool_call_id": "call_1", "content": "weather"},
        msg("assistant", "done"),
    ]

    with pytest.raises(MessageSequenceError, match="不匹配"):
        validate_message_sequence(messages)


def test_validate_message_sequence_requires_assistant_after_tools():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        msg("user", "new"),
    ]

    with pytest.raises(MessageSequenceError, match="role 必须是 assistant"):
        validate_message_sequence(messages)


def test_fit_rejects_invalid_message_sequence():
    budget = TokenBudget(max_tokens=100, counter=len)
    messages = [
        msg("system", "sys"),
        msg("user", "hi"),
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]

    with pytest.raises(MessageSequenceError):
        budget.fit(messages)
```

### 6.2 这 18 个测试分别防什么

| 测试 | 证明 |
|---|---|
| `test_estimate_tokens_for_empty_cjk_and_ascii` | 估算器对空串、中文、英文有明确行为 |
| `test_count_message_includes_overhead_and_tool_calls` | 消息开销和 tool_calls 参数被计入 |
| `test_fit_keeps_all_when_under_budget` | 未超预算时不乱裁 |
| `test_fit_drops_oldest_and_preserves_order` | 旧消息被丢弃，输出顺序不变 |
| `test_fit_respects_reserved_output` | 输出预留真的从输入预算里扣掉 |
| `test_fit_raises_when_protected_messages_exceed_budget` | 必保留消息超限时明确报错 |
| `test_invalid_budget_configuration` | 非法预算配置在构造期失败 |
| `test_fit_empty_messages` | 空输入不会崩 |
| `test_fit_does_not_mutate_input` | 裁剪不修改调用方列表 |
| `test_fit_without_user_keeps_system_and_newest_optional` | 没有 user 时仍保护 system，并保留最新可选消息 |
| `test_fit_keeps_tool_call_and_tool_result_together` | 工具调用和结果能整组保留 |
| `test_fit_does_not_leave_orphan_tool_messages` | 预算不够时整组丢弃，不产生孤儿 tool 消息 |
| `test_validate_message_sequence_accepts_tool_group` | 合法工具组不会被误报 |
| `test_validate_message_sequence_rejects_orphan_tool` | 没有 assistant 的 tool 消息被拒绝 |
| `test_validate_message_sequence_rejects_missing_tool_result` | 缺少 tool 结果时被拒绝 |
| `test_validate_message_sequence_rejects_wrong_tool_order` | 多个 tool 结果顺序错误被拒绝 |
| `test_validate_message_sequence_requires_assistant_after_tools` | tool 结果后必须是 assistant |
| `test_fit_rejects_invalid_message_sequence` | `fit()` 入口会调用校验，不会静默裁剪非法序列 |

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson10_token_budget.py -q
```

期望 `18 passed`；全量期望 `112 passed`。

---

## 7. 冒烟脚本

`scripts/lesson10_smoke.py`：

```python
from context.budget import BudgetExceededError, TokenBudget


def main() -> None:
    messages = [
        {"role": "system", "content": "你是葡萄酒酿酒大师。"},
        {"role": "user", "content": "旧问题：今天适合酿酒吗？"},
        {"role": "assistant", "content": "旧回答：适合。"},
        {"role": "user", "content": "新问题：帮我推荐一款干型酒。"},
    ]

    budget = TokenBudget(max_tokens=40, reserve_output=8)
    print("可用输入预算:", budget.available_tokens)
    print("裁剪前 token:", budget.count_messages(messages))

    result = budget.fit(messages)
    print("裁剪后 token:", result.final_tokens)
    print("丢弃下标:", result.dropped_indices)
    for message in result.messages:
        print(f"- {message['role']}: {message['content']}")

    try:
        TokenBudget(max_tokens=10, reserve_output=0).fit(
            [
                {"role": "system", "content": "1234567890"},
                {"role": "user", "content": "1234567890"},
            ]
        )
    except BudgetExceededError as exc:
        print("超预算:", exc)


if __name__ == "__main__":
    main()
```

运行：

```powershell
.venv\Scripts\python.exe -m scripts.lesson10_smoke
```

不需要 API key。

---

## 8. 参考实现

### 8.1 `context/budget.py`

```python
"""上下文 token 预算：计数、保护关键消息、从旧到新裁剪。

本模块只做预算规划，不调用模型，也不做摘要。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from context.messages import validate_message_sequence

TokenCounter = Callable[[str], int]

MESSAGE_OVERHEAD = 4


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 字符按 1，其他字符按约 4 字符 1 token。

    这不是精确 tokenizer，只用于预算规划。生产环境应留安全余量，
    或注入厂商 tokenizer。
    """
    if not text:
        return 0

    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - cjk
    return cjk + (other + 3) // 4


class BudgetExceededError(ValueError):
    """必须保留的 system / 最新 user 已经超过输入预算。"""


@dataclass(frozen=True)
class BudgetResult:
    messages: list[dict]
    original_tokens: int
    final_tokens: int
    dropped_indices: tuple[int, ...]

    @property
    def truncated(self) -> bool:
        return bool(self.dropped_indices)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped_indices)


class TokenBudget:
    def __init__(
        self,
        max_tokens: int,
        reserve_output: int = 0,
        counter: TokenCounter = estimate_tokens,
    ):
        if max_tokens <= 0:
            raise ValueError("max_tokens 必须大于 0")
        if reserve_output < 0:
            raise ValueError("reserve_output 不能为负数")
        if reserve_output >= max_tokens:
            raise ValueError("reserve_output 必须小于 max_tokens")

        self.max_tokens = max_tokens
        self.reserve_output = reserve_output
        self.counter = counter

    @property
    def available_tokens(self) -> int:
        return self.max_tokens - self.reserve_output

    def count_text(self, text: str) -> int:
        return self.counter(text or "")

    def count_message(self, message: dict) -> int:
        return MESSAGE_OVERHEAD + self.count_text(self._message_text(message))

    def count_messages(self, messages: Sequence[dict]) -> int:
        return sum(self.count_message(message) for message in messages)

    def fit(self, messages: Sequence[dict]) -> BudgetResult:
        """在预算内保留尽量新的消息，system 和最新 user 必须保留。

        带 tool_calls 的 assistant 消息与其后连续的 tool 结果视为一个原子组：
        要么整组保留，要么整组丢弃，避免产生孤立的 tool_call / tool 消息。
        """
        messages = list(messages)
        validate_message_sequence(messages)
        original_tokens = self.count_messages(messages)

        if original_tokens <= self.available_tokens:
            return BudgetResult(messages, original_tokens, original_tokens, ())

        groups = self._message_groups(messages)
        protected = self._protected_indices(messages)
        selected: set[int] = set()
        used = 0

        for group in groups:
            if any(index in protected for index in group):
                selected.update(group)
                used += sum(self.count_message(messages[index]) for index in group)

        if used > self.available_tokens:
            raise BudgetExceededError(
                f"必须保留的 system/最新 user 已需要 {used} tokens，"
                f"超过可用输入预算 {self.available_tokens}"
            )

        for group in reversed(groups):
            if any(index in selected for index in group):
                continue
            cost = sum(self.count_message(messages[index]) for index in group)
            if used + cost <= self.available_tokens:
                selected.update(group)
                used += cost

        final_messages = [
            message
            for index, message in enumerate(messages)
            if index in selected
        ]
        dropped_indices = tuple(
            index
            for index in range(len(messages))
            if index not in selected
        )
        return BudgetResult(
            messages=final_messages,
            original_tokens=original_tokens,
            final_tokens=used,
            dropped_indices=dropped_indices,
        )

    @staticmethod
    def _message_groups(messages: Sequence[dict]) -> list[list[int]]:
        """把 assistant(tool_calls) 与其后连续的 tool 消息合并成原子组。"""
        groups: list[list[int]] = []
        index = 0

        while index < len(messages):
            message = messages[index]
            if message.get("role") == "assistant" and message.get("tool_calls"):
                group = [index]
                cursor = index + 1
                while cursor < len(messages) and messages[cursor].get("role") == "tool":
                    group.append(cursor)
                    cursor += 1
                groups.append(group)
                index = cursor
            else:
                groups.append([index])
                index += 1

        return groups

    @staticmethod
    def _message_text(message: dict) -> str:
        parts: list[str] = []
        content = message.get("content")
        if content is not None:
            parts.append(str(content))

        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            parts.append(str(function.get("name") or ""))
            parts.append(str(function.get("arguments") or ""))

        return "\n".join(parts)

    @staticmethod
    def _protected_indices(messages: Sequence[dict]) -> set[int]:
        protected = {
            index
            for index, message in enumerate(messages)
            if message.get("role") == "system"
        }
        user_indices = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "user"
        ]
        if user_indices:
            protected.add(user_indices[-1])
        return protected
```

### 8.2 `context/messages.py`

```python
"""校验消息序列是否满足 OpenAI Chat Completions 的工具调用顺序。

这里只校验与工具调用相关的结构不变量：
- assistant 的 tool_calls 后面必须紧接对应的 tool 消息；
- tool 消息不能出现没有 assistant 的孤儿；
- tool 结果之后必须回到 assistant（或结束）；
- 多个 tool 结果必须按 tool_calls 的顺序返回。
"""

from __future__ import annotations

from typing import Sequence

ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})


class MessageSequenceError(ValueError):
    """消息序列不满足 OpenAI 工具调用协议。"""


def validate_message_sequence(messages: Sequence[dict]) -> None:
    """校验消息序列，失败时抛出 ``MessageSequenceError``。"""
    index = 0

    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict):
            raise MessageSequenceError(f"messages[{index}] 必须是 dict")

        role = message.get("role")
        if role not in ALLOWED_ROLES:
            raise MessageSequenceError(
                f"messages[{index}] 的 role 不合法: {role!r}"
            )

        if role == "tool":
            raise MessageSequenceError(
                f"messages[{index}] 是 tool，但前面没有 assistant tool_calls"
            )

        tool_calls = message.get("tool_calls")
        if role != "assistant" or not tool_calls:
            index += 1
            continue

        if not isinstance(tool_calls, list):
            raise MessageSequenceError(
                f"messages[{index}].tool_calls 必须是 list"
            )

        expected_ids: list[str] = []
        for call_index, call in enumerate(tool_calls):
            if not isinstance(call, dict):
                raise MessageSequenceError(
                    f"messages[{index}].tool_calls[{call_index}] 必须是 dict"
                )

            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id:
                raise MessageSequenceError(
                    f"messages[{index}].tool_calls[{call_index}] 缺少合法 id"
                )

            function = call.get("function")
            if not isinstance(function, dict):
                raise MessageSequenceError(
                    f"messages[{index}].tool_calls[{call_index}] 缺少 function"
                )
            if not isinstance(function.get("name"), str) or not function["name"]:
                raise MessageSequenceError(
                    f"messages[{index}].tool_calls[{call_index}] 缺少 function.name"
                )
            if not isinstance(function.get("arguments"), str):
                raise MessageSequenceError(
                    f"messages[{index}].tool_calls[{call_index}] "
                    f"的 function.arguments 必须是 JSON 字符串"
                )

            expected_ids.append(call_id)

        if len(expected_ids) != len(set(expected_ids)):
            raise MessageSequenceError(
                f"messages[{index}].tool_calls 存在重复 id"
            )

        cursor = index + 1
        actual_ids: list[str] = []
        while (
            cursor < len(messages)
            and isinstance(messages[cursor], dict)
            and messages[cursor].get("role") == "tool"
        ):
            tool_call_id = messages[cursor].get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise MessageSequenceError(
                    f"messages[{cursor}] 的 tool_call_id 不合法"
                )
            actual_ids.append(tool_call_id)
            cursor += 1

        if actual_ids != expected_ids:
            raise MessageSequenceError(
                f"messages[{index}] 的 tool_calls 与 tool 结果不匹配："
                f"期望 {expected_ids}，实际 {actual_ids}"
            )

        if cursor < len(messages):
            next_message = messages[cursor]
            if (
                not isinstance(next_message, dict)
                or next_message.get("role") != "assistant"
            ):
                raise MessageSequenceError(
                    f"messages[{cursor}] 紧跟在 tool 结果后，role 必须是 assistant"
                )

        index = cursor
```

### 8.3 `context/__init__.py`

```python
from .budget import BudgetExceededError, BudgetResult, TokenBudget, estimate_tokens
from .messages import MessageSequenceError, validate_message_sequence

__all__ = [
    "BudgetExceededError",
    "BudgetResult",
    "MessageSequenceError",
    "TokenBudget",
    "estimate_tokens",
    "validate_message_sequence",
]
```

---

## 9. 验收清单（DoD）

- [x] `context/budget.py` 存在，计数和裁剪不依赖网络
- [x] `estimate_tokens` 对空串、中文、英文有明确结果
- [x] `count_message` 会计入 `tool_calls` 的 name / arguments
- [x] `max_tokens` 扣掉 `reserve_output` 后再做输入预算
- [x] system 和最新 user 永远保留
- [x] 超预算时从旧到新丢弃，输出顺序不变
- [x] `assistant(tool_calls)` 与连续 `tool` 结果作为原子组，不会只保留一半
- [x] `validate_message_sequence` 能拒绝孤儿 tool、缺结果、错序、tool 后非 assistant
- [x] 必保留消息超预算时抛 `BudgetExceededError`
- [x] `fit()` 不修改输入列表
- [x] `pytest tests/unit/test_lesson10_token_budget.py -q` → 18 passed
- [x] 全量 `pytest -q` → 112 passed
- [x] 冒烟脚本不调用模型

---

## 10. 常见坑

| 坑 | 现象 | 处理 |
|---|---|---|
| 把 `max_tokens` 全给输入 | 输出被截断 | 先扣 `reserve_output` |
| 用 `len(text)` 当 token | 中文严重低估 | 用估算器或厂商 tokenizer |
| 只数 `content` | 长 `tool_calls` 参数漏算 | 把 name / arguments 计入 |
| 先丢 system | 人格和安全约束丢失 | system 设为必保留 |
| 先丢最新 user | 模型不知道当前问题 | 最后一条 user 必保留 |
| 必保留消息也超预算 | 返回空上下文或静默失败 | 抛 `BudgetExceededError` |
| 裁剪后改变顺序 | 对话因果错乱 | 按原索引输出 |
| 直接改传入列表 | 调用方状态被污染 | 先 `list(messages)` |
| 只按单条消息裁剪 | 留下 orphan tool_call / tool | `_message_groups()` + 组成本 |
| 估算器当计费 | 成本和账单对不上 | 第 33 课接精确统计 |

---

## 11. 扩展与思考题

1. **安全余量**：预算只用到 85%，剩下 15% 作为 tokenizer 误差和输出波动缓冲，是否更稳？
2. **摘要接入**：被丢弃的旧消息是直接扔，还是先压成一条 `system` 摘要？（第 11 课）
3. **工具结果优先级**：最新工具结果比旧 user 消息更重要吗？如何定义优先级？
4. **完整按轮次裁剪**：工具组已经原子化；下一步能把「user → assistant → tool」整轮也当成组吗？
5. **多模态**：`content` 是列表（文本 + 图片）时怎么计数？
6. **缓存**：相同消息前缀的 token 数能否缓存？对 prompt caching 有什么影响？

---

## 12. 与第 11 课的衔接

第 10 课解决「装不下就先丢旧的」，但直接丢会损失信息：

```text
旧对话：用户说他对甜型酒感兴趣
新对话：推荐一款酒
```

如果旧对话被整条丢掉，模型就不知道用户偏好。

第 11 课会做**滚动摘要**：

- 把被裁掉的历史压成一段摘要；
- 摘要本身也受 token 预算限制；
- 保留关键事实：偏好、目标、约束、已确认结论；
- 摘要放在 system 或单独的消息里，再交给 `TokenBudget` 计数。

一句话：

> **第 10 课管「装得下」，第 11 课管「丢了也别丢关键信息」。**
