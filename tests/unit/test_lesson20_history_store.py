import tempfile
from pathlib import Path

import pytest

from context.history import (
    HistorySnapshot,
    HistoryStore,
    HistoryStoreError,
    HistoryTrimConflictError,
    HistoryTrimError,
)
from context.summarizer import RollingSummary
from state.models import SessionState
from state.store import StateStore


@pytest.fixture
def store() -> HistoryStore:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson20-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    history = HistoryStore(db_path, max_messages_per_session=10)
    history.initialize()

    try:
        yield history
    finally:
        db_path.unlink(missing_ok=True)


def sample_messages():
    return [
        {"role": "user", "content": "我喜欢干型酒"},
        {"role": "assistant", "content": "可以看波尔多。"},
    ]


def append_messages(store, count: int):
    return store.append(
        "session-1",
        [
            {"role": "user", "content": f"message-{index}"}
            for index in range(count)
        ],
    )


def test_missing_session_returns_empty_snapshot(store):
    snapshot = store.load("session-1")

    assert isinstance(snapshot, HistorySnapshot)
    assert snapshot.messages == []
    assert snapshot.message_ids == []
    assert snapshot.summary == RollingSummary()
    assert snapshot.covered_through_id is None
    assert store.count("session-1") == 0


def test_append_and_load_preserves_message_payloads(store):
    messages = [
        {"role": "user", "content": "帮我查天气"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "晴"},
        {"role": "assistant", "content": "今天晴天。"},
    ]

    store.append("session-1", messages)

    snapshot = store.load("session-1")
    assert snapshot.messages == messages
    assert len(snapshot.message_ids) == len(messages)
    assert snapshot.message_ids == sorted(snapshot.message_ids)
    assert store.count("session-1") == 4


def test_messages_stay_ordered_across_appends(store):
    store.append("session-1", [{"role": "user", "content": "第一轮"}])
    store.append("session-1", [{"role": "assistant", "content": "回答一"}])
    store.append("session-1", [{"role": "user", "content": "第二轮"}])

    snapshot = store.load("session-1")

    assert [message["content"] for message in snapshot.messages] == [
        "第一轮",
        "回答一",
        "第二轮",
    ]
    assert snapshot.message_ids == sorted(snapshot.message_ids)


def test_history_persists_across_store_instances(store):
    store.append("session-1", sample_messages())

    reopened = HistoryStore(store.db_path, max_messages_per_session=10)
    reopened.initialize()

    assert reopened.load("session-1").messages == sample_messages()


def test_summary_round_trip(store):
    summary = RollingSummary(text="用户喜欢干型酒", covered_messages=4)

    store.save_summary("session-1", summary)

    snapshot = store.load("session-1")
    assert snapshot.summary == summary
    assert snapshot.covered_through_id is None


def test_summary_is_upserted(store):
    store.save_summary("session-1", RollingSummary(text="旧摘要", covered_messages=2))
    store.save_summary("session-1", RollingSummary(text="新摘要", covered_messages=5))

    assert store.load("session-1").summary == RollingSummary(
        text="新摘要",
        covered_messages=5,
    )


def test_append_does_not_trim(store):
    history = HistoryStore(store.db_path, max_messages_per_session=3)
    history.initialize()

    append_messages(history, 5)

    assert history.count("session-1") == 5


def test_plan_trim_is_read_only_and_returns_oldest(store):
    messages = [
        {"role": "user", "content": f"message-{index}"}
        for index in range(5)
    ]
    store.append("session-1", messages)

    plan = store.plan_trim("session-1", keep_last=2)

    assert plan.drop_through_id == 3
    assert plan.drop_count == 3
    assert plan.covered_count == 0
    assert plan.uncovered_count == 3
    assert plan.uncovered_ids == (1, 2, 3)
    assert [message["content"] for message in plan.uncovered_messages] == [
        "message-0",
        "message-1",
        "message-2",
    ]
    assert plan.remaining == 2
    assert store.count("session-1") == 5
    assert store.load("session-1").messages == messages


def test_plan_trim_is_empty_when_under_limit(store):
    store.append("session-1", sample_messages())

    plan = store.plan_trim("session-1", keep_last=3)

    assert plan.is_empty is True
    assert plan.drop_through_id is None
    assert plan.drop_count == 0
    assert plan.remaining == 2


def test_plan_trim_rejects_invalid_keep_last(store):
    with pytest.raises(ValueError, match="keep_last"):
        store.plan_trim("session-1", keep_last=0)


def test_commit_trim_deletes_messages_and_saves_summary(store):
    messages = [
        {"role": "user", "content": f"message-{index}"}
        for index in range(5)
    ]
    store.append("session-1", messages)
    plan = store.plan_trim("session-1", keep_last=2)
    summary = RollingSummary(text="覆盖前三条", covered_messages=3)

    store.commit_trim(plan, summary)

    snapshot = store.load("session-1")
    assert [message["content"] for message in snapshot.messages] == [
        "message-3",
        "message-4",
    ]
    assert snapshot.summary == summary
    assert snapshot.covered_through_id == 3
    assert store.count("session-1") == 2


def test_commit_trim_rejects_empty_summary_without_deleting(store):
    append_messages(store, 5)
    plan = store.plan_trim("session-1", keep_last=2)

    with pytest.raises(HistoryTrimError, match="摘要"):
        store.commit_trim(plan, RollingSummary())

    assert store.count("session-1") == 5
    assert store.load("session-1").covered_through_id is None


def test_commit_trim_stale_plan_conflicts_without_partial_write(store):
    append_messages(store, 5)
    plan = store.plan_trim("session-1", keep_last=2)
    store.clear("session-1")

    with pytest.raises(HistoryTrimConflictError, match="删除消息数量不匹配"):
        store.commit_trim(plan, RollingSummary(text="摘要", covered_messages=3))

    snapshot = store.load("session-1")
    assert snapshot.messages == []
    assert snapshot.summary == RollingSummary()


def test_covered_through_id_persists(store):
    append_messages(store, 4)
    plan = store.plan_trim("session-1", keep_last=1)
    store.commit_trim(plan, RollingSummary(text="覆盖", covered_messages=3))

    reopened = HistoryStore(store.db_path, max_messages_per_session=10)
    reopened.initialize()

    assert reopened.load("session-1").covered_through_id == 3


def test_save_summary_preserves_coverage(store):
    append_messages(store, 4)
    plan = store.plan_trim("session-1", keep_last=1)
    store.commit_trim(plan, RollingSummary(text="旧摘要", covered_messages=3))

    store.save_summary(
        "session-1",
        RollingSummary(text="新摘要", covered_messages=5),
    )

    snapshot = store.load("session-1")
    assert snapshot.summary.text == "新摘要"
    assert snapshot.covered_through_id == 3


def test_save_summary_can_advance_coverage_without_deleting(store):
    append_messages(store, 5)
    summary = RollingSummary(text="覆盖全部 5 条", covered_messages=5)

    store.save_summary(
        "session-1",
        summary,
        covered_through_id=5,
    )

    snapshot = store.load("session-1")
    assert snapshot.summary == summary
    assert snapshot.covered_through_id == 5
    assert store.count("session-1") == 5


def test_save_summary_rejects_coverage_regression(store):
    append_messages(store, 5)
    store.save_summary(
        "session-1",
        RollingSummary(text="覆盖全部", covered_messages=5),
        covered_through_id=5,
    )

    with pytest.raises(ValueError, match="倒退"):
        store.save_summary(
            "session-1",
            RollingSummary(text="覆盖前三条", covered_messages=3),
            covered_through_id=3,
        )


def test_save_summary_rejects_coverage_beyond_max_message_id(store):
    append_messages(store, 2)

    with pytest.raises(ValueError, match="超过最大"):
        store.save_summary(
            "session-1",
            RollingSummary(text="摘要", covered_messages=3),
            covered_through_id=3,
        )


def test_plan_trim_splits_covered_and_uncovered(store):
    append_messages(store, 5)
    store.save_summary(
        "session-1",
        RollingSummary(text="覆盖前两条", covered_messages=2),
        covered_through_id=2,
    )

    plan = store.plan_trim("session-1", keep_last=2)

    assert plan.drop_through_id == 3
    assert plan.drop_count == 3
    assert plan.covered_count == 2
    assert plan.uncovered_count == 1
    assert plan.uncovered_ids == (3,)
    assert [message["content"] for message in plan.uncovered_messages] == [
        "message-2"
    ]
    assert plan.target_covered_through_id == 3


def test_commit_trim_covered_only_does_not_need_summary(store):
    append_messages(store, 5)
    summary = RollingSummary(text="覆盖前三条", covered_messages=3)
    store.save_summary("session-1", summary, covered_through_id=3)
    plan = store.plan_trim("session-1", keep_last=2)

    assert plan.uncovered_count == 0
    store.commit_trim(plan, summary=None)

    snapshot = store.load("session-1")
    assert [message["content"] for message in snapshot.messages] == [
        "message-3",
        "message-4",
    ]
    assert snapshot.summary == summary
    assert snapshot.covered_through_id == 3


def test_commit_trim_mixed_only_summarizes_uncovered(store):
    append_messages(store, 5)
    store.save_summary(
        "session-1",
        RollingSummary(text="覆盖前两条", covered_messages=2),
        covered_through_id=2,
    )
    plan = store.plan_trim("session-1", keep_last=1)

    assert plan.covered_count == 2
    assert plan.uncovered_ids == (3, 4)
    store.commit_trim(
        plan,
        RollingSummary(text="覆盖到第四条", covered_messages=4),
    )

    snapshot = store.load("session-1")
    assert snapshot.messages == [{"role": "user", "content": "message-4"}]
    assert snapshot.covered_through_id == 4


def test_load_limit_returns_latest_messages_in_order(store):
    store.append(
        "session-1",
        [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "2"},
            {"role": "user", "content": "3"},
        ],
    )

    snapshot = store.load("session-1", limit=2)

    assert [message["content"] for message in snapshot.messages] == ["2", "3"]
    assert len(snapshot.message_ids) == 2


def test_clear_removes_messages_summary_and_coverage(store):
    store.append("session-1", sample_messages())
    store.save_summary(
        "session-1",
        RollingSummary(text="摘要", covered_messages=2),
        covered_through_id=2,
    )

    assert store.clear("session-1") is True
    assert store.load("session-1").messages == []
    assert store.load("session-1").summary == RollingSummary()
    assert store.load("session-1").covered_through_id is None
    assert store.clear("session-1") is False


def test_state_and_history_can_share_the_same_database(store):
    state_store = StateStore(store.db_path)
    state_store.initialize()
    state_store.save("session-1", SessionState(preferences={"style": "dry"}))
    store.append("session-1", sample_messages())

    reopened_state = StateStore(store.db_path)
    reopened_state.initialize()
    reopened_history = HistoryStore(store.db_path, max_messages_per_session=10)
    reopened_history.initialize()

    assert reopened_state.load("session-1").preferences == {"style": "dry"}
    assert reopened_history.load("session-1").messages == sample_messages()


@pytest.mark.parametrize("session_id", ["", "   "])
def test_blank_session_id_is_rejected(store, session_id):
    with pytest.raises(ValueError, match="session_id"):
        store.load(session_id)


def test_invalid_message_shape_is_rejected(store):
    with pytest.raises(TypeError, match="messages"):
        store.append("session-1", ["not-a-dict"])

    with pytest.raises(ValueError, match="role"):
        store.append("session-1", [{"role": "developer", "content": "x"}])


def test_invalid_retention_limit_is_rejected():
    with pytest.raises(ValueError, match="max_messages_per_session"):
        HistoryStore("unused.db", max_messages_per_session=0)


def test_corrupted_summary_is_reported(store):
    with store._connection() as conn:
        conn.execute(
            "INSERT INTO session_summaries "
            "(session_id, summary_json, covered_through_id, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("session-1", "not-json", None, "2026-01-01T00:00:00+00:00"),
        )

    with pytest.raises(HistoryStoreError, match="损坏"):
        store.load("session-1")
