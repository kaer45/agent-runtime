"""ContextEngine 的协议值对象。

本模块只有数据和单位约束，不含装配逻辑：

- 装配流程在 M2 的 ``ContextEngine``；
- 工具选择在 M2 的 ``context/tool_selector.py``；
- 压缩决策在 ``history/metrics.py``；
- 报告格式化在 M2 的 ``ContextReport`` 方法。

预算口径按冻结文档：组件之间不借预算，``grant = ceiling``，
报告分母恒为静态的 ``configured_capacity``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from context.errors import ContextError
from core.issues import ContextIssue, ContextIssueCode, IssueLevel
from history.models import HistorySnapshot
from state.models import StateSnapshot

ToolSelectionReason = Literal[
    "required_only",
    "ranked",
    "budget_trim",
    "empty",
]

_TOOL_SELECTION_REASONS = frozenset(get_args(ToolSelectionReason))


def _require_int(name: str, value: int, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数")
    if value < minimum:
        raise ValueError(f"{name} 必须 >= {minimum}，收到 {value}")


@dataclass(frozen=True)
class ContextLimits:
    """静态预算上限。

    ``configured_capacity`` 恒为 108,512：所有组件恒定计入，没有 per-turn
    开关。某个部署不要 tools 时，把 ``tools_ceiling`` 设为 0（变成 100,512）。
    """

    window_tokens: int = 256_000
    reserve_output: int = 8_000

    system_ceiling: int = 4_000
    user_ceiling: int = 4_000
    tools_ceiling: int = 8_000
    state_ceiling: int = 4_000
    summary_ceiling: int = 8_000
    history_ceiling: int = 80_000
    safety_tokens: int = 512

    def __post_init__(self) -> None:
        _require_int("window_tokens", self.window_tokens, minimum=1)
        _require_int("reserve_output", self.reserve_output, minimum=0)
        _require_int("system_ceiling", self.system_ceiling, minimum=0)
        _require_int("user_ceiling", self.user_ceiling, minimum=0)
        _require_int("tools_ceiling", self.tools_ceiling, minimum=0)
        _require_int("state_ceiling", self.state_ceiling, minimum=0)
        _require_int("summary_ceiling", self.summary_ceiling, minimum=0)
        _require_int("history_ceiling", self.history_ceiling, minimum=0)
        _require_int("safety_tokens", self.safety_tokens, minimum=0)

        if self.reserve_output >= self.window_tokens:
            raise ValueError(
                "reserve_output 必须小于 window_tokens："
                f"{self.reserve_output} >= {self.window_tokens}"
            )

    @property
    def available_tokens(self) -> int:
        return self.window_tokens - self.reserve_output

    @property
    def configured_capacity(self) -> int:
        return (
            self.system_ceiling
            + self.user_ceiling
            + self.tools_ceiling
            + self.state_ceiling
            + self.summary_ceiling
            + self.history_ceiling
            + self.safety_tokens
        )

    def validate(self) -> None:
        """启动校验：固定成本放不进 available 时抛 ``BUDGET_INFEASIBLE``。"""
        if self.configured_capacity > self.available_tokens:
            raise ContextError.from_code(
                ContextIssueCode.BUDGET_INFEASIBLE,
                "组件预算超过可用输入预算："
                f"{self.configured_capacity} > {self.available_tokens}",
                component="limits",
                details={
                    "configured_capacity": self.configured_capacity,
                    "available_tokens": self.available_tokens,
                },
            )


@dataclass(frozen=True)
class ToolSelection:
    """一次工具选择的结果。

    ``tools`` 就是本轮实际暴露给模型的工具，也是 middleware 唯一允许
    执行的白名单。
    """

    tools: tuple[dict, ...]
    selected_names: tuple[str, ...]
    dropped_names: tuple[str, ...]
    scores: dict[str, float]
    reason: ToolSelectionReason
    tokens: int

    def __post_init__(self) -> None:
        for name in ("tools", "selected_names", "dropped_names"):
            value = getattr(self, name)
            if isinstance(value, list):
                value = tuple(value)
            if not isinstance(value, tuple):
                raise TypeError(f"ToolSelection.{name} 必须是 tuple")
            object.__setattr__(self, name, value)

        if not all(isinstance(tool, dict) for tool in self.tools):
            raise TypeError("ToolSelection.tools 的元素必须是 dict")
        if not all(isinstance(name, str) for name in self.selected_names):
            raise TypeError("ToolSelection.selected_names 的元素必须是字符串")
        if not all(isinstance(name, str) for name in self.dropped_names):
            raise TypeError("ToolSelection.dropped_names 的元素必须是字符串")

        if not isinstance(self.scores, dict):
            raise TypeError("ToolSelection.scores 必须是 dict")

        if self.reason not in _TOOL_SELECTION_REASONS:
            raise ValueError(
                "ToolSelection.reason 必须是 "
                f"{sorted(_TOOL_SELECTION_REASONS)} 之一，收到 {self.reason!r}"
            )

        _require_int("ToolSelection.tokens", self.tokens, minimum=0)


@dataclass(frozen=True)
class DisplayRow:
    """展示层的一行。``level`` 只用于着色，``description`` 只用于展示。"""

    cells: tuple[str, ...]
    level: IssueLevel | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        cells = self.cells
        if isinstance(cells, list):
            cells = tuple(cells)
        if not isinstance(cells, tuple):
            raise TypeError("DisplayRow.cells 必须是 tuple[str, ...]")
        if not all(isinstance(cell, str) for cell in cells):
            raise TypeError("DisplayRow.cells 的元素必须是字符串")
        object.__setattr__(self, "cells", cells)

        if self.level is not None:
            object.__setattr__(self, "level", IssueLevel(self.level))


@dataclass(frozen=True)
class DisplaySection:
    """展示层的一个区块。每行单元格数必须等于列数。"""

    title: str
    columns: tuple[str, ...]
    rows: tuple[DisplayRow, ...]

    def __post_init__(self) -> None:
        for name in ("columns", "rows"):
            value = getattr(self, name)
            if isinstance(value, list):
                value = tuple(value)
            if not isinstance(value, tuple):
                raise TypeError(f"DisplaySection.{name} 必须是 tuple")
            object.__setattr__(self, name, value)

        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("DisplaySection.title 不能为空")

        if not all(isinstance(column, str) for column in self.columns):
            raise TypeError("DisplaySection.columns 的元素必须是字符串")

        if not all(isinstance(row, DisplayRow) for row in self.rows):
            raise TypeError("DisplaySection.rows 的元素必须是 DisplayRow")

        if any(len(row.cells) != len(self.columns) for row in self.rows):
            raise ValueError(
                "DisplaySection 每行的 cells 数量必须等于 columns 数量"
            )


@dataclass(frozen=True)
class RenderedComponent:
    """一个组件本轮渲染出的内容。"""

    messages: tuple[dict, ...] = ()
    tools: tuple[dict, ...] = ()
    tokens: int = 0
    truncated: bool = False
    dropped: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("messages", "tools", "dropped"):
            value = getattr(self, name)
            if isinstance(value, list):
                value = tuple(value)
            if not isinstance(value, tuple):
                raise TypeError(f"RenderedComponent.{name} 必须是 tuple")
            object.__setattr__(self, name, value)

        if not all(isinstance(message, dict) for message in self.messages):
            raise TypeError("RenderedComponent.messages 的元素必须是 dict")
        if not all(isinstance(tool, dict) for tool in self.tools):
            raise TypeError("RenderedComponent.tools 的元素必须是 dict")
        if not all(isinstance(name, str) for name in self.dropped):
            raise TypeError("RenderedComponent.dropped 的元素必须是字符串")

        _require_int("RenderedComponent.tokens", self.tokens, minimum=0)

        if not isinstance(self.truncated, bool):
            raise TypeError("RenderedComponent.truncated 必须是 bool")


@dataclass(frozen=True)
class ComponentUsage:
    """报告里一个组件的用量。"""

    used: int
    grant: int
    ceiling: int
    truncated: bool = False
    dropped: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_int("ComponentUsage.used", self.used, minimum=0)
        _require_int("ComponentUsage.grant", self.grant, minimum=0)
        _require_int("ComponentUsage.ceiling", self.ceiling, minimum=0)

        if not isinstance(self.truncated, bool):
            raise TypeError("ComponentUsage.truncated 必须是 bool")

        dropped = self.dropped
        if isinstance(dropped, list):
            dropped = tuple(dropped)
        if not isinstance(dropped, tuple):
            raise TypeError("ComponentUsage.dropped 必须是 tuple[str, ...]")
        object.__setattr__(self, "dropped", dropped)


@dataclass(frozen=True)
class ContextReport:
    """一次装配的报告。

    只放装配指标，不放阻塞错误；``issues`` 里只能有非 ERROR 级 issue。
    """

    components: dict[str, ComponentUsage]
    limits: ContextLimits

    message_tokens: int
    tool_tokens: int
    total_tokens: int
    available_tokens: int

    configured_capacity: int
    window_utilization: float
    configured_utilization: float

    history_tokens: int
    history_rendered_tokens: int
    history_utilization: float
    active_turns: int
    last6_tokens: int

    summary_render_truncated: bool
    tool_selection: ToolSelection | None
    issues: tuple[ContextIssue, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.components, dict):
            raise TypeError("ContextReport.components 必须是 dict")
        if not all(
            isinstance(name, str) and isinstance(usage, ComponentUsage)
            for name, usage in self.components.items()
        ):
            raise TypeError(
                "ContextReport.components 必须是 dict[str, ComponentUsage]"
            )

        if not isinstance(self.limits, ContextLimits):
            raise TypeError("ContextReport.limits 必须是 ContextLimits")

        issues = self.issues
        if isinstance(issues, list):
            issues = tuple(issues)
        if not isinstance(issues, tuple):
            raise TypeError("ContextReport.issues 必须是 tuple[ContextIssue, ...]")
        if not all(isinstance(issue, ContextIssue) for issue in issues):
            raise TypeError("ContextReport.issues 的元素必须是 ContextIssue")
        object.__setattr__(self, "issues", issues)

        blocking = [
            issue for issue in issues if issue.level is IssueLevel.ERROR
        ]
        if blocking:
            raise ValueError(
                "ContextReport.issues 只能放非阻塞 issue，"
                f"收到 ERROR：{[issue.code.value for issue in blocking]}"
            )

        if self.tool_selection is not None and not isinstance(
            self.tool_selection, ToolSelection
        ):
            raise TypeError(
                "ContextReport.tool_selection 必须是 ToolSelection 或 None"
            )

    # 格式化方法放在 report_format，避免模型模块反向依赖展示逻辑。

    def to_log(self) -> dict:
        """给日志的 JSON 可序列化 dict。"""
        from context.report_format import to_log

        return to_log(self)

    def format(self) -> tuple["DisplaySection", ...]:
        """给 UI 的结构化区块。"""
        from context.report_format import format_sections

        return format_sections(self)

    def to_display(self) -> str:
        """终端表格文本。"""
        from context.report_format import to_display

        return to_display(self)


@dataclass(frozen=True)
class ContextRequest:
    """一次装配请求。"""

    session_id: str
    turn_id: str
    user_input: str
    state: StateSnapshot
    history: HistorySnapshot
    tools: tuple[dict, ...] = ()
    required_tools: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in ("session_id", "turn_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ContextRequest.{name} 不能为空")

        if not isinstance(self.user_input, str):
            raise TypeError("ContextRequest.user_input 必须是字符串")

        if not isinstance(self.state, StateSnapshot):
            raise TypeError("ContextRequest.state 必须是 StateSnapshot")

        if not isinstance(self.history, HistorySnapshot):
            raise TypeError("ContextRequest.history 必须是 HistorySnapshot")

        tools = self.tools
        if isinstance(tools, list):
            tools = tuple(tools)
        if not isinstance(tools, tuple):
            raise TypeError("ContextRequest.tools 必须是 tuple[dict, ...]")
        if not all(isinstance(tool, dict) for tool in tools):
            raise TypeError("ContextRequest.tools 的元素必须是 dict")
        object.__setattr__(self, "tools", tools)

        required = self.required_tools
        if isinstance(required, (set, list)):
            required = frozenset(required)
        if not isinstance(required, frozenset):
            raise TypeError("ContextRequest.required_tools 必须是 frozenset[str]")
        if not all(isinstance(name, str) for name in required):
            raise TypeError("ContextRequest.required_tools 的元素必须是字符串")
        object.__setattr__(self, "required_tools", required)


@dataclass(frozen=True)
class ContextResult:
    """装配结果。``tools`` 就是本轮暴露给模型的 ``exposed_tools``。"""

    messages: list[dict]
    tools: list[dict]
    report: ContextReport

    def __post_init__(self) -> None:
        for name in ("messages", "tools"):
            value = getattr(self, name)
            if not isinstance(value, list):
                raise TypeError(f"ContextResult.{name} 必须是 list[dict]")
            if not all(isinstance(item, dict) for item in value):
                raise TypeError(f"ContextResult.{name} 的元素必须是 dict")

        if not isinstance(self.report, ContextReport):
            raise TypeError("ContextResult.report 必须是 ContextReport")
