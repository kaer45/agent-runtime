import asyncio
import json
from types import SimpleNamespace

import pytest

from agent import turn_orchestrator as orchestrator_module
from agent.runtime import AgentRuntime
from agent.turn_orchestrator import TurnFinishResult, TurnOrchestrator
from context.engine import ContextEngine
from context.errors import ContextError
from context.models import ContextLimits
from core.issues import ContextIssueCode
from events.models import EventType
from history.compactor import HistoryCompactor
from history.errors import (
    HistoryFloorUnmet,
    HistoryHardCapExceeded,
    HistoryStoreError,
)
from history.store import HistoryStore
from history.summarizer import HistorySummarizer
from state.models import SessionState, StateDelta, StateSnapshot
from state.registry import SessionStateRegistry
from state.service import StateService
from state.store import StateStore
from state.updater import StateUpdateResult
from tests.fixtures.fake_llm import FakeLLM, FakeMessage, FakeToolCall

SESSION = "session-1"


# --------------------------------------------------------------------- 假件


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


class FakeModel:
    """同时提供 chat 和 chat_with_meta 的假模型。"""

    def __init__(self, answers=None, summaries=None):
        self.answers = list(answers or [FakeMessage(content="好的")])
        self.summaries = list(summaries or [])
        self.calls: list[dict] = []
        self.summary_calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        item = self.answers.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def chat_with_meta(self, messages, **kwargs):
        self.summary_calls.append({"messages": list(messages), **kwargs})
        content = self.summaries.pop(0) if self.summaries else "新摘要"
        return SimpleNamespace(
            message=SimpleNamespace(content=content),
            finish_reason="stop",
            usage=None,
        )


class StubCompactor:
    def __init__(self, *, ensure_error=None, after_error=None):
        self.ensure_error = ensure_error
        self.after_error = after_error
        self.ensure_calls = 0
        self.after_calls = 0

    async def ensure_hard_cap(self, session_id: str) -> None:
        self.ensure_calls += 1
        if self.ensure_error is not None:
            raise self.ensure_error

    async def after_turn(self, session_id: str):
        self.after_calls += 1
        if self.after_error is not None:
            raise self.after_error
        return None


# --------------------------------------------------------------------- 组装


def make_parts(
    tmp_path,
    *,
    limits: ContextLimits | None = None,
    answers=None,
    summaries=None,
    updater=None,
):
    db_path = tmp_path / "orchestrator.db"

    state_store = StateStore(db_path)
    state_store.initialize()
    store = HistoryStore(db_path)
    store.initialize()

    limits = limits or ContextLimits()
    model = FakeModel(answers, summaries)
    engine = ContextEngine(system_prompt="你是侍酒师。", limits=limits)
    summarizer = HistorySummarizer(
        model, available_tokens=limits.available_tokens
    )
    compactor = HistoryCompactor(
        store=store, summarizer=summarizer, limits=limits
    )
    updater = updater or FakeUpdater()

    orchestrator = TurnOrchestrator(
        state_service=StateService(SessionStateRegistry(state_store)),
        store=store,
        engine=engine,
        compactor=compactor,
        updater=updater,
        llm=model,
    )

    return SimpleNamespace(
        db_path=db_path,
        state_store=state_store,
        store=store,
        engine=engine,
        summarizer=summarizer,
        compactor=compactor,
        updater=updater,
        model=model,
        orchestrator=orchestrator,
    )


def user(text: str) -> dict:
    return {"role": "user", "content": text}


def assistant(text: str) -> dict:
    return {"role": "assistant", "content": text}


def seed_turn(store: HistoryStore, turn_id: str, text: str = "酒" * 48):
    store.append_turn_once(
        SESSION, turn_id, [user(text), assistant("")]
    )


async def collect(events):
    return [event async for event in events]


def run_chat(parts, user_input: str, turn_id: str = "turn-1"):
    context = asyncio.run(
        parts.orchestrator.begin_turn(SESSION, turn_id=turn_id)
    )
    return asyncio.run(collect(parts.orchestrator.run_chat(context, user_input)))


# --------------------------------------------------------------------- 读取


def test_begin_turn_waits_then_loads_state_and_history(tmp_path):
    parts = make_parts(tmp_path)
    seed_turn(parts.store, "t1")

    context = asyncio.run(
        parts.orchestrator.begin_turn(SESSION, turn_id="t2")
    )

    assert parts.updater.wait_calls == [SESSION]
    assert context.session_id == SESSION
    assert context.turn_id == "t2"
    assert context.state.version == 0
    assert [turn.seq for turn in context.history.turns] == [1]
    assert context.history.summary.text == ""
    assert context.history.covered_through_seq == 0


@pytest.mark.parametrize(
    "error, code",
    [
        (HistoryFloorUnmet("floor"), ContextIssueCode.HISTORY_FLOOR_UNMET),
        (
            HistoryHardCapExceeded("hard"),
            ContextIssueCode.HISTORY_HARD_CAP_EXCEEDED,
        ),
    ],
)
def test_begin_turn_maps_hard_cap_errors(tmp_path, monkeypatch, error, code):
    parts = make_parts(tmp_path)
    seed_turn(parts.store, "t1")
    monkeypatch.setattr(orchestrator_module, "HARD_ARCHIVE_LIMIT", 1)
    parts.orchestrator.compactor = StubCompactor(ensure_error=error)

    with pytest.raises(ContextError) as info:
        asyncio.run(parts.orchestrator.begin_turn(SESSION, turn_id="t2"))

    assert info.value.code is code
    assert info.value.issue.level.value == "error"


# --------------------------------------------------------------------- 执行


def test_run_chat_assembles_context_and_persists_turn(tmp_path):
    parts = make_parts(tmp_path)

    events = run_chat(parts, "推荐一款。")

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.FINAL_ANSWER,
        EventType.RUN_FINISHED,
    ]
    assert events[1].data["content"] == "好的"

    sent = parts.model.calls[0]["messages"]
    assert sent[0]["role"] == "system"
    assert "侍酒师" in sent[0]["content"]
    assert sent[-1] == user("推荐一款。")

    data = events[-1].data
    assert data["context"]["configured_capacity"] == 108_512
    assert data["context"]["active_turns"] == 0
    assert "compaction" in data

    snapshot = parts.store.load(SESSION)
    assert [turn.seq for turn in snapshot.turns] == [1]
    assert list(snapshot.turns[0].messages) == [
        user("推荐一款。"),
        assistant("好的"),
    ]
    assert parts.updater.update_calls[0][0] == SESSION
    assert parts.updater.update_calls[0][1] == "turn-1"


def test_second_turn_sees_previous_turn(tmp_path):
    parts = make_parts(
        tmp_path,
        answers=[FakeMessage(content="第一次回答"), FakeMessage(content="第二次回答")],
    )

    run_chat(parts, "第一次问题", turn_id="t1")
    run_chat(parts, "第二次问题", turn_id="t2")

    sent = parts.model.calls[1]["messages"]
    contents = [message.get("content") for message in sent]
    assert "第一次问题" in contents
    assert "第一次回答" in contents
    assert sent[-1] == user("第二次问题")
    assert parts.store.count_turns(SESSION) == 2


def test_user_input_too_long_ends_turn_without_calling_model(tmp_path):
    parts = make_parts(
        tmp_path,
        limits=ContextLimits(user_ceiling=4),
    )

    events = run_chat(parts, "这句话明显超过了四个 token")

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.RUN_FINISHED,
    ]
    assert events[-1].data["error_code"] == "USER_INPUT_TOO_LONG"
    assert parts.model.calls == []
    assert parts.store.count_turns(SESSION) == 0


def test_model_failure_ends_turn_without_persisting(tmp_path):
    parts = make_parts(tmp_path, answers=[RuntimeError("boom")])

    events = run_chat(parts, "推荐一款。")

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.RUN_FINISHED,
    ]
    assert "boom" in events[-1].data["error"]
    assert parts.store.count_turns(SESSION) == 0
    assert parts.updater.update_calls == []


# --------------------------------------------------------------------- 收尾


def test_state_failure_keeps_answer_and_appends_turn(tmp_path):
    updater = FakeUpdater(
        result=StateUpdateResult(
            turn_id="turn-1",
            delta=StateDelta(),
            snapshot=StateSnapshot(SessionState(), 0),
            applied=False,
            error="state db down",
        )
    )
    parts = make_parts(tmp_path, updater=updater)

    events = run_chat(parts, "你好。")

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.FINAL_ANSWER,
        EventType.STATE_UPDATE_FAILED,
        EventType.RUN_FINISHED,
    ]
    assert events[2].data["error"] == "state db down"
    assert "状态更新失败" in events[-1].data["warnings"][0]
    assert parts.store.count_turns(SESSION) == 1


def test_finish_turn_is_idempotent(tmp_path):
    parts = make_parts(tmp_path)
    context = asyncio.run(
        parts.orchestrator.begin_turn(SESSION, turn_id="turn-1")
    )

    first = asyncio.run(
        parts.orchestrator.finish_turn(context, "你好。", "你好呀。")
    )
    second = asyncio.run(
        parts.orchestrator.finish_turn(context, "你好。", "你好呀。")
    )

    assert isinstance(first, TurnFinishResult)
    assert first.appended is True
    assert second.appended is False
    assert parts.store.count_turns(SESSION) == 1


def test_append_failure_skips_compaction(tmp_path, monkeypatch):
    parts = make_parts(tmp_path)
    parts.orchestrator.compactor = stub = StubCompactor()

    def boom(*args, **kwargs):
        raise HistoryStoreError("disk full")

    monkeypatch.setattr(parts.store, "append_turn_once", boom)

    result = asyncio.run(
        parts.orchestrator.finish_turn(
            asyncio.run(
                parts.orchestrator.begin_turn(SESSION, turn_id="turn-1")
            ),
            "你好。",
            "你好呀。",
        )
    )

    assert result.appended is False
    assert result.compaction is None
    assert "历史追加失败" in result.warnings[0]
    assert stub.after_calls == 0


def test_compaction_hard_cap_becomes_warning(tmp_path):
    parts = make_parts(tmp_path)
    parts.orchestrator.compactor = StubCompactor(
        after_error=HistoryHardCapExceeded("still over")
    )
    context = asyncio.run(
        parts.orchestrator.begin_turn(SESSION, turn_id="turn-1")
    )

    result = asyncio.run(
        parts.orchestrator.finish_turn(context, "你好。", "你好呀。")
    )

    assert result.appended is True
    assert result.compaction is None
    assert "历史硬上限" in result.warnings[0]


def test_finish_exception_is_contained(tmp_path, monkeypatch):
    parts = make_parts(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(parts.store, "append_turn_once", boom)

    events = run_chat(parts, "你好。")

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.FINAL_ANSWER,
        EventType.RUN_FINISHED,
    ]
    assert "回合收尾异常" in events[-1].data["warnings"][0]


# --------------------------------------------------------------------- 压缩


def test_compaction_result_is_reported(tmp_path):
    limits = ContextLimits(history_ceiling=400)
    parts = make_parts(
        tmp_path,
        limits=limits,
        answers=[FakeMessage(content="好的")],
        summaries=["新摘要"],
    )
    for index in range(1, 9):
        seed_turn(parts.store, f"t{index}")

    events = run_chat(parts, "推荐一款。")

    data = events[-1].data
    assert data["compaction"]["decision"] == "compact"
    assert data["compaction"]["compacted"] is True
    assert data["compaction"]["covered_through_seq"] == 3

    snapshot = parts.store.load(SESSION)
    assert [turn.seq for turn in snapshot.turns] == [4, 5, 6, 7, 8, 9]
    assert snapshot.summary.text == "新摘要"
    assert len(parts.model.summary_calls) == 1


# --------------------------------------------------------------------- 端到端


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


def test_runtime_remembers_turns_and_state_across_restart(tmp_path):
    db_path = tmp_path / "runtime.db"

    llm = FakeLLM(
        [
            FakeMessage(tool_calls=[classify_chat()]),
            FakeMessage(content="推荐波尔多。"),
            FakeMessage(
                tool_calls=[state_delta(preferences_set={"style": "dry"})]
            ),
            FakeMessage(tool_calls=[classify_chat()]),
            FakeMessage(content="可以看波尔多。"),
            FakeMessage(tool_calls=[state_delta()]),
        ]
    )
    runtime = AgentRuntime(llm=llm, db_path=db_path)

    first = asyncio.run(collect(runtime.run("我喜欢干型酒。", session_id=SESSION)))
    assert first[-1].type == EventType.RUN_FINISHED
    assert all(event.session_id == SESSION for event in first)
    assert runtime.history_store.count_turns(SESSION) == 1

    second = asyncio.run(collect(runtime.run("推荐一款。", session_id=SESSION)))
    assert second[-1].type == EventType.RUN_FINISHED
    assert runtime.history_store.count_turns(SESSION) == 2

    restarted_llm = FakeLLM(
        [
            FakeMessage(tool_calls=[classify_chat()]),
            FakeMessage(content="预算 800 的波尔多。"),
            FakeMessage(tool_calls=[state_delta()]),
        ]
    )
    restarted = AgentRuntime(llm=restarted_llm, db_path=db_path)
    third = asyncio.run(
        collect(restarted.run("那预算 800 呢？", session_id=SESSION))
    )

    assert third[-1].type == EventType.RUN_FINISHED
    assert restarted.history_store.count_turns(SESSION) == 3

    snapshot = restarted.history_store.load(SESSION)
    assert [list(turn.messages)[0]["content"] for turn in snapshot.turns] == [
        "我喜欢干型酒。",
        "推荐一款。",
        "那预算 800 呢？",
    ]

    state = asyncio.run(restarted.state_service.get(SESSION))
    assert state.state.preferences == {"style": "dry"}
    assert state.version == 1

    third_chat_messages = restarted_llm.calls[1]["messages"]
    contents = [message.get("content", "") for message in third_chat_messages]
    assert any("我喜欢干型酒。" in content for content in contents)
    assert any("dry" in content for content in contents)
    assert third_chat_messages[-1] == user("那预算 800 呢？")
