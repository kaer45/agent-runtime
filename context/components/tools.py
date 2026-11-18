"""tools 组件：把 ``ToolSelection`` 包装成 ``RenderedComponent``。"""

from __future__ import annotations

from context.components.base import AssemblyContext
from context.models import RenderedComponent, ToolSelection
from context.tool_selector import ToolSelector, count_schema


class ToolComponent:
    """选择由 ``ToolSelector`` 负责，组件只做包装。"""

    name = "tools"

    def __init__(self, selector: ToolSelector):
        self.selector = selector

    def tokens(self, ctx: AssemblyContext) -> int:
        return sum(
            count_schema(ctx.counter, schema)
            for schema in ctx.request.tools
        )

    def select(
        self,
        ctx: AssemblyContext,
        max_tokens: int,
    ) -> tuple[RenderedComponent, ToolSelection | None]:
        """返回本轮的组件结果和选择详情；没有工具时为 ``None``。"""
        if not ctx.request.tools:
            return RenderedComponent(), None

        selection = self.selector.select(
            user_input=ctx.request.user_input,
            state=ctx.request.state.state,
            tools=ctx.request.tools,
            required_tools=ctx.request.required_tools,
            ceiling=max_tokens,
        )

        rendered = RenderedComponent(
            tools=selection.tools,
            tokens=selection.tokens,
            dropped=selection.dropped_names,
        )
        return rendered, selection

    def render(
        self,
        ctx: AssemblyContext,
        max_tokens: int,
    ) -> RenderedComponent:
        rendered, _ = self.select(ctx, max_tokens)
        return rendered
