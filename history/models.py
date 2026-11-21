"""turn 级历史的值对象。

v2 的存储粒度是 turn，不是 message：

- ``TurnRecord``：一轮完整对话；
- ``RollingSummary``：全程唯一的滚动摘要，只有文本；
- ``HistorySnapshot``：一次 ``load`` 的结果，只含未覆盖 turn。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from history.errors import HistoryTurnInvalid


@dataclass(frozen=True)
class TurnRecord:
    """一轮完整对话。

    ``token_count`` 由 ``core.tokens.count_messages`` 在写入时算好，
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


def validate_turn_messages(messages: Sequence[dict]) -> None:
    """校验一轮消息是否满足 turn 结构（strategy §4.5）。

    规则：

    - 非空；第一条是 ``user``；一轮只有一条 ``user``；
    - 最后一条是无 ``tool_calls`` 的 ``assistant``；
    - 不允许 ``system``；
    - ``assistant(tool_calls)`` 与后续 ``tool`` 结果一一配对，顺序一致；
    - 每条 ``tool`` 都必须带 ``tool_call_id``。
    """
    if not isinstance(messages, (tuple, list)):
        raise TypeError("messages 必须是消息序列")

    messages = tuple(messages)
    if not messages:
        raise HistoryTurnInvalid("turn 消息不能为空")
    if not all(isinstance(message, dict) for message in messages):
        raise HistoryTurnInvalid("turn 的每条消息都必须是 dict")

    roles = [message.get("role") for message in messages]
    if roles[0] != "user":
        raise HistoryTurnInvalid("turn 的第一条消息必须是 user")
    if roles.count("user") != 1:
        raise HistoryTurnInvalid("一轮只允许一条 user 消息")
    if "system" in roles:
        raise HistoryTurnInvalid("turn 内不允许 system 消息")

    last = messages[-1]
    if last.get("role") != "assistant" or last.get("tool_calls"):
        raise HistoryTurnInvalid(
            "turn 的最后一条必须是无 tool_calls 的 assistant"
        )

    index = 1
    while index < len(messages):
        message = messages[index]
        role = message.get("role")

        if role == "tool":
            raise HistoryTurnInvalid(
                "tool 消息必须紧跟在带 tool_calls 的 assistant 之后"
            )

        if role == "assistant" and message.get("tool_calls"):
            call_ids = _tool_call_ids(message["tool_calls"])

            cursor = index + 1
            tool_ids: list[str] = []
            while cursor < len(messages) and messages[cursor].get("role") == "tool":
                tool_id = messages[cursor].get("tool_call_id")
                if not isinstance(tool_id, str) or not tool_id:
                    raise HistoryTurnInvalid(
                        "每条 tool 消息都必须带非空 tool_call_id"
                    )
                tool_ids.append(tool_id)
                cursor += 1

            if tool_ids != call_ids:
                raise HistoryTurnInvalid(
                    "tool 结果必须与 tool_calls 一一对应且顺序一致"
                )

            index = cursor
            continue

        index += 1


def _tool_call_ids(tool_calls) -> list[str]:
    if not isinstance(tool_calls, (list, tuple)) or not tool_calls:
        raise HistoryTurnInvalid("assistant.tool_calls 必须是非空列表")

    call_ids: list[str] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            raise HistoryTurnInvalid("tool_calls 的每一项必须是 dict")

        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise HistoryTurnInvalid("每个 tool_call 都必须带非空 id")
        call_ids.append(call_id)

    if len(set(call_ids)) != len(call_ids):
        raise HistoryTurnInvalid("同一轮内 tool_call id 不能重复")

    return call_ids
