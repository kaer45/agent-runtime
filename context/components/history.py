"""history 组件：从最新 turn 往前选连续后缀。"""

from __future__ import annotations

from context.components.base import AssemblyContext
from context.models import RenderedComponent
from core.tokens import count_messages
from history.selection import select_recent_turns


class HistoryComponent:
    """只选完整 turn；丢弃的 turn 以 seq 形式报告。"""

    name = "history"

    def tokens(self, ctx: AssemblyContext) -> int:
        messages = [
            message
            for turn in ctx.request.history.turns
            for message in turn.messages
        ]
        return count_messages(ctx.counter, messages)

    def render(
        self,
        ctx: AssemblyContext,
        max_tokens: int,
    ) -> RenderedComponent:
        selection = select_recent_turns(
            ctx.request.history.turns,
            max_tokens,
            ctx.counter,
        )

        messages = tuple(
            message
            for turn in selection.selected
            for message in turn.messages
        )

        return RenderedComponent(
            messages=messages,
            tokens=selection.used_tokens,
            truncated=selection.truncated,
            dropped=tuple(str(seq) for seq in selection.dropped_seqs),
        )
