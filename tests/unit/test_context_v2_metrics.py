import pytest

from history.metrics import (
    COMPACTION_FLOOR_TURNS,
    COMPACTION_TARGET_UTILIZATION,
    CompactionDecision,
    HistoryMetrics,
    analyze,
    decide,
)
from history.models import TurnRecord

CEILING = 80_000


def make_turns(token_counts: list[int]) -> tuple[TurnRecord, ...]:
    return tuple(
        TurnRecord(
            turn_id=f"turn-{index}",
            seq=index,
            messages=({"role": "user", "content": f"q{index}"},),
            token_count=tokens,
            created_at="2026-11-17T00:00:00+00:00",
        )
        for index, tokens in enumerate(token_counts, start=1)
    )


def test_frozen_constants():
    assert COMPACTION_FLOOR_TURNS == 6
    assert COMPACTION_TARGET_UTILIZATION == 0.60


def test_analyze_of_empty_history_is_all_zero():
    metrics = analyze((), CEILING)

    assert metrics == HistoryMetrics(
        active_turns=0,
        history_tokens=0,
        last6_tokens=0,
        history_utilization=0.0,
    )


def test_analyze_only_sums_the_last_six_turns_for_last6():
    turns = make_turns([1_000] * 3 + [10_000] * 6)

    metrics = analyze(turns, CEILING)

    assert metrics.active_turns == 9
    assert metrics.history_tokens == 3_000 + 60_000
    assert metrics.last6_tokens == 60_000


def test_analyze_uses_all_turns_when_fewer_than_six():
    turns = make_turns([1_000, 2_000, 3_000])

    metrics = analyze(turns, CEILING)

    assert metrics.last6_tokens == 6_000


def test_analyze_does_not_clamp_utilization():
    turns = make_turns([10_000] * 20)

    metrics = analyze(turns, CEILING)

    assert metrics.history_utilization == pytest.approx(2.5)


@pytest.mark.parametrize("ceiling", [0, -1])
def test_analyze_rejects_bad_ceiling(ceiling):
    with pytest.raises(ValueError, match="history_ceiling"):
        analyze((), ceiling)


def test_analyze_rejects_non_turn_records():
    with pytest.raises(TypeError, match="TurnRecord"):
        analyze(({"seq": 1},), CEILING)


@pytest.mark.parametrize(
    ("token_counts", "expected"),
    [
        # 空历史：不动作。
        ([], CompactionDecision.NOOP),
        # 6 轮、37.5%：没到目标水位。
        ([5_000] * 6, CompactionDecision.NOOP),
        # 6 轮、75%：到目标水位，但受 floor 限制，只报告。
        ([10_000] * 6, CompactionDecision.OVER_TARGET),
        # 7 轮、87.5%，最近 6 轮 60K：压缩。
        ([10_000] * 7, CompactionDecision.COMPACT),
        # 最近 6 轮正好 60%，边界按 >= 处理。
        ([5_000] * 6 + [18_000], CompactionDecision.COMPACT),
        # 最近 6 轮 81K 超过 ceiling：floor 优先于压缩。
        ([13_500] * 6, CompactionDecision.FLOOR_UNMET),
        # 更老的历史再多也不能绕过 floor。
        ([1_000] * 5 + [13_500] * 6, CompactionDecision.FLOOR_UNMET),
        # active_turns < 6 且超水位：已知边界，不产生 OVER_TARGET。
        ([10_000] * 5, CompactionDecision.NOOP),
    ],
)
def test_decision_table(token_counts, expected):
    metrics = analyze(make_turns(token_counts), CEILING)

    assert decide(metrics, CEILING) is expected


def test_decide_rejects_bad_metrics_type():
    with pytest.raises(TypeError, match="HistoryMetrics"):
        decide({"active_turns": 0}, CEILING)


def test_decide_rejects_bad_ceiling():
    metrics = analyze((), CEILING)

    with pytest.raises(ValueError, match="history_ceiling"):
        decide(metrics, 0)
