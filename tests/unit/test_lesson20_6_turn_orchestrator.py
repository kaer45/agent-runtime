import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime import AgentRuntime
from agent.turn_orchestrator import TurnOrchestrator
from context.budget import TokenBudget
from context.builder import ContextBuilder
from context.history import HistoryStore, HistoryStoreError
from context.planner import ContextPlan, ContextPlanner
from context.summarizer import RollingSummary
from events.models import EventType
from state.extractor import StateDelta
from state.models import SessionState
from state.service import StateService
from state.registry import SessionStateRegistry
from state.store import StateSnapshot, StateStore
from state.updater import StateUpdateResult
from tests.fixtures.fake_llm import FakeLLM, FakeMessage, FakeToolCall


class FakeUpdater:
    def __init__(self, result: StateUpdateResult | None = None):
        self.result = result
        self.wait_calls: list[str] = []
        self.update_calls: list[tuple] = []

    async def wait(self, session_id: str) -> None:
        self.wait_calls.append(session_id)

    async def update(self, session_id, turn_id, messages):
        self.update_calls.append((session_id, turn_id, list(messages)))
        if self.result is not None:
            return self.result
        return StateUpdateResult(
            turn_id=turn_id,
            delta=StateDelta(),
            snapshot=StateSnapshot(SessionState(), 0),
            applied=False,
        )


class FakeResponder:
    def __init__(self, answer: str = "好的"):
        self.answer_text = answer
        self.calls: list[list[dict]] = []

    async def answer(self, messages):
        self.calls.append(list(messages))
        return self.answer_text


class FakeSummarizer:
    def __init__(
        self,
        summary: RollingSummary | None = None,
        error: Exception | None = None,
    ):
        self.summary = summary or RollingSummary(
            text="滚动摘要",
            covered_messages=0,
        )
        self.error = error
        self.calls: list[tuple[RollingSummary, list[dict]]] = []

    async def update(self, previous, messages):
        self.calls.append((previous, list(messages)))
        if self.error is not None:
            raise self.error
        return self.summary


class FakePlanner:
    def __init__(self, plan: ContextPlan):
        self.plan_value = plan
        self.calls: list[dict] = []

    def plan(self, **kwargs):
        self.calls.append(kwargs)
        return self.plan_value


def make_plan(
    *,
    kept_history,
    kept_ids,
    dropped_history=(),
    dropped_ids=(),
) -> ContextPlan:
    return ContextPlan(
        state_max_tokens=600,
        summary_max_tokens=400,
        summary_text_max_tokens=380,
        history_max_tokens=500,
        summary_tokens=0,
        history_tokens=0,
        kept_history=tuple(kept_history),
        kept_message_ids=tuple(kept_ids),
        dropped_history=tuple(dropped_history),
        dropped_message_ids=tuple(dropped_ids),
    )


@pytest.fixture
def parts():
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson20-6-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    state_store = StateStore(db_path)
    state_store.initialize()
    history = HistoryStore(db_path, max_messages_per_session=10)
    history.initialize()

    budget = TokenBudget(max_tokens=2000, reserve_output=400)
    builder = ContextBuilder(
        system_prompt="你是侍酒师。",
        budget=budget,
        state_max_tokens=600,
    )
    updater = FakeUpdater()
    responder = FakeResponder("推荐上梅多克。")
    summarizer = FakeSummarizer(
        RollingSummary(text="用户喜欢干型酒。", covered_messages=2)
    )
    planner = ContextPlanner(
        state_max_tokens=600,
        summary_max_tokens=400,
    )
    orchestrator = TurnOrchestrator(
        state_service=StateService(SessionStateRegistry(state_store)),
        history_store=history,
        planner=planner,
        builder=builder,
        summarizer=summarizer,
        updater=updater,
        chat_responder=responder,
    )

    try:
        yield SimpleNamespace(
            db_path=db_path,
            state_store=state_store,
            history=history,
            builder=builder,
            updater=updater,
            responder=responder,
            summarizer=summarizer,
            planner=planner,
            orchestrator=orchestrator,
        )
    finally:
        db_path.unlink(missing_ok=True)


def sample_history():
    return [
        {"role": "user", "content": "我喜欢干型酒。"},
        {"role": "assistant", "content": "可以看波尔多。"},
        {"role": "user", "content": "预算 800。"},
        {"role": "assistant", "content": "上梅多克不错。"},
    ]


def classify_chat() -> FakeToolCall:
    return FakeToolCall(
        name="classify_intent",
        arguments=json.dumps({"intent": "chat", "reason": "test"}),
        call_id="intent-1",
    )


def state_delta(**payload) -> FakeToolCall:
    return FakeToolCall(
        name="submit_state_delta",
        arguments=json.dumps(payload, ensure_ascii=False),
        call_id="state-1",
    )


async def collect(events):
    return [event async for event in events]


def test_begin_turn_waits_then_loads_state_and_history(parts):
    parts.history.append("session-1", sample_history())

    context = asyncio.run(
        parts.orchestrator.begin_turn("session-1", turn_id="turn-1")
    )

    assert parts.updater.wait_calls == ["session-1"]
    assert context.session_id == "session-1"
    assert context.turn_id == "turn-1"
    assert context.state.version == 0
    assert context.history.message_ids == [1, 2, 3, 4]
    assert context.history.covered_through_id is None


def test_run_chat_builds_messages_and_appends_history(parts):
    context = asyncio.run(
        parts.orchestrator.begin_turn("session-1", turn_id="turn-1")
    )

    events = asyncio.run(
        collect(parts.orchestrator.run_chat(context, "推荐一款。"))
    )

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.FINAL_ANSWER,
        EventType.RUN_FINISHED,
    ]
    assert events[1].data["content"] == "推荐上梅多克。"

    sent = parts.responder.calls[0]
    assert sent[0]["role"] == "system"
    assert sent[-1] == {"role": "user", "content": "推荐一款。"}

    snapshot = parts.history.load("session-1")
    assert snapshot.messages == [
        {"role": "user", "content": "推荐一款。"},
        {"role": "assistant", "content": "推荐上梅多克。"},
    ]
    assert parts.updater.update_calls[0][0] == "session-1"
    assert parts.updater.update_calls[0][1] == "turn-1"


def test_request_level_drop_summarizes_without_deleting(parts):
    parts.history.append("session-1", sample_history())
    context = asyncio.run(
        parts.orchestrator.begin_turn("session-1", turn_id="turn-1")
    )
    old = context.history
    parts.orchestrator.planner = FakePlanner(
        make_plan(
            kept_history=old.messages[-2:],
            kept_ids=old.message_ids[-2:],
            dropped_history=old.messages[:2],
            dropped_ids=old.message_ids[:2],
        )
    )

    events = asyncio.run(
        collect(parts.orchestrator.run_chat(context, "预算改成 1000。"))
    )

    assert events[-1].type == EventType.RUN_FINISHED
    assert parts.summarizer.calls[0][1] == old.messages[:2]

    snapshot = parts.history.load("session-1")
    assert snapshot.covered_through_id == 2
    assert snapshot.summary.text == "用户喜欢干型酒。"
    # 请求级摘要只推进 coverage，不删除；本轮结束再追加两条。
    assert snapshot.messages[:4] == sample_history()
    assert len(snapshot.messages) == 6

    sent = parts.responder.calls[0]
    assert any("用户喜欢干型酒" in message.get("content", "") for message in sent)


def test_storage_trim_summarizes_uncovered_before_delete(parts):
    parts.history.max_messages_per_session = 3
    parts.history.append("session-1", sample_history()[:3])
    context = asyncio.run(
        parts.orchestrator.begin_turn("session-1", turn_id="turn-1")
    )
    parts.orchestrator.planner = FakePlanner(
        make_plan(
            kept_history=context.history.messages,
            kept_ids=context.history.message_ids,
        )
    )
    parts.summarizer.summary = RollingSummary(
        text="存储级摘要",
        covered_messages=2,
    )

    events = asyncio.run(
        collect(parts.orchestrator.run_chat(context, "继续推荐。"))
    )

    assert events[-1].type == EventType.RUN_FINISHED
    assert len(parts.summarizer.calls) == 1
    trimmed_messages = parts.summarizer.calls[0][1]
    assert [message["content"] for message in trimmed_messages] == [
        "我喜欢干型酒。",
        "可以看波尔多。",
    ]

    snapshot = parts.history.load("session-1")
    assert parts.history.count("session-1") == 3
    assert snapshot.covered_through_id == 2
    assert snapshot.summary.text == "存储级摘要"


def test_state_failure_still_answers_and_appends_history(parts):
    parts.updater.result = StateUpdateResult(
        turn_id="turn-1",
        delta=StateDelta(),
        snapshot=StateSnapshot(SessionState(), 0),
        applied=False,
        error="state db down",
    )
    context = asyncio.run(
        parts.orchestrator.begin_turn("session-1", turn_id="turn-1")
    )

    events = asyncio.run(
        collect(parts.orchestrator.run_chat(context, "你好。"))
    )

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.FINAL_ANSWER,
        EventType.STATE_UPDATE_FAILED,
        EventType.RUN_FINISHED,
    ]
    assert events[2].data["error"] == "state db down"
    assert parts.history.count("session-1") == 2


def test_finish_turn_is_idempotent_for_same_turn(parts):
    context = asyncio.run(
        parts.orchestrator.begin_turn("session-1", turn_id="turn-1")
    )

    first = asyncio.run(
        parts.orchestrator.finish_turn(context, "你好。", "你好呀。")
    )
    second = asyncio.run(
        parts.orchestrator.finish_turn(context, "你好。", "你好呀。")
    )

    assert first.appended is True
    assert second.appended is False
    assert parts.history.count("session-1") == 2


def test_summary_persist_failure_degrades_but_answer_survives(parts, monkeypatch):
    parts.history.append("session-1", sample_history())
    context = asyncio.run(
        parts.orchestrator.begin_turn("session-1", turn_id="turn-1")
    )
    old = context.history
    parts.orchestrator.planner = FakePlanner(
        make_plan(
            kept_history=old.messages[-1:],
            kept_ids=old.message_ids[-1:],
            dropped_history=old.messages[:3],
            dropped_ids=old.message_ids[:3],
        )
    )

    def fail_save_summary(*args, **kwargs):
        raise HistoryStoreError("summary db down")

    monkeypatch.setattr(parts.history, "save_summary", fail_save_summary)

    events = asyncio.run(
        collect(parts.orchestrator.run_chat(context, "继续。"))
    )

    assert events[1].type == EventType.FINAL_ANSWER
    assert parts.responder.calls
    assert events[-1].type == EventType.RUN_FINISHED
    assert "摘要落库失败" in events[-1].data["warnings"][0]
    snapshot = parts.history.load("session-1")
    assert snapshot.covered_through_id is None
    assert len(snapshot.messages) == 6


def test_runtime_remembers_previous_turn_across_restart():
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson20-6-runtime-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    try:
        llm = FakeLLM([
            FakeMessage(tool_calls=[classify_chat()]),
            FakeMessage(content="推荐波尔多。"),
            FakeMessage(tool_calls=[state_delta(preferences_set={"style": "dry"})]),
            FakeMessage(tool_calls=[classify_chat()]),
            FakeMessage(content="可以看波尔多。"),
            FakeMessage(tool_calls=[state_delta()]),
        ])
        runtime = AgentRuntime(llm=llm, db_path=db_path)

        first = asyncio.run(
            collect(runtime.run("我喜欢干型酒。", session_id="session-1"))
        )
        assert first[-1].type == EventType.RUN_FINISHED
        assert all(event.session_id == "session-1" for event in first)
        assert runtime.history_store.count("session-1") == 2

        second = asyncio.run(
            collect(runtime.run("推荐一款。", session_id="session-1"))
        )
        assert second[-1].type == EventType.RUN_FINISHED
        assert runtime.history_store.count("session-1") == 4

        # 重启：用同一个 DB 新建 Runtime，应恢复状态和历史。
        restarted_llm = FakeLLM([
            FakeMessage(tool_calls=[classify_chat()]),
            FakeMessage(content="预算 800 的波尔多。"),
            FakeMessage(tool_calls=[state_delta()]),
        ])
        restarted = AgentRuntime(llm=restarted_llm, db_path=db_path)
        third = asyncio.run(
            collect(restarted.run("那预算 800 呢？", session_id="session-1"))
        )

        assert third[-1].type == EventType.RUN_FINISHED
        assert restarted.history_store.count("session-1") == 6
        snapshot = restarted.history_store.load("session-1")
        assert [message["content"] for message in snapshot.messages[:4]] == [
            "我喜欢干型酒。",
            "推荐波尔多。",
            "推荐一款。",
            "可以看波尔多。",
        ]

        state = asyncio.run(restarted.state_service.get("session-1"))
        assert state.state.preferences == {"style": "dry"}
        assert state.version == 1

        third_chat_messages = restarted_llm.calls[1]["messages"]
        assert any(
            message.get("content") == "我喜欢干型酒。"
            for message in third_chat_messages
        )
        assert "dry" in third_chat_messages[0]["content"]
    finally:
        db_path.unlink(missing_ok=True)
