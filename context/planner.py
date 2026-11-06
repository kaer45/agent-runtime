"""在组装上下文之前分配 token 预算，并预裁剪历史消息。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from context.budget import BudgetExceededError, MESSAGE_OVERHEAD, TokenBudget
from context.builder import SUMMARY_HEADER
from context.summarizer import RollingSummary


@dataclass(frozen=True)
class ContextPlan:
    """一次请求的预算与历史选择结果。"""

    state_max_tokens: int
    summary_max_tokens: int
    summary_text_max_tokens: int
    history_max_tokens: int

    summary_tokens: int
    history_tokens: int

    kept_history: tuple[dict, ...]
    kept_message_ids: tuple[int, ...]
    dropped_history: tuple[dict, ...]
    dropped_message_ids: tuple[int, ...]


class ContextPlanner:
    """固定配额规划：state、summary、history 各自有上限。

    规划阶段不调用 LLM、不读数据库、不渲染状态。
    """

    def __init__(
        self,
        state_max_tokens: int = 600,
        summary_max_tokens: int = 400,
        safety_tokens: int = 64,
    ):
        if (
            not isinstance(state_max_tokens, int)
            or isinstance(state_max_tokens, bool)
            or state_max_tokens <= 0
        ):
            raise ValueError("state_max_tokens 必须是正整数")
        if (
            not isinstance(summary_max_tokens, int)
            or isinstance(summary_max_tokens, bool)
            or summary_max_tokens <= 0
        ):
            raise ValueError("summary_max_tokens 必须是正整数")
        if (
            not isinstance(safety_tokens, int)
            or isinstance(safety_tokens, bool)
            or safety_tokens < 0
        ):
            raise ValueError("safety_tokens 必须是非负整数")

        self.state_max_tokens = state_max_tokens
        self.summary_max_tokens = summary_max_tokens
        self.safety_tokens = safety_tokens

    def plan(
        self,
        user_input: str,
        system_prompt: str,
        summary: RollingSummary,
        history: Sequence[dict],
        message_ids: Sequence[int],
        budget: TokenBudget,
    ) -> ContextPlan:
        if not isinstance(user_input, str):
            raise TypeError("user_input 必须是 str")
        if not isinstance(system_prompt, str):
            raise TypeError("system_prompt 必须是 str")
        if not isinstance(summary, RollingSummary):
            raise TypeError("summary 必须是 RollingSummary")

        history_items = list(history)
        id_items = list(message_ids)
        if len(history_items) != len(id_items):
            raise ValueError("history 与 message_ids 长度必须一致")

        fixed = (
            MESSAGE_OVERHEAD
            + budget.count_text(system_prompt)
            + MESSAGE_OVERHEAD
            + budget.count_text(user_input)
        )
        remaining = budget.available_tokens - fixed - self.safety_tokens

        state_quota = min(self.state_max_tokens, max(remaining, 0))
        summary_quota = min(
            self.summary_max_tokens,
            max(remaining - state_quota, 0),
        )
        history_quota = max(
            remaining - state_quota - summary_quota,
            0,
        )

        summary_block = self._summary_block(summary)
        summary_tokens = budget.count_text(summary_block)
        if summary_tokens > summary_quota:
            raise BudgetExceededError(
                f"摘要需要 {summary_tokens} tokens，"
                f"超过摘要配额 {summary_quota}"
            )

        kept_indices, history_tokens = self._select_history(
            history_items,
            history_quota,
            budget,
        )
        kept_set = set(kept_indices)
        kept_history = tuple(history_items[index] for index in kept_indices)
        kept_ids = tuple(id_items[index] for index in kept_indices)
        dropped_history = tuple(
            message
            for index, message in enumerate(history_items)
            if index not in kept_set
        )
        dropped_ids = tuple(
            message_id
            for index, message_id in enumerate(id_items)
            if index not in kept_set
        )

        header_tokens = budget.count_text(f"{SUMMARY_HEADER}\n")
        summary_text_quota = max(summary_quota - header_tokens, 0)

        return ContextPlan(
            state_max_tokens=state_quota,
            summary_max_tokens=summary_quota,
            summary_text_max_tokens=summary_text_quota,
            history_max_tokens=history_quota,
            summary_tokens=summary_tokens,
            history_tokens=history_tokens,
            kept_history=kept_history,
            kept_message_ids=kept_ids,
            dropped_history=dropped_history,
            dropped_message_ids=dropped_ids,
        )

    @staticmethod
    def _summary_block(summary: RollingSummary) -> str:
        text = summary.text.strip()
        if not text:
            return ""
        return f"{SUMMARY_HEADER}\n{text}"

    @staticmethod
    def _select_history(
        history: Sequence[dict],
        limit: int,
        budget: TokenBudget,
    ) -> tuple[list[int], int]:
        if limit <= 0 or not history:
            return [], 0

        groups = ContextPlanner._message_groups(history)
        selected: set[int] = set()
        used = 0

        for group in reversed(groups):
            cost = sum(
                budget.count_message(history[index])
                for index in group
            )
            if used + cost > limit:
                break
            selected.update(group)
            used += cost

        kept = sorted(selected)
        return kept, used

    @staticmethod
    def _message_groups(history: Sequence[dict]) -> list[list[int]]:
        groups: list[list[int]] = []
        index = 0

        while index < len(history):
            message = history[index]
            if message.get("role") == "assistant" and message.get("tool_calls"):
                group = [index]
                cursor = index + 1
                while (
                    cursor < len(history)
                    and history[cursor].get("role") == "tool"
                ):
                    group.append(cursor)
                    cursor += 1
                groups.append(group)
                index = cursor
            else:
                groups.append([index])
                index += 1

        return groups
