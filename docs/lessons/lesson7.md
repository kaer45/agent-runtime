# 第 7 课：让 Agent 区分普通对话与任务规划

> 模块二 · 模型规划接入（第 3/5 课）
> 前置：第 6 课（`PlanRunner` 已能端到端执行任务）
> 下一课：第 8 课让规划器理解工具的真实能力
> 预计用时：2-3 小时
> 本课分支：`dev`

---

## 0. 开工前检查

先完成第 6 课的单测，再开始本课：

```powershell
git switch dev
git status --short
.venv\Scripts\python.exe -m pytest -q
```

期望：`28 passed`（21 + 第 6 课的 7 个）。

如果还没写第 6 课测试，先回到 [lesson6.md](lesson6.md) 第 6.1 节把测试补上；否则本课新增的 10 个测试会让总数对不上。

---

## 1. 本课目标

给 `AgentRuntime` 加一层**意图路由**：

```text
用户输入
  → IntentRouter.route()
      ├─ CHAT → ChatResponder → final_answer
      └─ TASK → PlanRunner   → 第 6 课的完整链路
```

学完你应该能回答：

1. 为什么「曹操的儿子叫什么」不应该进入 `TaskPlanner`？
2. 为什么分类器用「强制 tool call」而不是让模型在文本里输出 `chat` / `task`？
3. 分类失败时为什么默认走 chat，而不是 task？

**本课产出**

| 交付物 | 说明 |
|---|---|
| `agent/intent.py` | `Intent`、`IntentRouter`、`classify_intent` 工具 |
| `agent/chat_responder.py` | 普通对话路径，不规划、不调用工具 |
| `agent/runtime.py` | 根据意图选择 runner |
| `agent/__init__.py` | 导出更新 |
| `tests/unit/test_lesson07_routing.py` | 10 个测试 |
| `scripts/lesson07_smoke.py` | 聊天/任务两条路径的冒烟脚本 |

**本课明确不做**

- 不做多轮上下文判断（第 18 课）
- 不做「任务 + 澄清」多意图（后续扩展）
- 不做意图事件推送（可选扩展）
- 不改 `PlanRunner` / `TaskPlanner` / 调度器

---

## 2. 问题：所有输入都被当成任务

你之前跑过：

```text
python -m scripts.lesson06_smoke "曹操的儿子叫什么"
```

得到：

```text
plan_ready {'task_count': 1, 'tasks': [{'id': 'T1', 'tool': 'get_current_time'}]}
...
final_answer 根据提供的任务执行结果，只有当前时间信息，无法得知曹操儿子的名字
```

这不是模型「不知道曹操的儿子」，而是三层约束叠加：

1. `TaskPlanner` 的 system prompt 说「你的唯一职责是提交任务计划」，它没有直接回答的出口；
2. `submit_task_plan` 的 `tasks` 描述写「至少一个任务」，模型不敢交空计划；
3. 工具列表里只有 `get_current_time` 不需要参数，于是它挑了成本最低的工具来满足 schema。

回答阶段又被提示「请基于以上结果回答，不要编造未出现的信息」，所以它只能诚实地说不知道。

**任务计划协议没有错**，它只是假设调用方已经判断「这是任务」。第 6 课把这个前提跳过了，第 7 课补上。

---

## 3. 设计决策

### 3.1 三种路由方案

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A. 关键词规则 | 含「天气/酿酒/查」走 task，问候走 chat | 零成本、快 | 换个说法就失效 |
| B. LLM 分类器 | 单独一次轻量调用，输出 `chat` / `task` | 能理解语义、可测试 | 多一次调用、多几百毫秒 |
| C. 合并到一次调用 | 让模型自己选 `answer` 或 `submit_task_plan` | 只调一次 | 路由和规划耦合，失败难排查 |

**本课选 B**：独立分类器，职责单一，后面第 8 课改工具能力时不会牵扯到路由。

### 3.2 分类器也用「强制 tool call」

和 Planner 一样，分类器只暴露一个工具：

```json
{
  "name": "classify_intent",
  "parameters": {
    "type": "object",
    "properties": {
      "intent": {"type": "string", "enum": ["chat", "task"]},
      "reason": {"type": "string"}
    },
    "required": ["intent"]
  }
}
```

好处：

- 输出是结构化的 enum，不用从文本里正则提取；
- 和 Planner 用同一套 `tool_choice` / `thinking=False` 经验；
- 测试可以用 `FakeLLM` 直接返回 tool call。

⚠️ DeepSeek 的 thinking 模式不支持 named tool_choice，所以分类请求也要 `thinking=False`。

### 3.3 分类失败默认走 chat

分类器可能遇到：

- API 报错；
- 模型没调用工具；
- `arguments` 不是合法 JSON；
- `intent` 不是 `chat` / `task`。

这些情况统一回退到 **CHAT**。理由：误走 chat 最多是回答得不好；误走 task 可能调用工具、产生副作用、浪费时间。安全默认值优先。

### 3.4 ChatResponder 的边界

`ChatResponder` 只做三件事：

1. 用 system + user 两条消息调 `llm.chat`；
2. **不传 tools**；
3. 发 `run_started → final_answer → run_finished`。

它不复用 `PlanRunner` 的「只基于工具结果」提示词，因为聊天需要允许模型使用常识。任务路径仍然用原来的 `TASK_SYSTEM_PROMPT`。

---

## 4. 接口与事件

### 4.1 `Intent`

```python
class Intent(str, Enum):
    CHAT = "chat"
    TASK = "task"
```

### 4.2 `IntentRouter`

```python
class IntentRouter:
    def __init__(self, llm, registry): ...

    async def route(self, user_input: str) -> Intent:
        """返回 chat 或 task；分类失败时回退到 chat。"""
```

### 4.3 `ChatResponder`

```python
class ChatResponder:
    def __init__(self, llm, system_prompt): ...

    async def run(self, user_input: str, run_id: str | None = None):
        """run_started → final_answer → run_finished。"""
```

### 4.4 `AgentRuntime`

```python
class AgentRuntime:
    def __init__(self, llm: LLM | None = None): ...

    async def run(self, user_input: str):
        intent = await self.router.route(user_input)
        runner = self.task_runner if intent == Intent.TASK else self.chat_responder
        async for event in runner.run(user_input):
            yield event
```

`llm` 参数只为测试注入；生产环境不传。

### 4.5 事件序列

```text
CHAT:
  run_started
  final_answer
  run_finished

TASK:
  run_started
  plan_started
  plan_ready
  task_started / task_success / ...
  final_answer
  run_finished
```

本课不新增事件类型。前端现有代码已经能处理这两条路径。

---

## 5. 实现步骤

### 第 1 步：建 `agent/intent.py`

- `Intent` 枚举；
- `INTENT_TOOL`（`classify_intent`）；
- `INTENT_PROMPT` + `INTENT_RULES`（含「曹操的儿子」示例）；
- `IntentRouter.route()`。

✅ 完成标志：`route()` 能返回 `CHAT` / `TASK`，失败时返回 `CHAT`。

### 第 2 步：分类 prompt 的三条原则

1. **不确定选 chat**；
2. **工具列表里有某个工具，不代表无关问题要判 task**；
3. 只通过 `classify_intent` 输出。

示例必须覆盖：

- `你好` → chat
- `曹操的儿子叫什么` → chat
- `1+1 等于几` → chat
- `今天成都适合酿酒吗` → task
- `帮我查一下成都天气` → task

✅ 完成标志：`IntentRouter._system_prompt()` 里能看到工具名和这些示例。

### 第 3 步：建 `agent/chat_responder.py`

- 构造参数：`llm`、`system_prompt`；
- `run()` 发 `run_started`；
- `llm.chat` **不传 tools**；
- 成功发 `final_answer` + `run_finished`；
- 异常发 `run_finished(error)`。

✅ 完成标志：事件流只有三条，且 `llm.calls[0]["tools"] is None`。

### 第 4 步：改 `AgentRuntime`

- 增加 `llm: LLM | None = None` 参数，方便测试注入；
- 组装 `self.router`、`self.task_runner`、`self.chat_responder`；
- `run()` 先路由，再转发选中 runner 的事件。

✅ 完成标志：`AgentRuntime(llm=FakeLLM([...]))` 可以在测试里跑起来，不访问网络。

### 第 5 步：区分两个 system prompt

- `TASK_SYSTEM_PROMPT`：任务回答，强调「不要编造工具结果」；
- `CHAT_SYSTEM_PROMPT`：聊天回答，允许常识，强调「不要编造工具执行结果」。

不要把任务提示词复用到聊天路径，否则「曹操的儿子」仍然会被限制成「只能基于工具结果」。

### 第 6 步：写测试

按 6.1 的 10 个用例实现，先跑路由单测，再跑 runtime 分流测试。

✅ 完成标志：`10 passed`，全量 `38 passed`。

### 第 7 步：冒烟

按 7 的脚本分别跑：

```powershell
.venv\Scripts\python.exe -m scripts.lesson07_smoke "曹操的儿子叫什么"
.venv\Scripts\python.exe -m scripts.lesson07_smoke "今天成都适合酿酒吗"
```

第一条不应出现 `plan_*`，第二条应出现完整任务事件。

### 第 8 步：前端检查

不需要改前端。聊天路径只有 `run_started/final_answer/run_finished`，现有 `handleEvent` 已经能显示。

如果你之前在 `run_finished` 里把状态写成「连接结束」，可以顺手改成「执行完成」，但这不是本课重点。

---

## 6. 测试代码

### 6.1 `tests/unit/test_lesson07_routing.py`

```python
import asyncio
import json

import pytest

from agent.chat_responder import ChatResponder
from agent.intent import INTENT_TOOL, Intent, IntentRouter
from agent.runtime import AgentRuntime
from events.models import EventType, RuntimeEvent
from tests.fixtures.fake_llm import FakeLLM, FakeMessage, FakeToolCall
from tools.builtin import brewing, get_current_time, get_weather
from tools.registry import ToolRegistry


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.register(get_weather)
    r.register(get_current_time)
    r.register(brewing)
    return r


def classify(intent: str, call_id: str = "call_1") -> FakeToolCall:
    arguments = json.dumps({"intent": intent, "reason": "test"}, ensure_ascii=False)
    return FakeToolCall(name="classify_intent", arguments=arguments, call_id=call_id)


async def collect(events):
    return [event async for event in events]


def test_router_chat(registry):
    llm = FakeLLM([FakeMessage(tool_calls=[classify("chat")])])
    router = IntentRouter(llm, registry)

    intent = asyncio.run(router.route("曹操的儿子叫什么"))

    assert intent is Intent.CHAT
    call = llm.calls[0]
    assert call["tools"] == [INTENT_TOOL]
    assert call["tool_choice"] == {
        "type": "function",
        "function": {"name": "classify_intent"},
    }
    assert call["thinking"] is False
    prompt = call["messages"][0]["content"]
    assert "曹操" in prompt
    assert "get_weather" in prompt


def test_router_task(registry):
    llm = FakeLLM([FakeMessage(tool_calls=[classify("task")])])
    router = IntentRouter(llm, registry)

    intent = asyncio.run(router.route("今天成都适合酿酒吗"))

    assert intent is Intent.TASK


def test_router_malformed_json_falls_back_to_chat(registry):
    llm = FakeLLM([
        FakeMessage(tool_calls=[FakeToolCall(name="classify_intent", arguments="{not-json")])
    ])
    router = IntentRouter(llm, registry)

    assert asyncio.run(router.route("随便说点什么")) is Intent.CHAT


def test_router_missing_tool_call_falls_back_to_chat(registry):
    llm = FakeLLM([FakeMessage(content="这是一段普通回答")])
    router = IntentRouter(llm, registry)

    assert asyncio.run(router.route("你好")) is Intent.CHAT


def test_router_invalid_intent_falls_back_to_chat(registry):
    llm = FakeLLM([
        FakeMessage(tool_calls=[FakeToolCall(
            name="classify_intent",
            arguments='{"intent": "weather"}',
        )])
    ])
    router = IntentRouter(llm, registry)

    assert asyncio.run(router.route("今天天气怎么样")) is Intent.CHAT


def test_router_llm_error_falls_back_to_chat(registry):
    llm = FakeLLM([RuntimeError("boom")])
    router = IntentRouter(llm, registry)

    assert asyncio.run(router.route("你好")) is Intent.CHAT


def test_chat_responder_emits_answer():
    llm = FakeLLM([FakeMessage(content="曹操的儿子有曹昂、曹丕、曹植等。")])
    responder = ChatResponder(llm, "系统提示")

    events = asyncio.run(collect(responder.run("曹操的儿子叫什么", run_id="run-chat")))

    assert [e.type for e in events] == [
        EventType.RUN_STARTED,
        EventType.FINAL_ANSWER,
        EventType.RUN_FINISHED,
    ]
    assert {e.run_id for e in events} == {"run-chat"}
    assert "曹昂" in events[1].data["content"]
    assert llm.calls[0]["tools"] is None


def test_chat_responder_error_ends_run():
    llm = FakeLLM([RuntimeError("boom")])
    responder = ChatResponder(llm, "系统提示")

    events = asyncio.run(collect(responder.run("你好", run_id="run-chat")))

    assert [e.type for e in events] == [
        EventType.RUN_STARTED,
        EventType.RUN_FINISHED,
    ]
    assert "boom" in events[-1].data["error"]


def test_runtime_routes_chat():
    llm = FakeLLM([
        FakeMessage(tool_calls=[classify("chat")]),
        FakeMessage(content="曹操的儿子有曹昂、曹丕、曹植等。"),
    ])
    runtime = AgentRuntime(llm=llm)

    events = asyncio.run(collect(runtime.run("曹操的儿子叫什么")))

    assert [e.type for e in events] == [
        EventType.RUN_STARTED,
        EventType.FINAL_ANSWER,
        EventType.RUN_FINISHED,
    ]
    assert "曹昂" in events[1].data["content"]
    assert len(llm.calls) == 2
    assert llm.calls[0]["tools"] == [INTENT_TOOL]
    assert llm.calls[1]["tools"] is None


class FakeRunner:
    def __init__(self, content="fake answer"):
        self.content = content
        self.calls = []

    async def run(self, user_input, run_id=None):
        self.calls.append(user_input)
        yield RuntimeEvent(EventType.RUN_STARTED, run_id or "fake")
        yield RuntimeEvent(
            EventType.FINAL_ANSWER,
            run_id or "fake",
            data={"content": self.content},
        )
        yield RuntimeEvent(EventType.RUN_FINISHED, run_id or "fake")


def test_runtime_routes_task():
    llm = FakeLLM([FakeMessage(tool_calls=[classify("task")])])
    runtime = AgentRuntime(llm=llm)
    task_runner = FakeRunner("任务回答")
    chat_runner = FakeRunner("聊天回答")
    runtime.task_runner = task_runner
    runtime.chat_responder = chat_runner

    events = asyncio.run(collect(runtime.run("今天成都适合酿酒吗")))

    assert task_runner.calls == ["今天成都适合酿酒吗"]
    assert chat_runner.calls == []
    assert events[1].data["content"] == "任务回答"
    assert len(llm.calls) == 1
```

### 6.2 这 10 个测试分别防什么

| 测试 | 证明 |
|---|---|
| `test_router_chat` | 常识问题判 chat；prompt 带工具和示例；强制 tool_choice / thinking=False |
| `test_router_task` | 实时/工具问题判 task |
| `test_router_malformed_json_falls_back_to_chat` | 非法 JSON 不会抛到上层 |
| `test_router_missing_tool_call_falls_back_to_chat` | 模型没调工具时安全回退 |
| `test_router_invalid_intent_falls_back_to_chat` | 非法 enum 回退 |
| `test_router_llm_error_falls_back_to_chat` | API 异常回退 |
| `test_chat_responder_emits_answer` | 聊天事件顺序、run_id、不传 tools |
| `test_chat_responder_error_ends_run` | 聊天异常能收尾 |
| `test_runtime_routes_chat` | 真实 router + ChatResponder 端到端 |
| `test_runtime_routes_task` | 路由到 task runner，且不调用 chat |

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson07_routing.py -q
```

期望 `10 passed`；全量期望 `38 passed`（28 + 10）。

---

## 7. 冒烟脚本

`scripts/lesson07_smoke.py`：

```python
import asyncio
import sys

from agent.runtime import AgentRuntime


async def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "曹操的儿子叫什么"
    runtime = AgentRuntime()

    async for event in runtime.run(question):
        print(event.type.value, event.task_id or "", event.data)


if __name__ == "__main__":
    asyncio.run(main())
```

运行：

```powershell
.venv\Scripts\python.exe -m scripts.lesson07_smoke "曹操的儿子叫什么"
```

期望：

```text
run_started
final_answer 曹操的儿子有曹昂、曹丕、曹植……
run_finished
```

没有 `plan_started`、`plan_ready`、`task_*`。

再跑任务：

```powershell
.venv\Scripts\python.exe -m scripts.lesson07_smoke "今天成都适合酿酒吗"
```

期望出现 `plan_started → plan_ready → task_* → final_answer → run_finished`。

> 成本提示：聊天请求 = 1 次分类 + 1 次回答；任务请求 = 1 次分类 + 1 次规划 + 1 次回答。

---

## 8. 参考实现

### 8.1 `agent/intent.py`

```python
"""判断用户请求走聊天还是任务规划。"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

from tools.registry import ToolRegistry

if TYPE_CHECKING:
    from agent.llm import LLM

logger = logging.getLogger(__name__)

INTENT_TOOL_NAME = "classify_intent"

INTENT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": INTENT_TOOL_NAME,
        "description": "判断用户请求应该直接聊天回答，还是需要任务规划。",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": ["chat", "task"]},
                "reason": {"type": "string", "description": "一句话理由"},
            },
            "required": ["intent"],
        },
    },
}

INTENT_PROMPT = """\
你是一个意图分类器。判断用户这句话应该走哪条路径：

- chat：闲聊、问候、常识、知识问答、写作、观点、数学等，不需要实时数据或外部工具。
- task：需要查询实时/外部信息，或需要调用工具、多步操作才能完成。

可用工具（仅用于判断，不代表用户一定需要）：
"""

INTENT_RULES = """\
判断规则：
1. 不确定时选 chat，避免误执行工具。
2. 工具列表里有某个工具，不代表无关问题要判成 task。
3. 只通过 classify_intent 工具输出，不要输出解释。

示例：
- "你好" -> chat
- "曹操的儿子叫什么" -> chat
- "1+1 等于几" -> chat
- "今天成都适合酿酒吗" -> task
- "帮我查一下成都天气" -> task
"""


class Intent(str, Enum):
    """用户请求的路由目标。"""
    CHAT = "chat"
    TASK = "task"


class IntentRouter:
    """用一次轻量 LLM 调用判断 chat / task。"""

    def __init__(self, llm: "LLM", registry: ToolRegistry):
        """初始化路由器。

        Args:
            llm: 只需要 async chat(...) 方法；测试时可注入假模型。
            registry: 提供工具名与描述，帮助判断是否需要工具。
        """
        self.llm = llm
        self.registry = registry

    def _system_prompt(self) -> str:
        """拼接分类提示词和工具清单。"""
        tools = []
        for name in self.registry.list_tools():
            fn = self.registry.get_tool_schema(name)["function"]
            tools.append(f"- {name}: {fn['description']}")
        return INTENT_PROMPT + "\n".join(tools) + "\n\n" + INTENT_RULES

    async def route(self, user_input: str) -> Intent:
        """返回 chat 或 task；分类失败时回退到 chat。

        Args:
            user_input: 用户请求。

        Returns:
            Intent.CHAT 或 Intent.TASK。
        """
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": user_input},
        ]
        try:
            response = await self.llm.chat(
                messages=messages,
                tools=[INTENT_TOOL],
                tool_choice={"type": "function", "function": {"name": INTENT_TOOL_NAME}},
                thinking=False,
            )
        except Exception:
            logger.exception("意图分类请求失败，回退到 chat")
            return Intent.CHAT

        call = self._find_call(response)
        if call is None:
            logger.warning("意图分类没有返回工具调用，回退到 chat")
            return Intent.CHAT

        try:
            arguments = json.loads(call.function.arguments)
            intent = Intent(arguments["intent"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("意图分类结果不合法，回退到 chat: %r", call.function.arguments)
            return Intent.CHAT

        logger.info("意图分类: %s", intent.value)
        return intent

    @staticmethod
    def _find_call(response):
        """返回名为 classify_intent 的 tool call；没有则返回 None。"""
        for call in response.tool_calls or []:
            if call.function.name == INTENT_TOOL_NAME:
                return call
        return None
```

### 8.2 `agent/chat_responder.py`

```python
"""普通对话路径：直接回答，不规划、不调用工具。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

from agent.llm import LLM
from events.models import EventType, RuntimeEvent

logger = logging.getLogger(__name__)


class ChatResponder:
    """直接调用 LLM 回答用户。"""

    def __init__(self, llm: LLM, system_prompt: str):
        self.llm = llm
        self.system_prompt = system_prompt

    async def run(self, user_input: str, run_id: str | None = None) -> AsyncIterator[RuntimeEvent]:
        """回答用户并产出事件。"""
        run_id = run_id or str(uuid.uuid4())
        yield RuntimeEvent(type=EventType.RUN_STARTED, run_id=run_id)

        try:
            response = await self.llm.chat(
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_input},
                ]
            )
        except Exception as exc:
            logger.exception("聊天回答失败")
            yield RuntimeEvent(
                type=EventType.RUN_FINISHED,
                run_id=run_id,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        yield RuntimeEvent(
            type=EventType.FINAL_ANSWER,
            run_id=run_id,
            data={"content": response.content or ""},
        )
        yield RuntimeEvent(type=EventType.RUN_FINISHED, run_id=run_id)
```

### 8.3 `agent/runtime.py`

```python
import logging

from agent.chat_responder import ChatResponder
from agent.intent import Intent, IntentRouter
from agent.llm import LLM
from agent.plan_runner import PlanRunner
from agent.planner import TaskPlanner
from tasks.plan_executor import TaskPlanExecutor
from tasks.scheduler import TaskScheduler
from tools.builtin import brewing, get_current_time, get_weather
from tools.registry import ToolRegistry
from tools.runner import ToolRunner

logger = logging.getLogger(__name__)

TASK_SYSTEM_PROMPT = (
    "你是私人酒庄的葡萄酒酿酒大师。\n\n"
    "你可以使用提供的工具，不要自己编造工具结果。"
)

CHAT_SYSTEM_PROMPT = (
    "你是私人酒庄的葡萄酒酿酒大师。\n\n"
    "请用简洁、自然的中文回答用户；可以基于常识回答，但不要编造工具执行结果。"
)


class AgentRuntime:
    """组装路由、任务执行链与聊天链路，对外提供事件流。"""

    def __init__(self, llm: LLM | None = None):
        self.llm = llm or LLM()

        registry = ToolRegistry()
        registry.register(get_weather)
        registry.register(get_current_time)
        registry.register(brewing)

        tool_runner = ToolRunner(registry=registry)
        scheduler = TaskScheduler(tool_runner=tool_runner)
        executor = TaskPlanExecutor(scheduler=scheduler, registry=registry)
        planner = TaskPlanner(llm=self.llm, registry=registry)

        self.router = IntentRouter(llm=self.llm, registry=registry)
        self.task_runner = PlanRunner(
            planner=planner,
            executor=executor,
            llm=self.llm,
            system_prompt=TASK_SYSTEM_PROMPT,
        )
        self.chat_responder = ChatResponder(
            llm=self.llm,
            system_prompt=CHAT_SYSTEM_PROMPT,
        )

        logger.info("AgentRuntime 初始化完成，模型: %s", getattr(self.llm, "model", "unknown"))

    async def run(self, user_input: str):
        """根据意图选择聊天或任务路径。"""
        intent = await self.router.route(user_input)
        logger.info("路由意图: %s", intent.value)

        runner = self.task_runner if intent == Intent.TASK else self.chat_responder
        async for event in runner.run(user_input):
            yield event
```

### 8.4 `agent/__init__.py`

```python
from .chat_responder import ChatResponder
from .intent import Intent, IntentRouter
from .llm import LLM
from .plan_runner import PlanRunner
from .planner import PlanGenerationError, TaskPlanner
from .runtime import AgentRuntime

__all__ = [
    "LLM",
    "ChatResponder",
    "Intent",
    "IntentRouter",
    "PlanRunner",
    "PlanGenerationError",
    "TaskPlanner",
    "AgentRuntime",
]
```

---

## 9. 验收清单（DoD）

- [ ] `Intent` 和 `classify_intent` 工具存在
- [ ] `IntentRouter` 分类失败时回退 CHAT
- [ ] 分类请求强制 `classify_intent` + `thinking=False`
- [ ] `ChatResponder` 不传 tools
- [ ] `AgentRuntime.run()` 按意图选择 runner
- [ ] 「曹操的儿子叫什么」走聊天，不产生 `plan_*`
- [ ] 「今天成都适合酿酒吗」走任务，事件与第 6 课一致
- [ ] `pytest tests/unit/test_lesson07_routing.py -q` → 10 passed
- [ ] 全量 `pytest -q` → 38 passed
- [ ] 两条冒烟命令都符合预期
- [ ] `PlanRunner` / `TaskPlanner` / 调度器没有改动

---

## 10. 常见坑

| 坑 | 现象 | 处理 |
|---|---|---|
| 分类器用 `tool_choice="auto"` | 模型有时不调用工具，结果为空 | 强制 `classify_intent` |
| 忘记 `thinking=False` | DeepSeek 400：thinking 不支持 named tool_choice | 和 Planner 一样关闭 |
| 工具列表让分类器过度判 task | 「曹操的儿子」也走 task | prompt 明确「工具列表不代表一定需要」+ 反例 |
| 分类失败默认 task | 误执行工具 | 默认 CHAT |
| 聊天路径复用任务提示词 | 仍然回答「只能基于工具结果」 | 单独的 `CHAT_SYSTEM_PROMPT` |
| `ChatResponder` 传了 tools | 模型发起工具调用，没人执行 | 不传 tools |
| `AgentRuntime` 日志访问 `llm.model` | `FakeLLM` 没有 `model` 属性 | `getattr(self.llm, "model", "unknown")` |
| 测试 runtime 时构造真实 LLM | 没 key 直接失败 | `AgentRuntime(llm=FakeLLM(...))` |
| `FakeRunner` 写成普通协程 | `async for` 报错 | 里面要有 `yield` |
| 追问「那明天呢」被误判 | 没有上下文 | 第 18 课后用会话上下文辅助路由 |

---

## 11. 扩展与思考题

1. **规则预筛**：问候语用正则直接走 chat，省一次分类调用。规则和 LLM 怎么组合？
2. **缓存**：相同输入的意图结果缓存，注意用户上下文变化。
3. **意图事件**：加一个 `intent_detected` 事件，前端显示「本次走了聊天/任务」。
4. **多意图**：`task + clarify`、`chat + memory`，路由从二分类变成多标签。
5. **小模型**：分类用更便宜/更快的模型，回答仍用主模型。
6. **分类器与工具目录**：第 8 课会把工具描述抽成 `ToolCatalog`，`IntentRouter` 和 `TaskPlanner` 可以共用，避免两处维护。

---

## 12. 与第 8 课的衔接

第 8 课做「让规划器理解工具真实能力」：

- 把 `TaskPlanner._format_tool()` 和 `IntentRouter._system_prompt()` 里重复的工具目录逻辑抽成 `ToolCatalog`；
- 目录里包含参数名、类型、必填、工具描述；
- 解决「工具一多，prompt 太长」的问题（压缩、分组、按需注入）。

本课先让路由正确，第 8 课再让两边都用同一份工具能力描述。
