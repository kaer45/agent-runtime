"""ContextEngine：上下文装配、预算、报告与工具选择。

历史与压缩在 ``history`` 包；本包只负责把一次请求装配成 messages / tools。
"""

from .engine import CONTEXT_NOTE, ContextEngine
from .errors import ContextError
from .models import (
    ComponentUsage,
    ContextLimits,
    ContextReport,
    ContextRequest,
    ContextResult,
    DisplayRow,
    DisplaySection,
    RenderedComponent,
    ToolSelection,
)
from .state_renderer import StateRenderer
from .summary_renderer import SummaryRenderer
from .tool_selector import ToolSelector

__all__ = [
    "CONTEXT_NOTE",
    "ComponentUsage",
    "ContextEngine",
    "ContextError",
    "ContextLimits",
    "ContextReport",
    "ContextRequest",
    "ContextResult",
    "DisplayRow",
    "DisplaySection",
    "RenderedComponent",
    "StateRenderer",
    "SummaryRenderer",
    "ToolSelection",
    "ToolSelector",
]
