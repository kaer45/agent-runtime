"""上下文 token 预算：计数、保护关键消息、从旧到新裁剪。

本模块只做预算规划，不调用模型，也不做摘要。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence # Sequence可以支持遍历、索引、len，相比于Iterator更灵活，Iterator有时不支持len和索引

from context.messages import validate_message_sequence

TokenCounter = Callable[[str], int] # 实现依赖注入，相当于声明好了一个接口，接收一个字符串，返回统计的token数，以后可以传不同实现

MESSAGE_OVERHEAD = 4


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 字符按 1，其他字符按约 4 字符 1 token。

    这不是精确 tokenizer，只用于预算规划。生产环境应留安全余量，
    或注入厂商 tokenizer。
    """
    if not text:
        return 0

    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - cjk
    return cjk + (other + 3) // 4


class BudgetExceededError(ValueError):
    """必须保留的 system / 最新 user_input 已经超过输入预算"""

@dataclass(frozen=True)
class BudgetResult:
    messages: list[dict]
    original_tokens: int
    final_tokens: int
    dropped_indices: tuple[int, ...]

    @property
    def truncated(self) -> bool:
        """派生属性，用于告诉上层这次消息压缩有没有截断。"""
        return bool(self.dropped_indices)

    @property
    def dropped_count(self) -> int:
        return len(self.dropped_indices)


class TokenBudget:
    def __init__(
        self,
        max_tokens: int,
        reserve_output: int = 0,
        counter: TokenCounter = estimate_tokens,
    ):
        if max_tokens <= 0:
            raise ValueError("max_tokens 必须大于 0")
        if reserve_output < 0:
            raise ValueError("reserve_output 不能为负数")
        if reserve_output >= max_tokens:
            raise ValueError("reserve_output 必须小于 max_tokens")

        self.max_tokens = max_tokens
        self.reserve_output = reserve_output
        self.counter = counter

    @property
    def available_tokens(self)->int:
        return self.max_tokens-self.reserve_output

    def count_text(self,text:str)->int:
        return self.counter(text or "")

    def count_message(self, message: dict) -> int:
        return MESSAGE_OVERHEAD + self.count_text(self._message_text(message))

    def count_messages(self, messages: Sequence[dict]) -> int:
        return sum(self.count_message(message) for message in messages)

    def fit(self, messages: Sequence[dict]) -> BudgetResult:
        """在预算内保留尽量新的消息，system 和最新 user 必须保留。

        带 tool_calls 的 assistant 消息与其后连续的 tool 结果视为一个原子组：
        要么整组保留，要么整组丢弃，避免产生孤立的 tool_call / tool 消息。
        """
        messages = list(messages)
        validate_message_sequence(messages)
        original_tokens = self.count_messages(messages)

        if original_tokens <= self.available_tokens:
            return BudgetResult(messages, original_tokens, original_tokens, ())

        groups = self._message_groups(messages)
        protected = self._protected_indices(messages)
        selected: set[int] = set()
        used = 0

        for group in groups:
            if any(index in protected for index in group):
                selected.update(group)
                used += sum(self.count_message(messages[index]) for index in group)

        if used > self.available_tokens:
            raise BudgetExceededError(
                f"必须保留的 system/最新 user 已需要 {used} tokens，"
                f"超过可用输入预算 {self.available_tokens}"
            )

        for group in reversed(groups):
            if any(index in selected for index in group):
                continue
            cost = sum(self.count_message(messages[index]) for index in group)
            if used + cost <= self.available_tokens:
                selected.update(group)
                used += cost

        final_messages = [
            message
            for index, message in enumerate(messages)
            if index in selected
        ]
        dropped_indices = tuple(
            index
            for index in range(len(messages))
            if index not in selected
        )
        return BudgetResult(
            messages=final_messages,
            original_tokens=original_tokens,
            final_tokens=used,
            dropped_indices=dropped_indices,
        )

    @staticmethod
    def _message_groups(messages: Sequence[dict]) -> list[list[int]]:
        """把 assistant(tool_calls) 与其后连续的 tool 消息合并成原子组。"""
        groups: list[list[int]] = []
        index = 0

        while index < len(messages):
            message = messages[index]
            if message.get("role") == "assistant" and message.get("tool_calls"):
                group = [index]
                cursor = index + 1
                while cursor < len(messages) and messages[cursor].get("role") == "tool":
                    group.append(cursor)
                    cursor += 1
                groups.append(group)
                index = cursor
            else:
                groups.append([index])
                index += 1

        return groups

    @staticmethod
    def _message_text(message: dict) -> str:
        parts: list[str] = []
        content = message.get("content")
        if content is not None:
            parts.append(str(content))

        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            parts.append(str(function.get("name") or ""))
            parts.append(str(function.get("arguments") or ""))

        return "\n".join(parts)

    @staticmethod
    def _protected_indices(messages: Sequence[dict]) -> set[int]:
        protected = {
            index
            for index, message in enumerate(messages)
            if message.get("role") == "system"
        }
        user_indices = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "user"
        ]
        if user_indices:
            protected.add(user_indices[-1])
        return protected
    