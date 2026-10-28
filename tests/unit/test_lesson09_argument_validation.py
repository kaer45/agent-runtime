import asyncio
from typing import Literal

import pytest

from agent.planner import PlanGenerationError, TaskPlanner
from events.models import EventType
from tasks.plan import TaskPlan
from tasks.plan_executor import TaskPlanExecutor
from tasks.scheduler import TaskScheduler
from tasks.validator import ArgumentValidationError, ToolArgumentValidator
from tests.fixtures.fake_llm import FakeLLM, FakeMessage, plan_call
from tools.builtin import get_current_time, get_weather
from tools.registry import ToolRegistry
from tools.runner import ToolRunner


def pour_wine(style: Literal["dry", "sweet"], bottles: int = 1) -> str:
    """按指定风格倒酒。

    Args:
        style: 酒款风格，只能是 dry 或 sweet。
        bottles: 倒几瓶，默认 1。
    """
    return f"{style} x {bottles}"


def select_wines(years: list[int]) -> list[str]:
    """按年份筛选酒款。

    Args:
        years: 年份列表，每个元素必须是整数。
    """
    return [str(year) for year in years]


async def current_label() -> str:
    """返回一个字符串标签，用于测试上游结果类型不匹配。"""
    return "not-an-int"


async def add_sugar(grams: int) -> str:
    """加糖。

    Args:
        grams: 糖的克数，必须是整数。
    """
    return f"{grams}g"


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.register(get_weather)
    r.register(get_current_time)
    r.register(pour_wine)
    r.register(select_wines)
    r.register(current_label)
    r.register(add_sugar)
    return r


def make_plan(tool: str, arguments: dict) -> TaskPlan:
    return TaskPlan(
        tasks=[
            {
                "id": "T1",
                "tool": tool,
                "arguments": arguments,
                "dependencies": [],
            }
        ]
    )


def make_chain_plan() -> TaskPlan:
    return TaskPlan(
        tasks=[
            {
                "id": "T1",
                "tool": "current_label",
                "arguments": {},
                "dependencies": [],
            },
            {
                "id": "T2",
                "tool": "add_sugar",
                "arguments": {"grams": {"from_task": "T1"}},
                "dependencies": ["T1"],
            },
        ]
    )


async def collect(events):
    return [event async for event in events]


def test_valid_arguments_pass(registry):
    validator = ToolArgumentValidator(registry)

    validator.validate_arguments("get_weather", {"city": "成都"})


def test_missing_required_argument_rejected(registry):
    validator = ToolArgumentValidator(registry)

    with pytest.raises(ArgumentValidationError) as exc:
        validator.validate_plan(make_plan("get_weather", {}))

    message = str(exc.value)
    assert "city" in message
    assert "required" in message


def test_wrong_type_rejected(registry):
    validator = ToolArgumentValidator(registry)

    with pytest.raises(ArgumentValidationError) as exc:
        validator.validate_plan(make_plan("get_weather", {"city": 123}))

    message = str(exc.value)
    assert "city" in message
    assert "string" in message


def test_extra_argument_rejected(registry):
    validator = ToolArgumentValidator(registry)

    with pytest.raises(ArgumentValidationError) as exc:
        validator.validate_plan(
            make_plan("get_weather", {"city": "成都", "unit": "celsius"})
        )

    message = str(exc.value)
    assert "unit" in message or "Additional" in message


def test_enum_value_rejected(registry):
    validator = ToolArgumentValidator(registry)

    validator.validate_arguments("pour_wine", {"style": "dry"})

    with pytest.raises(ArgumentValidationError) as exc:
        validator.validate_plan(make_plan("pour_wine", {"style": "sour"}))

    assert "sweet" in str(exc.value)


def test_array_item_type_rejected(registry):
    validator = ToolArgumentValidator(registry)

    with pytest.raises(ArgumentValidationError) as exc:
        validator.validate_plan(
            make_plan("select_wines", {"years": [2019, "2020"]})
        )

    message = str(exc.value)
    assert "2020" in message or "integer" in message


def test_plan_error_contains_task_and_tool(registry):
    validator = ToolArgumentValidator(registry)

    with pytest.raises(ArgumentValidationError) as exc:
        validator.validate_plan(make_plan("get_weather", {}))

    message = str(exc.value)
    assert "T1" in message
    assert "get_weather" in message


def test_placeholders_are_skipped_only_in_prevalidation(registry):
    validator = ToolArgumentValidator(registry)
    plan = make_chain_plan()

    # 预校验阶段看不到 T1 的结果，只能跳过含占位符的任务。
    validator.validate_plan(plan, allow_placeholders=True)

    # 关闭跳过后，占位符对象会被当成整数参数，校验失败。
    with pytest.raises(ArgumentValidationError):
        validator.validate_plan(plan, allow_placeholders=False)


def test_planner_repairs_invalid_arguments(registry):
    bad = [
        {
            "id": "T1",
            "tool": "get_weather",
            "arguments": {"city": 123},
            "dependencies": [],
        }
    ]
    good = [
        {
            "id": "T1",
            "tool": "get_weather",
            "arguments": {"city": "成都"},
            "dependencies": [],
        }
    ]
    llm = FakeLLM([
        FakeMessage(tool_calls=[plan_call(bad)]),
        FakeMessage(tool_calls=[plan_call(good, call_id="call_2")]),
    ])
    planner = TaskPlanner(llm, registry)

    plan = asyncio.run(planner.plan("今天成都天气怎么样"))

    assert plan.tasks[0].arguments["city"] == "成都"
    assert len(llm.calls) == 2

    tool_messages = [
        message for message in llm.calls[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert "city" in tool_messages[-1]["content"]


def test_planner_gives_up_after_invalid_arguments(registry):
    bad = [
        {
            "id": "T1",
            "tool": "get_weather",
            "arguments": {"city": 123},
            "dependencies": [],
        }
    ]
    llm = FakeLLM([
        FakeMessage(tool_calls=[plan_call(bad, call_id=f"c{i}")])
        for i in range(3)
    ])
    planner = TaskPlanner(llm, registry, max_repairs=2)

    with pytest.raises(PlanGenerationError, match="3 次尝试"):
        asyncio.run(planner.plan("今天成都天气怎么样"))

    assert len(llm.calls) == 3


def test_executor_rejects_invalid_arguments_before_scheduler(registry):
    executor = TaskPlanExecutor(scheduler=object(), registry=registry)
    plan = make_plan("get_weather", {"city": 123})

    with pytest.raises(ArgumentValidationError):
        asyncio.run(collect(executor.execute(plan, "run-1")))


def test_scheduler_validates_resolved_arguments(registry):
    executor = TaskPlanExecutor(
        scheduler=TaskScheduler(tool_runner=ToolRunner(registry=registry)),
        registry=registry,
    )

    events = asyncio.run(collect(executor.execute(make_chain_plan(), "run-1")))

    failed = [event for event in events if event.type == EventType.TASK_FAILED]
    assert [event.task_id for event in failed] == ["T2"]
    assert "integer" in failed[0].data["error"] or "not of type" in failed[0].data["error"]
