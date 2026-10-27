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

    decision = asyncio.run(router.decide("曹操的儿子叫什么"))

    assert decision.intent is Intent.CHAT
    assert decision.source == "llm"
    assert decision.reason == "test"

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


def test_rule_greeting_skips_llm(registry):
    llm = FakeLLM([])
    router = IntentRouter(llm, registry)

    decision = asyncio.run(router.decide("你好"))

    assert decision.intent is Intent.CHAT
    assert decision.source == "rule"
    assert decision.rule == "greeting"
    assert llm.calls == []


def test_rule_task_phrase_goes_to_llm(registry):
    llm = FakeLLM([FakeMessage(tool_calls=[classify("task")])])
    router = IntentRouter(llm, registry)

    decision = asyncio.run(router.decide("你好，帮我查成都天气"))

    assert decision.intent is Intent.TASK
    assert decision.source == "llm"
    assert len(llm.calls) == 1


def test_router_malformed_json_falls_back_to_chat(registry):
    llm = FakeLLM([
        FakeMessage(tool_calls=[FakeToolCall(name="classify_intent", arguments="{not-json")])
    ])
    router = IntentRouter(llm, registry)

    assert asyncio.run(router.route("随便说点什么")) is Intent.CHAT


def test_router_missing_tool_call_falls_back_to_chat(registry):
    llm = FakeLLM([FakeMessage(content="这是一段普通回答")])
    router = IntentRouter(llm, registry)

    assert asyncio.run(router.route("曹操的儿子叫什么")) is Intent.CHAT


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

    decision = asyncio.run(router.decide("曹操的儿子叫什么"))

    assert decision.intent is Intent.CHAT
    assert decision.source == "fallback"


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
        EventType.INTENT_DETECTED,
        EventType.RUN_STARTED,
        EventType.FINAL_ANSWER,
        EventType.RUN_FINISHED,
    ]
    assert events[0].data == {
        "intent": "chat",
        "source": "llm",
        "rule": None,
        "reason": "test",
    }
    assert "曹昂" in events[2].data["content"]
    assert len({e.run_id for e in events}) == 1
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

    assert [e.type for e in events] == [
        EventType.INTENT_DETECTED,
        EventType.RUN_STARTED,
        EventType.FINAL_ANSWER,
        EventType.RUN_FINISHED,
    ]
    assert events[0].data["intent"] == "task"
    assert events[0].data["source"] == "llm"
    assert events[2].data["content"] == "任务回答"
    assert task_runner.calls == ["今天成都适合酿酒吗"]
    assert chat_runner.calls == []
    assert len({e.run_id for e in events}) == 1
    assert len(llm.calls) == 1


def test_runtime_rule_shortcut_skips_classifier():
    llm = FakeLLM([FakeMessage(content="你好！有什么可以帮你？")])
    runtime = AgentRuntime(llm=llm)

    events = asyncio.run(collect(runtime.run("你好")))

    assert [e.type for e in events] == [
        EventType.INTENT_DETECTED,
        EventType.RUN_STARTED,
        EventType.FINAL_ANSWER,
        EventType.RUN_FINISHED,
    ]
    assert events[0].data == {
        "intent": "chat",
        "source": "rule",
        "rule": "greeting",
        "reason": "",
    }
    assert len(llm.calls) == 1
    assert llm.calls[0]["tools"] is None
    assert len({e.run_id for e in events}) == 1
