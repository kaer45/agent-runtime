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
    """存储摘要保持完整，渲染超限时按句子前缀截断。

    截断在原文上进行；``measure`` 负责把候选片段映射成最终消息成本，
    这样转义膨胀和 counter 不可加性都被算进去，不需要事后重试。
    """

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

        def measure(candidate: str) -> int:
            return count_message(
                ctx.counter,
                self._wrap(escape_context_tags(candidate)),
            )

        rendered = self.renderer.render(text, max_tokens, measure=measure)
        if not rendered.text:
            return RenderedComponent(truncated=rendered.truncated)

        message = self._wrap(escape_context_tags(rendered.text))
        return RenderedComponent(
            messages=(message,),
            tokens=count_message(ctx.counter, message),
            truncated=rendered.truncated,
        )

    @staticmethod
    def _wrap(text: str) -> dict:
        return summary_message(text)
