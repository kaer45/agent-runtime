"""turn 级历史的值对象。

v2 的存储粒度是 turn，不是 message：

- ``TurnRecord``：一轮完整对话；
- ``RollingSummary``：全程唯一的滚动摘要，只有文本；
- ``HistorySnapshot``：一次 ``load`` 的结果，只含未覆盖 turn。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnRecord:
    """一轮完整对话。

    ``token_count`` 由 ``TokenBudget.count_messages`` 在写入时算好，
    读出来直接用于指标计算，不再重算。
    """

    turn_id: str
    seq: int
    messages: tuple[dict, ...]
    token_count: int
    created_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.turn_id, str) or not self.turn_id.strip():
            raise ValueError("TurnRecord.turn_id 不能为空")

        if isinstance(self.seq, bool) or not isinstance(self.seq, int):
            raise TypeError("TurnRecord.seq 必须是整数")
        if self.seq < 1:
            raise ValueError(f"TurnRecord.seq 必须 >= 1，收到 {self.seq}")

        messages = self.messages
        if isinstance(messages, list):
            messages = tuple(messages)
        if not isinstance(messages, tuple):
            raise TypeError("TurnRecord.messages 必须是 tuple[dict, ...]")
        if not messages:
            raise ValueError("TurnRecord.messages 不能为空")
        if not all(isinstance(message, dict) for message in messages):
            raise TypeError("TurnRecord.messages 的元素必须是 dict")
        object.__setattr__(self, "messages", messages)

        if isinstance(self.token_count, bool) or not isinstance(
            self.token_count, int
        ):
            raise TypeError("TurnRecord.token_count 必须是整数")
        if self.token_count <= 0:
            raise ValueError("TurnRecord.token_count 必须大于 0")

        if not isinstance(self.created_at, str) or not self.created_at.strip():
            raise ValueError("TurnRecord.created_at 不能为空")


@dataclass(frozen=True)
class RollingSummary:
    """滚动摘要。

    只有文本：覆盖位置由 ``covered_through_seq`` 表达，生成层是否被截断
    只记日志，不进存储模型。
    """

    text: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("RollingSummary.text 必须是字符串")


@dataclass(frozen=True)
class HistorySnapshot:
    """一次 ``load`` 的结果。

    ``turns`` 只包含 ``seq > covered_through_seq`` 的轮次，按 seq 升序，
    且不含已归档 turn。
    """

    turns: tuple[TurnRecord, ...]
    summary: RollingSummary
    covered_through_seq: int

    def __post_init__(self) -> None:
        turns = self.turns
        if isinstance(turns, list):
            turns = tuple(turns)
        if not isinstance(turns, tuple):
            raise TypeError("HistorySnapshot.turns 必须是 tuple[TurnRecord, ...]")
        if not all(isinstance(turn, TurnRecord) for turn in turns):
            raise TypeError("HistorySnapshot.turns 的元素必须是 TurnRecord")
        object.__setattr__(self, "turns", turns)

        if not isinstance(self.summary, RollingSummary):
            raise TypeError("HistorySnapshot.summary 必须是 RollingSummary")

        coverage = self.covered_through_seq
        if isinstance(coverage, bool) or not isinstance(coverage, int):
            raise TypeError("HistorySnapshot.covered_through_seq 必须是整数")
        if coverage < 0:
            raise ValueError("HistorySnapshot.covered_through_seq 不能为负数")

        seqs = [turn.seq for turn in turns]
        if len(set(seqs)) != len(seqs):
            raise ValueError("HistorySnapshot.turns 的 seq 不能重复")
        if seqs != sorted(seqs):
            raise ValueError("HistorySnapshot.turns 必须按 seq 升序")
        if any(seq <= coverage for seq in seqs):
            raise ValueError(
                "HistorySnapshot.turns 必须全部满足 seq > covered_through_seq"
            )
