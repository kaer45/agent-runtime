"""state 组件：``<state>`` 包装的 user 消息。"""

from __future__ import annotations

from context.components.base import (
    AssemblyContext,
    empty_component,
    escape_context_tags,
    state_message,
)
from context.models import RenderedComponent
from context.state_renderer import RenderedState, StateRenderer
from core.tokens import count_message

_UNLIMITED = 10**9


class StateComponent:
    """空 state 直接省略；放不下时按 section 截断并报告。"""

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

        overhead = count_message(ctx.counter, self._wrap(""))
        budget = max_tokens - overhead
        rendered = self.renderer.render(state, max(budget, 0))

        for _ in range(3):
            if not rendered.text:
                return RenderedComponent(
                    truncated=rendered.truncated,
                    dropped=rendered.dropped_sections,
                )

            message = self._wrap(rendered.text)
            actual = count_message(ctx.counter, message)
            if actual <= max_tokens:
                return RenderedComponent(
                    messages=(message,),
                    tokens=actual,
                    truncated=rendered.truncated,
                    dropped=rendered.dropped_sections,
                )

            budget -= actual - max_tokens
            if budget < 0:
                break
            rendered = self.renderer.render(state, budget)

        return RenderedComponent(
            truncated=True,
            dropped=rendered.dropped_sections,
        )

    @staticmethod
    def _wrap(text: str) -> dict:
        return state_message(escape_context_tags(text))
