import asyncio

from agent.plan_runner import PlanRunner
from agent.planner import PlanGenerationError
from events.models import EventType, RuntimeEvent
from tasks.plan import TaskPlan
from tasks.plan_executor import TaskPlanExecutor
from tasks.scheduler import TaskScheduler
from tests.fixtures.fake_llm import FakeLLM, FakeMessage
from tools.registry import ToolRegistry
from tools.runner import ToolRunner


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


def make_plan() -> TaskPlan:
    return TaskPlan.model_validate({"tasks": TASKS})


class FakePlanner:
    def __init__(self, plan=None, error=None):
        self.plan_obj = plan
        self.error = error
        self.calls = []

    async def plan(self, user_input: str) -> TaskPlan:
        self.calls.append(user_input)
        if self.error:
            raise self.error
        return self.plan_obj


class FakeExecutor:
    def __init__(self, events):
        self.events = events
        self.calls = []

    async def execute(self, plan, run_id):
        self.calls.append((plan, run_id))
        for event in self.events:
            yield RuntimeEvent(
                type=event.type,
                run_id=run_id,
                task_id=event.task_id,
                data=event.data,
            )


class BrokenExecutor:
    async def execute(self, plan, run_id):
        raise RuntimeError("executor boom")
        yield  # pragma: no cover


async def collect(events):
    return [event async for event in events]


def success_events():
    return [
        RuntimeEvent(EventType.RUN_STARTED, "ignored"),
        RuntimeEvent(EventType.TASK_STARTED, "ignored", "T1", {"tool_name": "get_weather"}),
        RuntimeEvent(
            EventType.TASK_SUCCESS, "ignored", "T1",
            {"tool_name": "get_weather", "result": "波尔多-晴"},
        ),
        RuntimeEvent(
            EventType.TASK_SUCCESS, "ignored", "T2",
            {"tool_name": "get_current_time", "result": "2026-10-26 22:00:00"},
        ),
        RuntimeEvent(
            EventType.TASK_SUCCESS, "ignored", "T3",
            {"tool_name": "brewing", "result": "适合酿酒"},
        ),
        RuntimeEvent(EventType.RUN_FINISHED, "ignored"),
    ]


def test_run_emits_plan_execute_and_answer():
    plan = make_plan()
    executor = FakeExecutor(success_events())
    llm = FakeLLM([FakeMessage(content="结论：适合酿酒")])
    runner = PlanRunner(FakePlanner(plan), executor, llm, "系统提示")

    events = asyncio.run(collect(runner.run("今天波尔多适合酿酒吗？", run_id="run-1")))

    assert [e.type for e in events] == [
        EventType.RUN_STARTED,
        EventType.PLAN_STARTED,
        EventType.PLAN_READY,
        EventType.TASK_STARTED,
        EventType.TASK_SUCCESS,
        EventType.TASK_SUCCESS,
        EventType.TASK_SUCCESS,
        EventType.FINAL_ANSWER,
        EventType.RUN_FINISHED,
    ]
    assert {e.run_id for e in events} == {"run-1"}
    assert executor.calls == [(plan, "run-1")]
    assert events[2].data["task_count"] == 3
    assert events[-2].data["content"] == "结论：适合酿酒"
    assert llm.calls[0]["tools"] is None


def test_results_are_passed_to_answer_llm():
    llm = FakeLLM([FakeMessage(content="结论")])
    runner = PlanRunner(FakePlanner(make_plan()), FakeExecutor(success_events()), llm, "系统提示")

    asyncio.run(collect(runner.run("今天适合酿酒吗？", run_id="run-1")))

    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "波尔多-晴" in prompt
    assert "适合酿酒" in prompt
    assert "T3" in prompt


def test_plan_failure_stops_before_execution():
    executor = FakeExecutor([])
    llm = FakeLLM([])
    runner = PlanRunner(
        FakePlanner(error=PlanGenerationError("模型没给出合法计划")),
        executor,
        llm,
        "系统提示",
    )

    events = asyncio.run(collect(runner.run("今天适合酿酒吗？", run_id="run-bad")))

    assert [e.type for e in events] == [
        EventType.RUN_STARTED,
        EventType.PLAN_STARTED,
        EventType.PLAN_FAILED,
        EventType.RUN_FINISHED,
    ]
    assert "模型没给出合法计划" in events[2].data["error"]
    assert executor.calls == []
    assert llm.calls == []


def test_failed_and_blocked_results_are_passed_to_llm():
    events_in = [
        RuntimeEvent(EventType.RUN_STARTED, "ignored"),
        RuntimeEvent(EventType.TASK_FAILED, "ignored", "T1",
                     {"tool_name": "get_weather", "error": "boom"}),
        RuntimeEvent(EventType.TASK_BLOCKED, "ignored", "T3",
                     {"tool_name": "brewing", "reason": "Dependency 'T1' blocked"}),
        RuntimeEvent(EventType.RUN_FINISHED, "ignored"),
    ]
    llm = FakeLLM([FakeMessage(content="失败原因")])
    runner = PlanRunner(FakePlanner(make_plan()), FakeExecutor(events_in), llm, "系统提示")

    asyncio.run(collect(runner.run("今天适合酿酒吗？", run_id="run-2")))

    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "boom" in prompt
    assert "blocked" in prompt


def test_run_id_is_generated_when_absent():
    llm = FakeLLM([FakeMessage(content="结论")])
    runner = PlanRunner(FakePlanner(make_plan()), FakeExecutor(success_events()), llm, "系统提示")

    events = asyncio.run(collect(runner.run("今天适合酿酒吗？")))

    run_ids = {e.run_id for e in events}
    assert len(run_ids) == 1
    assert next(iter(run_ids))


def test_executor_error_ends_run():
    llm = FakeLLM([])
    runner = PlanRunner(FakePlanner(make_plan()), BrokenExecutor(), llm, "系统提示")

    events = asyncio.run(collect(runner.run("今天适合酿酒吗？", run_id="run-3")))

    assert [e.type for e in events] == [
        EventType.RUN_STARTED,
        EventType.PLAN_STARTED,
        EventType.PLAN_READY,
        EventType.RUN_FINISHED,
    ]
    assert "executor boom" in events[-1].data["error"]
    assert llm.calls == []


async def get_weather(city: str) -> str:
    return f"{city}-晴"


async def get_current_time() -> str:
    return "2026-10-26 22:00:00"


async def brewing(weather: str, current_time: str) -> str:
    return f"{weather} / {current_time} / 适合酿酒"


def test_integration_with_real_scheduler():
    registry = ToolRegistry()
    registry.register(get_weather)
    registry.register(get_current_time)
    registry.register(brewing)

    executor = TaskPlanExecutor(
        scheduler=TaskScheduler(tool_runner=ToolRunner(registry=registry)),
        registry=registry,
    )
    llm = FakeLLM([FakeMessage(content="综合：适合酿酒")])
    runner = PlanRunner(FakePlanner(make_plan()), executor, llm, "系统提示")

    events = asyncio.run(collect(runner.run("今天波尔多适合酿酒吗？", run_id="run-it")))

    successes = [e.task_id for e in events if e.type == EventType.TASK_SUCCESS]
    assert set(successes) == {"T1", "T2", "T3"}
    assert successes[-1] == "T3"
    assert {e.run_id for e in events} == {"run-it"}

    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "波尔多-晴" in prompt
    assert "适合酿酒" in prompt

    assert events[-2].type == EventType.FINAL_ANSWER
    assert events[-1].type == EventType.RUN_FINISHED