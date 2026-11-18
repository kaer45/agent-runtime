"""组件共享的装配上下文、协议和文本工具。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from context.models import ContextLimits, ContextRequest, RenderedComponent
from core.tokens import TokenCounter

_TAG_ESCAPES = (
    ("</state>", "&lt;/state&gt;"),
    ("</summary>", "&lt;/summary&gt;"),
    ("<state>", "&lt;state&gt;"),
    ("<summary>", "&lt;summary&gt;"),
)


@dataclass(frozen=True)
class AssemblyContext:
    """一次装配传给所有组件的只读上下文。"""

    request: ContextRequest
    limits: ContextLimits
    counter: TokenCounter


class BaseComponent(Protocol):
    """组件协议。

    ``tokens`` 返回该组件“不截断时”需要的 token 数；
    ``render`` 在 ``max_tokens`` 内产出内容。
    """

    name: str

    def tokens(self, ctx: AssemblyContext) -> int: ...

    def render(
        self,
        ctx: AssemblyContext,
        max_tokens: int,
    ) -> RenderedComponent: ...


def escape_context_tags(text: str) -> str:
    """转义内容里的伪标签，防止破坏 state / summary 边界。"""
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")

    for raw, escaped in _TAG_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def user_message(content: str) -> dict:
    return {"role": "user", "content": content}


def system_message(content: str) -> dict:
    return {"role": "system", "content": content}


def state_message(rendered: str) -> dict:
    return user_message(f"<state>\n{rendered}\n</state>")


def summary_message(rendered: str) -> dict:
    return user_message(f"<summary>\n{rendered}\n</summary>")


def empty_component() -> RenderedComponent:
    return RenderedComponent()
