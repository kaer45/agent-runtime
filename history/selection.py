"""从最新 turn 往前选连续后缀，供历史组件渲染。

与 ``HistorySummarizer.fit_turns`` 方向相反：那个是为摘要输入从**最老**取
连续前缀，这里是历史渲染从**最新**取连续后缀。两者不能共用。

规则（protocol §4.6）：

- 只选完整 turn，不切 turn、不切 tool 组；
- 遇到第一个放不下的 turn 就停止，保证结果是连续后缀；
- 丢弃的 turn 记 seq，供报告和排障使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from core.tokens import TokenCounter, count_messages
from history.turns import TurnRecord


@dataclass(frozen=True)
class TurnSelection:
    """一次历史渲染的挑选结果。"""

    selected: tuple[TurnRecord, ...]
    dropped_seqs: tuple[int, ...]
    used_tokens: int
    truncated: bool


def select_recent_turns(
    turns: Sequence[TurnRecord],
    ceiling: int,
    counter: TokenCounter,
) -> TurnSelection:
    """按 ``ceiling`` 从最新往老选连续后缀。"""
    if isinstance(ceiling, bool) or not isinstance(ceiling, int):
        raise TypeError("history_ceiling 必须是整数")
    if ceiling < 0:
        raise ValueError("history_ceiling 不能为负数")

    turns = tuple(turns)
    if not all(isinstance(turn, TurnRecord) for turn in turns):
        raise TypeError("select_recent_turns 只接受 TurnRecord")

    selected: list[TurnRecord] = []
    used = 0
    dropped: list[int] = []

    for turn in reversed(turns):
        cost = count_messages(counter, turn.messages)
        if used + cost > ceiling:
            # 连续后缀：第一个放不下就停，更老的都不再尝试。
            dropped = [item.seq for item in turns[: len(turns) - len(selected)]]
            break
        selected.append(turn)
        used += cost

    selected.reverse()
    return TurnSelection(
        selected=tuple(selected),
        dropped_seqs=tuple(sorted(dropped, reverse=True)),
        used_tokens=used,
        truncated=bool(dropped),
    )
