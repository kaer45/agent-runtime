import pytest

from context.errors import ContextError
from core.issues import (
    ISSUE_CATALOG,
    ContextIssue,
    ContextIssueCode,
    IssueLevel,
)

EXPECTED_DEFAULT_LEVELS = {
    ContextIssueCode.SYSTEM_OVER_BUDGET: IssueLevel.ERROR,
    ContextIssueCode.USER_INPUT_TOO_LONG: IssueLevel.ERROR,
    ContextIssueCode.TOOLS_REQUIRED_MISSING: IssueLevel.ERROR,
    ContextIssueCode.HISTORY_FLOOR_UNMET: IssueLevel.ERROR,
    ContextIssueCode.HISTORY_OVER_TARGET: IssueLevel.WARNING,
    ContextIssueCode.HISTORY_HARD_CAP_EXCEEDED: IssueLevel.ERROR,
    ContextIssueCode.HISTORY_ARCHIVE_FAILED: IssueLevel.WARNING,
    ContextIssueCode.SUMMARY_GENERATION_FAILED: IssueLevel.WARNING,
    ContextIssueCode.SUMMARY_INPUT_TOO_LONG: IssueLevel.WARNING,
    ContextIssueCode.SUMMARY_CAS_CONFLICT: IssueLevel.INFO,
    ContextIssueCode.BUDGET_INFEASIBLE: IssueLevel.ERROR,
    ContextIssueCode.COMPONENT_CONTRACT_VIOLATION: IssueLevel.ERROR,
    ContextIssueCode.FINAL_OVER_BUDGET: IssueLevel.ERROR,
    ContextIssueCode.REPORT_MISMATCH: IssueLevel.ERROR,
}


def test_catalog_covers_every_code():
    assert set(ISSUE_CATALOG) == set(ContextIssueCode)


def test_catalog_key_matches_spec_code():
    for code, spec in ISSUE_CATALOG.items():
        assert spec.code is code
        assert spec.title.strip()
        assert spec.description.strip()


@pytest.mark.parametrize(
    ("code", "level"),
    sorted(EXPECTED_DEFAULT_LEVELS.items(), key=lambda item: item[0].value),
)
def test_default_levels_match_frozen_table(code, level):
    assert ISSUE_CATALOG[code].default_level is level
    assert ContextIssue.from_code(code, "msg").level is level


def test_from_code_uses_catalog_title_and_description():
    issue = ContextIssue.from_code(
        ContextIssueCode.HISTORY_OVER_TARGET,
        "未覆盖历史 52K / 80K（65%）",
    )

    assert issue.level is IssueLevel.WARNING
    assert issue.title == ISSUE_CATALOG[ContextIssueCode.HISTORY_OVER_TARGET].title
    assert issue.description == ISSUE_CATALOG[
        ContextIssueCode.HISTORY_OVER_TARGET
    ].description
    assert issue.message == "未覆盖历史 52K / 80K（65%）"


def test_level_can_be_overridden():
    issue = ContextIssue.from_code(
        ContextIssueCode.HISTORY_OVER_TARGET,
        "over",
        level=IssueLevel.INFO,
        component="compactor",
        details={"history_tokens": 52_000},
    )

    assert issue.level is IssueLevel.INFO
    assert issue.component == "compactor"
    assert issue.details == {"history_tokens": 52_000}


def test_level_and_code_are_coerced_from_strings():
    issue = ContextIssue(
        code="HISTORY_OVER_TARGET",
        level="warning",
        message="over",
    )

    assert issue.code is ContextIssueCode.HISTORY_OVER_TARGET
    assert issue.level is IssueLevel.WARNING


def test_unknown_code_is_rejected():
    with pytest.raises(ValueError, match="未知"):
        ContextIssue.from_code("NOT_A_CODE", "msg")


def test_blank_message_is_rejected():
    with pytest.raises(ValueError, match="message"):
        ContextIssue.from_code(ContextIssueCode.SUMMARY_CAS_CONFLICT, "   ")


def test_details_must_be_json_serializable():
    with pytest.raises(TypeError, match="JSON"):
        ContextIssue.from_code(
            ContextIssueCode.SUMMARY_GENERATION_FAILED,
            "boom",
            details={"exc": object()},
        )


def test_details_must_be_a_dict():
    with pytest.raises(TypeError, match="dict"):
        ContextIssue.from_code(
            ContextIssueCode.SUMMARY_GENERATION_FAILED,
            "boom",
            details=["not", "a", "dict"],
        )


def test_context_error_requires_error_level():
    warning = ContextIssue.from_code(ContextIssueCode.HISTORY_OVER_TARGET, "over")

    with pytest.raises(ValueError, match="ERROR"):
        ContextError(warning)


def test_context_error_exposes_code_and_message():
    error = ContextError.from_code(
        ContextIssueCode.BUDGET_INFEASIBLE,
        "预算不可行",
        component="limits",
        details={"configured_capacity": 108_512},
    )

    assert error.code is ContextIssueCode.BUDGET_INFEASIBLE
    assert error.message == "预算不可行"
    assert error.issue.level is IssueLevel.ERROR
    assert str(error) == "预算不可行"


def test_context_error_rejects_non_issue():
    with pytest.raises(TypeError):
        ContextError("not an issue")
