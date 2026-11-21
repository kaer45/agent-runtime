"""历史压缩的共享指标与决策。

``analyze`` / ``decide`` 同时被 ``ContextEngine``（只报 issue）和
``HistoryCompactor``（真正压缩）使用，两边不得各写一套触发顺序。

决策顺序（与 strategy §5.3 一致）：

1. ``last6_tokens > history_ceiling`` → ``FLOOR_UNMET``
2. ``active_turns > 6`` 且 ``history_utilization >= 0.60`` → ``COMPACT``
3. ``active_turns == 6`` 且 ``history_utilization >= 0.60`` → ``OVER_TARGET``
4. 其余 → ``NOOP``
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from history.models import TurnRecord

# 压缩永远保留的最近完整轮数。
COMPACTION_FLOOR_TURNS = 6

# 未覆盖历史达到 history_ceiling 的这个比例才考虑压缩。
COMPACTION_TARGET_UTILIZATION = 0.60


@dataclass(frozen=True)
class HistoryMetrics:
    """一次 load 出来的历史指标。"""

    active_turns: int
    history_tokens: int
    last6_tokens: int
    history_utilization: float


class CompactionDecision(str, Enum):
    FLOOR_UNMET = "floor_unmet"
    COMPACT = "compact"
    OVER_TARGET = "over_target"
    NOOP = "noop"


def analyze(turns: Sequence[TurnRecord], ceiling: int) -> HistoryMetrics:
    """计算触发指标。

    ``history_utilization`` 不做 clamp：大于 1.0 表示未覆盖历史
    已经超过 ``history_ceiling``。
    """
    if isinstance(ceiling, bool) or not isinstance(ceiling, int):
        raise TypeError("history_ceiling 必须是整数")
    if ceiling <= 0:
        raise ValueError("history_ceiling 必须大于 0")

    turns = tuple(turns)
    if not all(isinstance(turn, TurnRecord) for turn in turns):
        raise TypeError("analyze 只接受 TurnRecord")

    history_tokens = sum(turn.token_count for turn in turns)
    last6_tokens = sum(
        turn.token_count for turn in turns[-COMPACTION_FLOOR_TURNS:]
    )

    return HistoryMetrics(
        active_turns=len(turns),
        history_tokens=history_tokens,
        last6_tokens=last6_tokens,
        history_utilization=history_tokens / ceiling,
    )


def decide(metrics: HistoryMetrics, ceiling: int) -> CompactionDecision:
    """按固定顺序给出压缩决策。

    ``ceiling`` 用于判定 ``FLOOR_UNMET``：它只看最近 6 轮，不看总 history。
    """
    if not isinstance(metrics, HistoryMetrics):
        raise TypeError("decide 只接受 HistoryMetrics")
    if isinstance(ceiling, bool) or not isinstance(ceiling, int):
        raise TypeError("history_ceiling 必须是整数")
    if ceiling <= 0:
        raise ValueError("history_ceiling 必须大于 0")

    if metrics.last6_tokens > ceiling:
        return CompactionDecision.FLOOR_UNMET

    if (
        metrics.active_turns > COMPACTION_FLOOR_TURNS
        and metrics.history_utilization >= COMPACTION_TARGET_UTILIZATION
    ):
        return CompactionDecision.COMPACT

    if (
        metrics.active_turns == COMPACTION_FLOOR_TURNS
        and metrics.history_utilization >= COMPACTION_TARGET_UTILIZATION
    ):
        return CompactionDecision.OVER_TARGET

    return CompactionDecision.NOOP
