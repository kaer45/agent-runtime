"""M2 thin slice：store → engine → 假 LLM → store → 空压缩器。

不引入新的生产 API，只用测试替身把上下文的入口和出口接起来，
证明装配层与存储层的 seam 是通的。
"""

from __future__ import annotations

from context.engine import ContextEngine
from context.models import ContextRequest
from history.metrics import CompactionDecision
from history.store import HistoryStore
from state.models import SessionState, StateSnapshot


class NullCompactor:
    """替代 HistoryCompactor：只记录被调用，不做压缩。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.decision = CompactionDecision.NOOP

    def after_turn(self, session_id: str) -> CompactionDecision:
        self.calls.append(session_id)
        return self.decision


def fake_llm(messages: list[dict]) -> dict:
    """假模型：把最后一条用户输入原样回显。"""
    last_user = next(
        message["content"]
        for message in reversed(messages)
        if message["role"] == "user"
    )
    return {"role": "assistant", "content": f"回应：{last_user}"}


def build_request(snapshot, text: str, turn_id: str) -> ContextRequest:
    return ContextRequest(
        session_id="session-1",
        turn_id=turn_id,
        user_input=text,
        state=StateSnapshot(SessionState(), 0),
        history=snapshot,
        tools=(),
        required_tools=frozenset(),
    )


def run_one_turn(store, engine, compactor, text: str, turn_id: str) -> dict:
    snapshot = store.load("session-1")
    request = build_request(snapshot, text, turn_id)
    result = engine.assemble(request)

    reply = fake_llm(result.messages)
    store.append_turn_once(
        "session-1",
        turn_id,
        [
            {"role": "user", "content": text},
            reply,
        ],
    )
    compactor.after_turn("session-1")
    return result.report.to_log()


def test_two_turns_round_trip(tmp_path):
    store = HistoryStore(tmp_path / "history.db", counter=len)
    store.initialize()
    engine = ContextEngine(system_prompt="你是侍酒师。", counter=len)
    compactor = NullCompactor()

    first = run_one_turn(store, engine, compactor, "我想喝干白", "turn-1")
    assert first["history_tokens"] == 0
    assert first["active_turns"] == 0

    second = run_one_turn(store, engine, compactor, "预算 300 以内", "turn-2")

    snapshot = store.load("session-1")
    assert [turn.seq for turn in snapshot.turns] == [1, 2]
    assert second["active_turns"] == 1
    assert second["history_rendered_tokens"] > 0
    assert compactor.calls == ["session-1", "session-1"]


def test_second_call_sees_first_turn_in_messages(tmp_path):
    store = HistoryStore(tmp_path / "history.db", counter=len)
    store.initialize()
    engine = ContextEngine(system_prompt="你是侍酒师。", counter=len)
    compactor = NullCompactor()

    run_one_turn(store, engine, compactor, "我想喝干白", "turn-1")

    snapshot = store.load("session-1")
    request = build_request(snapshot, "预算 300 以内", "turn-2")
    result = engine.assemble(request)

    roles = [message["role"] for message in result.messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert result.messages[1]["content"] == "我想喝干白"
    assert result.messages[2]["content"] == "回应：我想喝干白"
    assert result.messages[-1]["content"] == "预算 300 以内"


def test_report_book_stays_closed_after_round_trip(tmp_path):
    store = HistoryStore(tmp_path / "history.db", counter=len)
    store.initialize()
    engine = ContextEngine(system_prompt="你是侍酒师。", counter=len)
    compactor = NullCompactor()

    run_one_turn(store, engine, compactor, "第一问", "turn-1")
    report = run_one_turn(store, engine, compactor, "第二问", "turn-2")

    assert report["total_tokens"] == (
        report["message_tokens"] + report["tool_tokens"]
    )
    assert sum(
        usage["used"] for usage in report["components"].values()
    ) == report["total_tokens"]


def test_duplicate_turn_id_does_not_duplicate_history(tmp_path):
    store = HistoryStore(tmp_path / "history.db", counter=len)
    store.initialize()
    engine = ContextEngine(system_prompt="你是侍酒师。", counter=len)
    compactor = NullCompactor()

    run_one_turn(store, engine, compactor, "第一问", "turn-1")
    run_one_turn(store, engine, compactor, "重复提交", "turn-1")

    assert store.count_turns("session-1") == 1
