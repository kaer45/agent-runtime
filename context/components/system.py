"""system 组件：固定的 instructions + 上下文说明。"""

from __future__ import annotations

from context.components.base import AssemblyContext, system_message
from context.models import RenderedComponent
from core.tokens import count_message


class SystemComponent:
    """system 消息在 engine 构造时生成并校验，运行时不再截断。"""

    name = "system"

    def __init__(self, content: str):
        self._message = system_message(content)

    @property
    def message(self) -> dict:
        return self._message

    def tokens(self, ctx: AssemblyContext) -> int:
        return count_message(ctx.counter, self._message)

    def render(
        self,
        ctx: AssemblyContext,
        max_tokens: int,
    ) -> RenderedComponent:
        tokens = count_message(ctx.counter, self._message)
        return RenderedComponent(messages=(self._message,), tokens=tokens)
