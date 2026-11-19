import asyncio
from types import SimpleNamespace

import pytest

from context.models import ContextLimits
from core.issues import ContextIssueCode, IssueLevel
from history import compactor as compactor_module
from history.compactor import CompactionResult, HistoryCompactor
from history.errors import (
    HistoryCoverageConflict,
    HistoryFloorUnmet,
    HistoryHardCapExceeded,
    HistoryStoreError,
    SummaryError,
)
from history.metrics import CompactionDecision
from history.turn_store import HistoryStore
from history.turn_summarizer import HistorySummarizer
from history.turns import RollingSummary

SESSION = "session-1"


# --------------------------------------------------------------------- 构造


def turn_messages(size: int) -> list[dict]:
    """构造 ``token_count`` 恰好为 ``size`` 的一轮（counter=len）。"""
    return [
        {"role": "user", "content": "u" * (size - 8)},
        {"role": "assistant", "content": ""},
    ]


def response(content: str = "新摘要", finish_reason: str = "stop"):
    return SimpleNamespace(
        message=SimpleNamespace(content=content),
        finish_reason=finish_reason,
        usage=None,
    )


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat_with_meta(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RecordingSummarizer(HistorySummarizer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.updated_seqs: list[tuple[int, ...]] = []

    async def update(self, previous, turns):
        self.updated_seqs.append(tuple(turn.seq for turn in turns))
        return await super().update(previous, turns)


class FailingSummarizer(HistorySummarizer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.update_calls = 0

    async def update(self, previous, turns):
        self.update_calls += 1
        raise SummaryError("stub 摘要失败")


@pytest.fixture
def store(tmp_path) -> HistoryStore:
    instance = HistoryStore(tmp_path / "history.db", counter=len)
    instance.initialize()
    return instance


def append_turns(store: HistoryStore, count: int, size: int = 100) -> None:
    for seq in range(1, count + 1):
        assert store.append_turn_once(
            SESSION, f"turn-{seq}", turn_messages(size)
        )


def make_compactor(
    store: HistoryStore,
    *,
    ceiling: int = 1_000,
    summarizer: HistorySummarizer | None = None,
    responses=None,
) -> HistoryCompactor:
    if summarizer is None:
        llm = FakeLLM(responses if responses is not None else [response()])
        summarizer = HistorySummarizer(llm, counter=len)

    return HistoryCompactor(
        store=store,
        summarizer=summarizer,
        limits=ContextLimits(history_ceiling=ceiling),
        counter=len,
    )


def run(coro, timeout: float = 5.0):
    async def wrapper():
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(wrapper())


# --------------------------------------------------------------------- 构造校验


def test_compactor_rejects_non_store(store):
    with pytest.raises(TypeError, match="store"):
        HistoryCompactor(
            store=object(),
            summarizer=HistorySummarizer(FakeLLM([])),
            limits=ContextLimits(),
        )


def test_compactor_rejects_zero_history_ceiling(store):
    with pytest.raises(ValueError, match="history_ceiling"):
        HistoryCompactor(
            store=store,
            summarizer=HistorySummarizer(FakeLLM([]), counter=len),
            limits=ContextLimits(history_ceiling=0),
        )


# --------------------------------------------------------------------- 决策


def test_floor_unmet_skips_compaction(store):
    append_turns(store, 6, size=30)  # 180 tokens > ceiling 100

    result = run(make_compactor(store, ceiling=100).after_turn(SESSION))

    assert result.decision is CompactionDecision.FLOOR_UNMET
    assert result.compacted is False
    assert result.covered_through_seq == 0
    assert result.issues == ()
    assert result.metrics.last6_tokens == 180
    assert store.load(SESSION).summary.text == ""


def test_noop_when_under_target(store):
    append_turns(store, 3, size=100)

    result = run(make_compactor(store, ceiling=10_000).after_turn(SESSION))

    assert result.decision is CompactionDecision.NOOP
    assert result.compacted is False
    assert result.issues == ()


def test_compact_advances_coverage_and_keeps_floor(store):
    append_turns(store, 10, size=100)  # history = 1,000 = ceiling

    result = run(make_compactor(store, ceiling=1_000).after_turn(SESSION))

    assert result.decision is CompactionDecision.COMPACT
    assert result.compacted is True
    assert result.covered_through_seq == 4
    assert result.metrics.active_turns == 10
    assert result.issues == ()

    snapshot = store.load(SESSION)
    assert [turn.seq for turn in snapshot.turns] == [5, 6, 7, 8, 9, 10]
    assert snapshot.summary.text == "新摘要"
    assert snapshot.covered_through_seq == 4


def test_compact_targets_only_turns_before_floor(store):
    append_turns(store, 12, size=100)
    summarizer = RecordingSummarizer(FakeLLM([response()]), counter=len)

    result = run(
        make_compactor(
            store, ceiling=1_000, summarizer=summarizer
        ).after_turn(SESSION)
    )

    assert summarizer.updated_seqs == [(1, 2, 3, 4, 5, 6)]
    assert result.covered_through_seq == 6
    assert len(store.load(SESSION).turns) == 6


def test_over_target_reports_warning_without_compacting(store):
    append_turns(store, 6, size=150)  # 900 / 1,000 = 90%

    result = run(make_compactor(store, ceiling=1_000).after_turn(SESSION))

    assert result.decision is CompactionDecision.OVER_TARGET
    assert result.compacted is False
    assert result.covered_through_seq == 0
    assert len(result.issues) == 1

    issue = result.issues[0]
    assert issue.code is ContextIssueCode.HISTORY_OVER_TARGET
    assert issue.level is IssueLevel.WARNING


# --------------------------------------------------------------------- 失败路径


def test_summary_failure_keeps_coverage(store):
    append_turns(store, 10, size=100)
    summarizer = FailingSummarizer(FakeLLM([]), counter=len)

    result = run(
        make_compactor(
            store, ceiling=1_000, summarizer=summarizer
        ).after_turn(SESSION)
    )

    assert result.decision is CompactionDecision.COMPACT
    assert result.compacted is False
    assert result.covered_through_seq == 0
    assert [issue.code for issue in result.issues] == [
        ContextIssueCode.SUMMARY_GENERATION_FAILED
    ]
    assert result.issues[0].level is IssueLevel.WARNING
    assert result.issues[0].details["stage"] == "generate"
    assert summarizer.update_calls == 1

    snapshot = store.load(SESSION)
    assert snapshot.summary.text == ""
    assert len(snapshot.turns) == 10


def test_save_failure_keeps_coverage(store, monkeypatch):
    append_turns(store, 10, size=100)

    def boom(*args, **kwargs):
        raise HistoryStoreError("disk full")

    monkeypatch.setattr(store, "save_summary_cas", boom)

    result = run(make_compactor(store, ceiling=1_000).after_turn(SESSION))

    assert result.compacted is False
    assert result.covered_through_seq == 0
    assert [issue.code for issue in result.issues] == [
        ContextIssueCode.SUMMARY_GENERATION_FAILED
    ]
    assert result.issues[0].details["stage"] == "save"


def test_cas_conflict_keeps_coverage_and_records_info(store, monkeypatch):
    append_turns(store, 10, size=100)
    store.save_summary_cas(
        SESSION,
        RollingSummary(text="旧摘要"),
        covered_through_seq=3,
        expected_covered_through_seq=0,
    )

    def conflict(*args, **kwargs):
        raise HistoryCoverageConflict("coverage moved")

    monkeypatch.setattr(store, "save_summary_cas", conflict)

    result = run(make_compactor(store, ceiling=1_000).after_turn(SESSION))

    assert result.compacted is False
    assert result.covered_through_seq == 3
    assert [issue.code for issue in result.issues] == [
        ContextIssueCode.SUMMARY_CAS_CONFLICT
    ]
    assert result.issues[0].level is IssueLevel.INFO
    assert store.load(SESSION).summary.text == "旧摘要"


def test_compaction_trims_to_fitting_prefix(store):
    append_turns(store, 10, size=100)
    snapshot = store.load(SESSION)
    candidates = snapshot.turns[:-6]

    probe = HistorySummarizer(FakeLLM([]), counter=len)
    fits_two = probe._prompt_tokens(snapshot.summary, candidates[:2])

    summarizer = RecordingSummarizer(
        FakeLLM([response()]), counter=len, input_ceiling=fits_two
    )

    result = run(
        make_compactor(
            store, ceiling=1_000, summarizer=summarizer
        ).after_turn(SESSION)
    )

    assert summarizer.updated_seqs == [(1, 2)]
    assert result.compacted is True
    assert result.covered_through_seq == 2
    assert len(store.load(SESSION).turns) == 8


def test_summary_input_too_long_skips_compaction(store):
    append_turns(store, 10, size=100)
    # input_ceiling=1：连一个 turn 都放不进。
    summarizer = HistorySummarizer(
        FakeLLM([response()]), counter=len, input_ceiling=1
    )

    result = run(
        make_compactor(
            store, ceiling=1_000, summarizer=summarizer
        ).after_turn(SESSION)
    )

    assert result.decision is CompactionDecision.COMPACT
    assert result.compacted is False
    assert result.covered_through_seq == 0
    assert [issue.code for issue in result.issues] == [
        ContextIssueCode.SUMMARY_INPUT_TOO_LONG
    ]
    assert result.issues[0].details["input_ceiling"] == 1


# --------------------------------------------------------------------- 归档


def test_soft_archive_removes_covered_turns(store, monkeypatch):
    monkeypatch.setattr(compactor_module, "SOFT_ARCHIVE_LIMIT", 5)
    append_turns(store, 5, size=100)
    store.save_summary_cas(
        SESSION,
        RollingSummary(text="旧摘要"),
        covered_through_seq=2,
        expected_covered_through_seq=0,
    )

    result = run(make_compactor(store, ceiling=10_000).after_turn(SESSION))

    assert result.issues == ()
    assert [turn.seq for turn in store.load(SESSION).turns] == [3, 4, 5]


def test_soft_archive_failure_adds_warning(store, monkeypatch):
    monkeypatch.setattr(compactor_module, "SOFT_ARCHIVE_LIMIT", 5)
    append_turns(store, 5, size=100)

    def boom(session_id):
        raise HistoryStoreError("archive locked")

    monkeypatch.setattr(store, "archive_covered", boom)

    result = run(make_compactor(store, ceiling=10_000).after_turn(SESSION))

    assert [issue.code for issue in result.issues] == [
        ContextIssueCode.HISTORY_ARCHIVE_FAILED
    ]
    assert result.issues[0].level is IssueLevel.WARNING
    assert result.issues[0].details["stage"] == "soft_archive"


# --------------------------------------------------------------------- 硬上限


def test_ensure_hard_cap_noop_below_limit(store):
    append_turns(store, 3, size=100)
    compactor = make_compactor(store, ceiling=10_000)

    assert run(compactor.ensure_hard_cap(SESSION)) is None
    assert len(store.load(SESSION).turns) == 3


def test_ensure_hard_cap_archives_before_compacting(store, monkeypatch):
    monkeypatch.setattr(compactor_module, "HARD_ARCHIVE_LIMIT", 5)
    append_turns(store, 5, size=100)
    store.save_summary_cas(
        SESSION,
        RollingSummary(text="旧摘要"),
        covered_through_seq=2,
        expected_covered_through_seq=0,
    )
    summarizer = RecordingSummarizer(FakeLLM([]), counter=len)

    run(
        make_compactor(
            store, ceiling=10_000, summarizer=summarizer
        ).ensure_hard_cap(SESSION)
    )

    assert summarizer.updated_seqs == []
    assert [turn.seq for turn in store.load(SESSION).turns] == [3, 4, 5]


def test_ensure_hard_cap_force_compacts_above_floor(store, monkeypatch):
    monkeypatch.setattr(compactor_module, "HARD_ARCHIVE_LIMIT", 7)
    append_turns(store, 7, size=100)
    summarizer = RecordingSummarizer(FakeLLM([response()]), counter=len)

    run(
        make_compactor(
            store, ceiling=10_000, summarizer=summarizer
        ).ensure_hard_cap(SESSION)
    )

    assert summarizer.updated_seqs == [(1,)]
    assert [turn.seq for turn in store.load(SESSION).turns] == [
        2,
        3,
        4,
        5,
        6,
        7,
    ]


def test_ensure_hard_cap_raises_floor_unmet(store, monkeypatch):
    monkeypatch.setattr(compactor_module, "HARD_ARCHIVE_LIMIT", 6)
    append_turns(store, 7, size=30)  # 最近 6 轮 180 > ceiling 100
    compactor = make_compactor(store, ceiling=100)

    with pytest.raises(HistoryFloorUnmet):
        run(compactor.ensure_hard_cap(SESSION))


def test_ensure_hard_cap_raises_when_still_over(store, monkeypatch):
    monkeypatch.setattr(compactor_module, "HARD_ARCHIVE_LIMIT", 6)
    append_turns(store, 10, size=10)
    compactor = make_compactor(store, ceiling=10_000)

    with pytest.raises(HistoryHardCapExceeded):
        run(compactor.ensure_hard_cap(SESSION))


def test_after_turn_uses_locked_hard_cap(store, monkeypatch):
    """``after_turn`` 已持锁，必须走无锁版本，否则这里会超时。"""
    monkeypatch.setattr(compactor_module, "HARD_ARCHIVE_LIMIT", 6)
    append_turns(store, 10, size=10)
    compactor = make_compactor(store, ceiling=10_000)

    with pytest.raises(HistoryHardCapExceeded):
        run(compactor.after_turn(SESSION))


# --------------------------------------------------------------------- 并发


def test_after_turn_is_serialized_per_session(store):
    append_turns(store, 10, size=100)
    summarizer = RecordingSummarizer(
        FakeLLM([response(), response()]), counter=len
    )
    compactor = make_compactor(store, ceiling=1_000, summarizer=summarizer)

    async def run_both():
        return await asyncio.gather(
            compactor.after_turn(SESSION),
            compactor.after_turn(SESSION),
        )

    first, second = run(run_both())

    assert first.compacted is True
    assert second.decision is CompactionDecision.OVER_TARGET
    assert second.compacted is False
    assert summarizer.updated_seqs == [(1, 2, 3, 4)]

    snapshot = store.load(SESSION)
    assert [turn.seq for turn in snapshot.turns] == [5, 6, 7, 8, 9, 10]


def test_result_is_frozen(store):
    append_turns(store, 3, size=100)
    result = run(make_compactor(store, ceiling=10_000).after_turn(SESSION))

    assert isinstance(result, CompactionResult)
    with pytest.raises(AttributeError):
        result.compacted = True
