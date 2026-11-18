"""state 组件：``<state>`` 包装的 user 消息。"""

from __future__ import annotations

from context.components.base import (
    AssemblyContext,
    empty_component,
    escape_context_tags,
    state_message,
)
from context.models import RenderedComponent
from context.state_renderer import StateRenderer
from core.tokens import count_message

_UNLIMITED = 10**9


class StateComponent:
    """空 state 直接省略；放不下时按 section 截断并报告。

    截断在原文上进行；``measure`` 负责把候选文本映射成最终消息成本，
    这样转义膨胀和 counter 不可加性都被算进去，不需要事后重试。
    """

    name = "state"

    def __init__(self, renderer: StateRenderer):
        self.renderer = renderer

    def tokens(self, ctx: AssemblyContext) -> int:
        state = ctx.request.state.state
        if self.renderer.is_empty(state):
            return 0

        rendered = self.renderer.render(state, _UNLIMITED)
        return count_message(ctx.counter, self._wrap(rendered.text))

    def render(
        self,
        ctx: AssemblyContext,
        max_tokens: int,
    ) -> RenderedComponent:
        state = ctx.request.state.state
        if self.renderer.is_empty(state):
            return empty_component()

        def measure(candidate: str) -> int:
            return count_message(
                ctx.counter,
                self._wrap(escape_context_tags(candidate)),
            )

        rendered = self.renderer.render(state, max_tokens, measure=measure)
        if not rendered.text:
            return RenderedComponent(
                truncated=rendered.truncated,
                dropped=rendered.dropped_sections,
            )

        message = self._wrap(escape_context_tags(rendered.text))
        return RenderedComponent(
            messages=(message,),
            tokens=count_message(ctx.counter, message),
            truncated=rendered.truncated,
            dropped=rendered.dropped_sections,
        )

    @staticmethod
    def _wrap(text: str) -> dict:
        return state_message(text)
