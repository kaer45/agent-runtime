"""user 组件：最新用户输入，不加标签。"""

from __future__ import annotations

from context.components.base import AssemblyContext, user_message
from context.errors import ContextError
from context.models import RenderedComponent
from core.issues import ContextIssueCode
from core.tokens import count_message


class UserComponent:
    """超过 ``user_ceiling`` 直接失败，不截断用户输入。"""

    name = "user"

    def tokens(self, ctx: AssemblyContext) -> int:
        return count_message(ctx.counter, self._message(ctx))

    def render(
        self,
        ctx: AssemblyContext,
        max_tokens: int,
    ) -> RenderedComponent:
        message = self._message(ctx)
        tokens = count_message(ctx.counter, message)

        if tokens > max_tokens:
            raise ContextError.from_code(
                ContextIssueCode.USER_INPUT_TOO_LONG,
                "最新用户输入过长，请缩短或拆分输入后重试："
                f"{tokens} > {max_tokens}",
                component="user",
                details={"tokens": tokens, "user_ceiling": max_tokens},
            )

        return RenderedComponent(messages=(message,), tokens=tokens)

    @staticmethod
    def _message(ctx: AssemblyContext) -> dict:
        return user_message(ctx.request.user_input)
