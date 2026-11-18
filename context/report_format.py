"""``ContextReport`` 的三种纯格式化输出。

- ``to_log()``：给日志的 JSON 可序列化 dict；
- ``format_sections()``：给 UI 的结构化 ``DisplaySection``；
- ``to_display()``：终端表格文本。

三者都不输出完整 tool schema，也不修改 report。
"""

from __future__ import annotations

from context.models import ContextReport, DisplayRow, DisplaySection

COMPONENT_ORDER = ("system", "state", "summary", "history", "tools", "user")

BUDGET_COLUMNS = ("Item", "Value")
COMPONENT_COLUMNS = ("Component", "Used", "Cap", "Used%", "Status", "Detail")
HISTORY_COLUMNS = ("Metric", "Value")
TOOLS_COLUMNS = ("Item", "Value")
ISSUE_COLUMNS = ("Level", "Code", "Message")

_RULE = "-" * 80


def to_log(report: ContextReport) -> dict:
    """结构化日志：不含 schema、不含 issue 的 title / description。"""
    tools = None
    if report.tool_selection is not None:
        tools = {
            "selected_names": list(report.tool_selection.selected_names),
            "dropped_names": list(report.tool_selection.dropped_names),
            "reason": report.tool_selection.reason,
            "tokens": report.tool_selection.tokens,
        }

    return {
        "message_tokens": report.message_tokens,
        "tool_tokens": report.tool_tokens,
        "total_tokens": report.total_tokens,
        "available_tokens": report.available_tokens,
        "configured_capacity": report.configured_capacity,
        "window_utilization": report.window_utilization,
        "configured_utilization": report.configured_utilization,
        "history_tokens": report.history_tokens,
        "history_rendered_tokens": report.history_rendered_tokens,
        "history_utilization": report.history_utilization,
        "active_turns": report.active_turns,
        "last6_tokens": report.last6_tokens,
        "summary_render_truncated": report.summary_render_truncated,
        "components": {
            name: {
                "used": usage.used,
                "grant": usage.grant,
                "ceiling": usage.ceiling,
                "truncated": usage.truncated,
                "dropped_count": len(usage.dropped),
            }
            for name, usage in report.components.items()
        },
        "tools": tools,
        "issues": [
            {
                "code": issue.code.value,
                "level": issue.level.value,
                "message": issue.message,
                "component": issue.component,
                "details": issue.details,
            }
            for issue in report.issues
        ],
    }


def format_sections(report: ContextReport) -> tuple[DisplaySection, ...]:
    """给 UI 的结构化区块。"""
    return (
        _budget_section(report),
        _components_section(report),
        _history_section(report),
        _tools_section(report),
        _issues_section(report),
    )


def to_display(report: ContextReport) -> str:
    """终端表格文本。"""
    sections = {section.title: section for section in format_sections(report)}

    lines: list[str] = ["=== Context Budget Report ==="]
    lines.extend(_render_budget_rows(sections["Budget"]))
    lines.append("")
    lines.append(_RULE)
    lines.extend(_render_component_table(sections["Components"]))
    lines.append(_RULE)
    lines.extend(_render_total_row(report))
    lines.append("")
    lines.extend(_render_title("History"))
    lines.extend(_render_pairs(sections["History"]))
    lines.append("")
    lines.extend(_render_title("Tools"))
    lines.extend(_render_pairs(sections["Tools"]))
    lines.append("")
    lines.extend(_render_title("Issues"))
    lines.extend(_render_issues(sections["Issues"]))

    return "\n".join(lines)


# --------------------------------------------------------------------- sections


def _budget_section(report: ContextReport) -> DisplaySection:
    limits = report.limits
    rows = (
        ("Model Window", f"{limits.window_tokens:,}"),
        ("Reserved for Output", f"{limits.reserve_output:,}"),
        ("Available for Input", f"{report.available_tokens:,}"),
        ("Safety (denominator only)", f"{limits.safety_tokens:,}"),
        ("Configured Capacity", f"{report.configured_capacity:,}"),
        ("Message Tokens", f"{report.message_tokens:,}"),
        ("Tool Tokens", f"{report.tool_tokens:,}"),
        ("Total Used", f"{report.total_tokens:,}"),
        ("Window Utilization", _percent(report.window_utilization)),
    )
    return DisplaySection(
        title="Budget",
        columns=BUDGET_COLUMNS,
        rows=tuple(DisplayRow(cells=row) for row in rows),
    )


def _components_section(report: ContextReport) -> DisplaySection:
    rows = [
        DisplayRow(cells=_component_cells(name, report))
        for name in COMPONENT_ORDER
        if name in report.components
    ]
    return DisplaySection(
        title="Components",
        columns=COMPONENT_COLUMNS,
        rows=tuple(rows),
    )


def _history_section(report: ContextReport) -> DisplaySection:
    rows = (
        ("loaded tokens", f"{report.history_tokens:,}"),
        ("rendered tokens", f"{report.history_rendered_tokens:,}"),
        ("active turns", str(report.active_turns)),
        ("last 6 tokens", f"{report.last6_tokens:,}"),
        ("history utilization", _percent(report.history_utilization)),
    )
    return DisplaySection(
        title="History",
        columns=HISTORY_COLUMNS,
        rows=tuple(DisplayRow(cells=row) for row in rows),
    )


def _tools_section(report: ContextReport) -> DisplaySection:
    selection = report.tool_selection
    if selection is None:
        rows = (("selected", "-"), ("dropped", "-"), ("reason", "disabled"), ("tokens", "0"))
    else:
        rows = (
            ("selected", ", ".join(selection.selected_names) or "-"),
            ("dropped", ", ".join(selection.dropped_names) or "-"),
            ("reason", selection.reason),
            ("tokens", f"{selection.tokens:,}"),
        )
    return DisplaySection(
        title="Tools",
        columns=TOOLS_COLUMNS,
        rows=tuple(DisplayRow(cells=row) for row in rows),
    )


def _issues_section(report: ContextReport) -> DisplaySection:
    rows = tuple(
        DisplayRow(
            cells=(issue.level.value.upper(), issue.code.value, issue.message),
            level=issue.level,
            description=issue.description,
        )
        for issue in report.issues
    )
    return DisplaySection(title="Issues", columns=ISSUE_COLUMNS, rows=rows)


# --------------------------------------------------------------------- helpers


def _component_cells(name: str, report: ContextReport) -> tuple[str, ...]:
    usage = report.components[name]
    percent = usage.used / usage.ceiling if usage.ceiling else 0.0

    return (
        name.capitalize(),
        f"{usage.used:,}",
        f"{usage.ceiling:,}",
        f"{percent:.1%}",
        _status(name, usage),
        _detail(name, report, usage),
    )


def _status(name: str, usage) -> str:
    if usage.dropped and name == "tools":
        return "TRIMMED"
    if usage.dropped or usage.truncated:
        return "TRUNCATED"
    if usage.used == 0:
        return "EMPTY"
    return "OK"


def _detail(name: str, report: ContextReport, usage) -> str:
    if name == "history":
        return (
            f"loaded {report.history_tokens:,}, "
            f"rendered {report.history_rendered_tokens:,}, "
            f"dropped {len(usage.dropped)} turns, "
            f"active {report.active_turns}, "
            f"last6 {report.last6_tokens:,}"
        )
    if name == "tools":
        selection = report.tool_selection
        if selection is None:
            return "disabled"
        return (
            f"selected {len(selection.selected_names)}, "
            f"dropped {len(selection.dropped_names)}, "
            f"reason {selection.reason}"
        )
    if name == "summary":
        return f"render_truncated {report.summary_render_truncated}"
    if name == "state" and usage.dropped:
        return "dropped sections: " + ", ".join(usage.dropped)
    return ""


def _percent(value: float) -> str:
    return f"{value:.1%}"


def _render_budget_rows(section: DisplaySection) -> list[str]:
    lines: list[str] = []
    for index, row in enumerate(section.rows):
        label, value = row.cells
        lines.append(f"{label + ':':<28}{value:>10}")
        if index == 3:
            lines.append("")
    return lines


def _render_component_table(section: DisplaySection) -> list[str]:
    header = (
        f"{section.columns[0]:<14}{section.columns[1]:>10}{section.columns[2]:>10}"
        f"{section.columns[3]:>10}{section.columns[4]:>8}    {section.columns[5]}"
    )
    lines = [header]
    for row in section.rows:
        name, used, cap, percent, status, detail = row.cells
        line = (
            f"{name:<14}{used:>10}{cap:>10}{percent:>10}{status:>8}"
        )
        if detail:
            line = f"{line}    {detail}"
        lines.append(line.rstrip())
    return lines


def _render_total_row(report: ContextReport) -> list[str]:
    line = (
        f"{'Total':<14}"
        f"{report.total_tokens:>10,}"
        f"{report.configured_capacity:>10,}"
        f"{_percent(report.configured_utilization):>10}"
        f"{'-':>8}"
    )
    return [line.rstrip()]


def _render_title(title: str) -> list[str]:
    return [f"=== {title} ==="]


def _render_pairs(section: DisplaySection) -> list[str]:
    return [
        f"{label:<24}{value}"
        for label, value in (row.cells for row in section.rows)
    ]


def _render_issues(section: DisplaySection) -> list[str]:
    if not section.rows:
        return ["(none)"]

    lines: list[str] = []
    for row in section.rows:
        level, code, message = row.cells
        lines.append(f"{level:<9}{code}")
        lines.append(f"  message:     {message}")
        if row.description:
            lines.append(f"  description: {row.description}")
    return lines
