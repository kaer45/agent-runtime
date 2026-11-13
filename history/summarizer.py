"""对话历史滚动摘要：把旧摘要和新增消息压成新的摘要。

只负责生成摘要，不决定丢弃哪些信息：调用方把 Tokenbudget 丢弃的
消息或本轮新增消息传给 update() 即可。
"""

from __future__ import annotations

from typing import Any, Sequence

from core.tokens import estimate_tokens, TokenCounter
from .models import RollingSummary
from .prompts import SUMMARY_SYSTEM_PROMPT



class SummaryError(ValueError):
    """摘要生成失败或摘要为空。"""


class HistorySummarizer:
    """用模型生成滚动摘要。"""

    def __init__(
        self,
        llm: Any,
        max_summary_tokens: int = 300,
        counter: TokenCounter = estimate_tokens
    ):
        if max_summary_tokens <= 0:
            raise ValueError("max_summary_tokens 必须大于 0")

        self.llm=llm
        self.max_summary_tokens=max_summary_tokens
        self.counter=counter

    async def update(
        self,
        previous: RollingSummary,
        messages: Sequence[dict],
    ) -> RollingSummary:
        """把新增消息合并进旧摘要。

        Args:
            previous: 上一次的滚动摘要。
            messages: 新增/被丢弃的消息；不要重复传已覆盖的消息。

        Returns:
            新的 RollingSummary。

        Raises:
            SummaryError: 模型调用失败或返回空摘要。
        """
        if not messages:
            # 如果不需要摘要直接返回。
            return previous

        try:
            response=await self.llm.chat(
                messages=self._build_messages(previous,messages),
                temperature=0,
                thinking=False,
            )
        except Exception as exc:
            raise SummaryError(f"摘要生成失败：{exc}") from exc

        content = (response.content or "").strip()
        if not content:
            raise SummaryError("模型返回了空摘要")

        return RollingSummary(
            text=self._truncate(content),
            covered_messages=previous.covered_messages + len(messages),
        )

    def _build_messages(
        self,
        previous: RollingSummary,
        messages: Sequence[dict],
    ) -> list[dict]:
        previous_text = previous.text or "（无）"
        rendered = self._render_messages(messages)
        return [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"已有摘要：\n{previous_text}\n\n"
                    f"新增消息：\n{rendered}\n\n"
                    f"请输出更新后的摘要，尽量不超过 "
                    f"{self.max_summary_tokens * 0.8} tokens。"
                ),
            },
        ]

    @staticmethod
    def _render_messages(messages: Sequence[dict]) -> str:
        lines: list[str] = []
        for message in messages:
            role = message.get("role", "unknown")
            content = message.get("content")
            if content is not None:
                lines.append(f"{role}: {content}")

            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                lines.append(
                    f"{role} tool_call: {function.get('name', '')}"
                    f"({function.get('arguments', '')})"
                )

        return "\n".join(lines) or "（无）"

    def _truncate(self, text: str) -> str:
        if self.count_text(text) <= self.max_summary_tokens:
            return text

        ellipsis = "…"
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = text[:middle].rstrip() + ellipsis
            if self.count_text(candidate) <= self.max_summary_tokens:
                low = middle
            else:
                high = middle - 1

        return text[:low].rstrip() + ellipsis

    def count_text(self, text: str) -> int:
        return self.counter(text or "")

