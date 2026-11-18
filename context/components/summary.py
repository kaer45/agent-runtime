"""summary 组件：``<summary>`` 包装的 user 消息。"""

from __future__ import annotations

from context.components.base import (
    AssemblyContext,
    empty_component,
    escape_context_tags,
    summary_message,
)
from context.models import RenderedComponent
from context.summary_renderer import SummaryRenderer
from core.tokens import count_message

_UNLIMITED = 10**9


class SummaryComponent:
    """存储摘要保持完整，渲染超限时按句子前缀截断。"""

    name = "summary"

    def __init__(self, renderer: SummaryRenderer):
        self.renderer = renderer

    def tokens(self, ctx: AssemblyContext) -> int:
        text = ctx.request.history.summary.text
        if not text.strip():
            return 0

        rendered = self.renderer.render(text, _UNLIMITED)
        return count_message(ctx.counter, self._wrap(rendered.text))

    def render(
        self,
        ctx: AssemblyContext,
        max_tokens: int,
    ) -> RenderedComponent:
        text = ctx.request.history.summary.text
        if not text.strip():
            return empty_component()

        overhead = count_message(ctx.counter, self._wrap(""))
        budget = max_tokens - overhead
        rendered = self.renderer.render(text, max(budget, 0))

        for _ in range(3):
            if not rendered.text:
                return RenderedComponent(truncated=rendered.truncated)

            message = self._wrap(rendered.text)
            actual = count_message(ctx.counter, message)
            if actual <= max_tokens:
                return RenderedComponent(
                    messages=(message,),
                    tokens=actual,
                    truncated=rendered.truncated,
                )

            budget -= actual - max_tokens
            if budget < 0:
                break
            rendered = self.renderer.render(text, budget)

        return RenderedComponent(truncated=True)

    @staticmethod
    def _wrap(text: str) -> dict:
        return summary_message(escape_context_tags(text))
