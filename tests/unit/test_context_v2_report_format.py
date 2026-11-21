import json

from context.engine import ContextEngine
from context.models import ContextRequest
from context.tool_selector import ToolSelector
from core.issues import ContextIssueCode, IssueLevel
from history.models import HistorySnapshot, RollingSummary, TurnRecord
from state.models import SessionState, StateSnapshot

TOOL = {
    "type": "function",
    "function": {
        "name": "search_wine",
        "description": "搜索葡萄酒",
        "parameters": {"type": "object", "properties": {}},
    },
}

OTHER_TOOL = {
    "type": "function",
    "function": {
        "name": "brew",
        "description": "酿酒",
        "parameters": {"type": "object", "properties": {}},
    },
}


def make_turn(seq: int, tokens: int = 1) -> TurnRecord:
    return TurnRecord(
        turn_id=f"turn-{seq}",
        seq=seq,
        messages=(
            {"role": "user", "content": f"问题{seq}"},
            {"role": "assistant", "content": "回答"},
        ),
        token_count=tokens,
        created_at="2026-11-18T00:00:00+00:00",
    )


def build_report(*, turns=(), state=None, summary="", tools=(), tool_selector=None):
    engine = ContextEngine(
        system_prompt="你是侍酒师。",
        counter=len,
        tool_selector=tool_selector,
    )
    request = ContextRequest(
        session_id="session-1",
        turn_id="turn-1",
        user_input="推荐一款干白",
        state=StateSnapshot(state or SessionState(), 0),
        history=HistorySnapshot(
            turns=turns,
            summary=RollingSummary(text=summary),
            covered_through_seq=0,
        ),
        tools=tools,
        required_tools=frozenset({"search_wine"} if tools else set()),
    )
    return engine.assemble(request).report


def over_target_report():
    turns = tuple(make_turn(seq, tokens=8_000) for seq in range(1, 7))
    return build_report(turns=turns)


def test_to_log_is_json_serializable():
    log = over_target_report().to_log()

    assert json.loads(json.dumps(log)) == log


def test_to_log_has_flat_multipliers_and_no_schema():
    report = build_report(
        state=SessionState(preferences={"酒体": "干型"}),
        tools=(TOOL,),
    )

    log = report.to_log()
    text = json.dumps(log, ensure_ascii=False)

    assert log["configured_capacity"] == 108_512
    assert log["tools"]["selected_names"] == ["search_wine"]
    assert "parameters" not in text
    assert "description" not in json.dumps(log)


def test_to_log_compresses_dropped_into_count():
    log = over_target_report().to_log()

    assert "dropped_count" in log["components"]["history"]
    assert isinstance(log["components"]["history"]["dropped_count"], int)


def test_to_log_issue_fields():
    log = over_target_report().to_log()

    issue = log["issues"][0]
    assert issue["code"] == "HISTORY_OVER_TARGET"
    assert issue["level"] == "warning"
    assert "description" not in issue


def test_format_returns_five_sections():
    sections = build_report(tools=(TOOL,)).format()

    assert [section.title for section in sections] == [
        "Budget",
        "Components",
        "History",
        "Tools",
        "Issues",
    ]


def test_every_row_matches_its_columns():
    for section in over_target_report().format():
        for row in section.rows:
            assert len(row.cells) == len(section.columns)


def test_issue_section_carries_description():
    sections = {section.title: section for section in over_target_report().format()}
    row = sections["Issues"].rows[0]

    assert row.level is IssueLevel.WARNING
    assert row.cells[1] == "HISTORY_OVER_TARGET"
    assert row.description
    assert row.description not in row.cells


def test_to_display_contains_budget_table():
    text = build_report(tools=(TOOL,)).to_display()

    assert "=== Context Budget Report ===" in text
    assert "Model Window:                  256,000" in text
    assert "Configured Capacity:           108,512" in text
    assert "Total Used:" in text
    assert "Component" in text
    assert "Status" in text
    assert "search_wine" in text


def test_to_display_shows_issue_description():
    text = over_target_report().to_display()

    assert "HISTORY_OVER_TARGET" in text
    assert "WARNING" in text
    assert "description:" in text


def test_to_display_never_prints_schemas():
    text = build_report(tools=(TOOL,)).to_display()

    assert "parameters" not in text
    assert '"type": "function"' not in text


def test_to_display_statuses_cover_empty_and_trimmed():
    empty = build_report().to_display()
    assert "EMPTY" in empty

    trimmed = build_report(
        tools=(TOOL, OTHER_TOOL),
        tool_selector=ToolSelector(
            allowed_tools=frozenset({"search_wine"}),
            counter=len,
        ),
    ).to_display()
    assert "TRIMMED" in trimmed


def test_issue_codes_are_exposed_as_strings():
    report = over_target_report()

    assert report.issues[0].code is ContextIssueCode.HISTORY_OVER_TARGET
