import pytest

from context.errors import ContextError
from context.models import (
    ComponentUsage,
    ContextLimits,
    ContextReport,
    DisplayRow,
    DisplaySection,
    RenderedComponent,
    ToolSelection,
)
from core.issues import ContextIssue, ContextIssueCode, IssueLevel


def test_default_limits_match_the_frozen_budget_table():
    limits = ContextLimits()

    assert limits.window_tokens == 256_000
    assert limits.reserve_output == 8_000
    assert limits.available_tokens == 248_000
    assert limits.system_ceiling == 4_000
    assert limits.user_ceiling == 4_000
    assert limits.tools_ceiling == 8_000
    assert limits.state_ceiling == 4_000
    assert limits.summary_ceiling == 8_000
    assert limits.history_ceiling == 80_000
    assert limits.safety_tokens == 512


def test_configured_capacity_is_the_exact_sum():
    # 4,000 + 4,000 + 8,000 + 4,000 + 8,000 + 80,000 + 512
    assert ContextLimits().configured_capacity == 108_512


def test_capacity_without_tools_is_tools_ceiling_minus_eight_thousand():
    limits = ContextLimits(tools_ceiling=0)

    assert limits.configured_capacity == 100_512
    limits.validate()


def test_default_limits_pass_startup_validation():
    ContextLimits().validate()


def test_infeasible_budget_raises_error_issue():
    limits = ContextLimits(window_tokens=100_000, reserve_output=8_000)

    with pytest.raises(ContextError) as info:
        limits.validate()

    assert info.value.code is ContextIssueCode.BUDGET_INFEASIBLE
    assert info.value.issue.level is IssueLevel.ERROR
    assert info.value.issue.details["available_tokens"] == 92_000


def test_reserve_output_must_be_smaller_than_window():
    with pytest.raises(ValueError, match="reserve_output"):
        ContextLimits(window_tokens=8_000, reserve_output=8_000)


@pytest.mark.parametrize(
    "field",
    [
        "system_ceiling",
        "user_ceiling",
        "tools_ceiling",
        "state_ceiling",
        "summary_ceiling",
        "history_ceiling",
        "safety_tokens",
    ],
)
def test_ceilings_must_be_non_negative(field):
    with pytest.raises(ValueError, match=field):
        ContextLimits(**{field: -1})


def test_ceilings_reject_booleans():
    with pytest.raises(TypeError, match="safety_tokens"):
        ContextLimits(safety_tokens=True)


def make_usage(**overrides) -> ComponentUsage:
    values = {"used": 10, "grant": 4_000, "ceiling": 4_000}
    values.update(overrides)
    return ComponentUsage(**values)


def make_report(**overrides) -> ContextReport:
    values = {
        "components": {"system": make_usage()},
        "limits": ContextLimits(),
        "message_tokens": 10,
        "tool_tokens": 0,
        "total_tokens": 10,
        "available_tokens": 248_000,
        "configured_capacity": 108_512,
        "window_utilization": 10 / 248_000,
        "configured_utilization": 10 / 108_512,
        "history_tokens": 0,
        "history_rendered_tokens": 0,
        "history_utilization": 0.0,
        "active_turns": 0,
        "last6_tokens": 0,
        "summary_render_truncated": False,
        "tool_selection": None,
        "issues": (),
    }
    values.update(overrides)
    return ContextReport(**values)


def test_report_rejects_blocking_issues():
    error = ContextIssue.from_code(ContextIssueCode.FINAL_OVER_BUDGET, "超限")

    with pytest.raises(ValueError, match="非阻塞"):
        make_report(issues=(error,))


def test_report_accepts_non_blocking_issues():
    warning = ContextIssue.from_code(ContextIssueCode.HISTORY_OVER_TARGET, "over")

    report = make_report(issues=[warning])

    assert report.issues == (warning,)


def test_report_rejects_wrong_tool_selection_type():
    with pytest.raises(TypeError, match="tool_selection"):
        make_report(tool_selection={"tools": []})


def test_report_requires_component_usage_values():
    with pytest.raises(TypeError, match="components"):
        make_report(components={"system": 10})


def test_tool_selection_coerces_lists_to_tuples():
    selection = ToolSelection(
        tools=[{"type": "function"}],
        selected_names=["search_wine"],
        dropped_names=[],
        scores={"search_wine": 2.0},
        reason="ranked",
        tokens=120,
    )

    assert selection.tools == ({"type": "function"},)
    assert selection.selected_names == ("search_wine",)
    assert selection.dropped_names == ()


def test_tool_selection_rejects_unknown_reason():
    with pytest.raises(ValueError, match="reason"):
        ToolSelection(
            tools=(),
            selected_names=(),
            dropped_names=(),
            scores={},
            reason="because",
            tokens=0,
        )


def test_tool_selection_rejects_negative_tokens():
    with pytest.raises(ValueError, match="tokens"):
        ToolSelection(
            tools=(),
            selected_names=(),
            dropped_names=(),
            scores={},
            reason="empty",
            tokens=-1,
        )


def test_rendered_component_defaults_are_empty():
    rendered = RenderedComponent()

    assert rendered.messages == ()
    assert rendered.tools == ()
    assert rendered.tokens == 0
    assert rendered.truncated is False
    assert rendered.dropped == ()


def test_display_section_rejects_ragged_rows():
    with pytest.raises(ValueError, match="cells"):
        DisplaySection(
            title="Budget",
            columns=("Item", "Value"),
            rows=(DisplayRow(cells=("Window",)),),
        )


def test_display_row_coerces_cells():
    row = DisplayRow(cells=["Window", "256,000"])

    assert row.cells == ("Window", "256,000")
    assert row.level is None


def test_display_row_coerces_level():
    row = DisplayRow(cells=("a",), level="warning")

    assert row.level is IssueLevel.WARNING
