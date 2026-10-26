import asyncio
import json

import pytest

from agent.planner import PLAN_TOOL, PLAN_TOOL_NAME, PlanGenerationError, TaskPlanner
from tests.fixtures.fake_llm import FakeLLM, FakeMessage, FakeToolCall, plan_call
from tools.builtin import brewing, get_current_time, get_weather
from tools.registry import ToolRegistry


TASKS = [
    {"id": "T1", "tool": "get_weather", "arguments": {"city": "波尔多"}, "dependencies": []},
    {"id": "T2", "tool": "get_current_time", "arguments": {}, "dependencies": []},
    {
        "id": "T3",
        "tool": "brewing",
        "arguments": {
            "weather": {"from_task": "T1"},
            "current_time": {"from_task": "T2"},
        },
        "dependencies": ["T1", "T2"],
    },
]


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.register(get_weather)
    r.register(get_current_time)
    r.register(brewing)
    return r


def test_plan_success(registry):
    llm = FakeLLM([FakeMessage(tool_calls=[plan_call(TASKS)])])
    planner = TaskPlanner(llm, registry)

    plan = asyncio.run(planner.plan("今天波尔多适合酿酒吗？"))

    assert [t.id for t in plan.tasks] == ["T1", "T2", "T3"]
    assert plan.tasks[2].dependencies == ["T1", "T2"]
    assert plan.tasks[2].arguments["weather"] == {"from_task": "T1"}

    call = llm.calls[0]
    assert call["thinking"] is False
    assert call["tools"] == [PLAN_TOOL]
    assert call["tool_choice"] == {
        "type": "function",
        "function": {"name": PLAN_TOOL_NAME},
    }
    system = call["messages"][0]["content"]
    assert "get_weather" in system and "brewing" in system
    assert "current_time" in system


def test_repair_missing_dependency(registry):
    bad = [dict(t) for t in TASKS]
    bad[2] = {**bad[2], "dependencies": []}  # from_task 还在，但依赖忘了写

    llm = FakeLLM([
        FakeMessage(tool_calls=[plan_call(bad)]),
        FakeMessage(tool_calls=[plan_call(TASKS, call_id="call_2")]),
    ])
    planner = TaskPlanner(llm, registry)

    plan = asyncio.run(planner.plan("今天波尔多适合酿酒吗？"))

    assert len(plan.tasks) == 3
    assert len(llm.calls) == 2
    tool_errors = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_errors and "dependencies" in tool_errors[-1]["content"]


def test_repair_unknown_tool(registry):
    bad = [dict(t) for t in TASKS]
    bad[2] = {**bad[2], "tool": "make_wine"}

    llm = FakeLLM([
        FakeMessage(tool_calls=[plan_call(bad)]),
        FakeMessage(tool_calls=[plan_call(TASKS, call_id="call_2")]),
    ])
    planner = TaskPlanner(llm, registry)

    plan = asyncio.run(planner.plan("帮我酿酒"))

    assert plan.tasks[2].tool == "brewing"
    assert len(llm.calls) == 2
    assert "未注册的工具" in llm.calls[1]["messages"][-1]["content"]


def test_repair_malformed_json(registry):
    llm = FakeLLM([
        FakeMessage(tool_calls=[FakeToolCall(arguments="{not-json")]),
        FakeMessage(tool_calls=[plan_call(TASKS, call_id="call_2")]),
    ])
    planner = TaskPlanner(llm, registry)

    plan = asyncio.run(planner.plan("今天适合酿酒吗？"))

    assert len(plan.tasks) == 3
    assert len(llm.calls) == 2
    assert "计划不合法" in llm.calls[1]["messages"][-1]["content"]


def test_content_fallback(registry):
    llm = FakeLLM([
        FakeMessage(content=json.dumps({"tasks": TASKS}, ensure_ascii=False))
    ])
    planner = TaskPlanner(llm, registry)

    plan = asyncio.run(planner.plan("今天适合酿酒吗？"))

    assert len(plan.tasks) == 3
    assert len(llm.calls) == 1


def test_give_up_after_max_repairs(registry):
    bad = [dict(t) for t in TASKS]
    bad[2] = {**bad[2], "dependencies": []}

    llm = FakeLLM([
        FakeMessage(tool_calls=[plan_call(bad, call_id=f"c{i}")]) for i in range(3)
    ])
    planner = TaskPlanner(llm, registry, max_repairs=2)

    with pytest.raises(PlanGenerationError, match="3 次尝试"):
        asyncio.run(planner.plan("今天适合酿酒吗？"))

    assert len(llm.calls) == 3


def test_invalid_content_then_repair(registry):
    llm = FakeLLM([
        FakeMessage(content="我想调用 get_weather"),
        FakeMessage(tool_calls=[plan_call(TASKS, call_id="call_2")]),
    ])
    planner = TaskPlanner(llm, registry)

    plan = asyncio.run(planner.plan("今天适合酿酒吗？"))

    assert len(plan.tasks) == 3
    assert len(llm.calls) == 2
    assert "没有调用" in llm.calls[1]["messages"][-1]["content"]


def test_max_repairs_must_be_non_negative(registry):
    with pytest.raises(ValueError):
        TaskPlanner(FakeLLM([]), registry, max_repairs=-1)