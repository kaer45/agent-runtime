"""把 ``RollingSummary`` 文本按句子前缀截断（v2）。

与旧 ``SummaryRenderer`` 的区别：

- 不输出 ``SUMMARY_HEADER``，``<summary>`` 标签由组件添加；
- 存储摘要始终完整，截断只影响本次 prompt；
- 先按句子边界截，第一句都放不下时退化为字符前缀 + ``…``。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.tokens import TokenCounter, estimate_tokens

# 句子边界：中英文句号、问号、叹号和换行。
SENTENCE_ENDINGS = "。！？.!?\n"

ELLIPSIS = "…"


@dataclass(frozen=True)
class RenderedSummary:
    """摘要渲染结果。"""

    text: str = ""
    tokens: int = 0
    truncated: bool = False


class SummaryRenderer:
    """把摘要文本裁进给定预算。"""

    def __init__(self, counter: TokenCounter = estimate_tokens):
        self.counter = counter

    def render(self, text: str, max_tokens: int) -> RenderedSummary:
        if not isinstance(text, str):
            raise TypeError("text 必须是字符串")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise TypeError("max_tokens 必须是整数")
        if max_tokens < 0:
            raise ValueError("max_tokens 不能为负数")

        stripped = text.strip()
        if not stripped:
            return RenderedSummary()

        if self._count(stripped) <= max_tokens:
            return RenderedSummary(
                text=stripped,
                tokens=self._count(stripped),
                truncated=False,
            )

        sentence = self._sentence_prefix(stripped, max_tokens)
        if sentence is not None:
            return RenderedSummary(
                text=sentence,
                tokens=self._count(sentence),
                truncated=True,
            )

        fallback = self._char_prefix(stripped, max_tokens)
        return RenderedSummary(
            text=fallback,
            tokens=self._count(fallback),
            truncated=True,
        )

    def _sentence_prefix(self, text: str, max_tokens: int) -> str | None:
        """最长的、以句子边界结尾且放得下的前缀。"""
        best: str | None = None
        for index, char in enumerate(text):
            if char not in SENTENCE_ENDINGS:
                continue

            candidate = text[: index + 1].rstrip()
            if not candidate:
                continue
            if self._count(candidate) <= max_tokens:
                best = candidate
            else:
                break

        return best

    def _char_prefix(self, text: str, max_tokens: int) -> str:
        """退化路径：二分找最长字符前缀，尾部加省略号。"""
        if self._count(ELLIPSIS) > max_tokens:
            return ""

        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self._count(text[:middle].rstrip() + ELLIPSIS) <= max_tokens:
                low = middle
            else:
                high = middle - 1

        if low == 0:
            return ""

        return text[:low].rstrip() + ELLIPSIS

    def _count(self, text: str) -> int:
        return self.counter(text or "")
