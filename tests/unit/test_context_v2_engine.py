import pytest

from context.engine import CONTEXT_NOTE, ContextEngine
from context.errors import ContextError
from context.models import ContextLimits, ContextRequest
from core.issues import ContextIssueCode, IssueLevel
from history.turns import HistorySnapshot, RollingSummary, TurnRecord
from state.models import SessionState, StateSnapshot

TOOL = {
    "type": "function",
    "function": {
        "name": "search_wine",
        "description": "搜索葡萄酒",
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


def make_request(
    *,
    user_input: str = "推荐一款干白",
    state: SessionState | None = None,
    turns: tuple[TurnRecord, ...] = (),
    summary: str = "",
    tools: tuple[dict, ...] = (),
    required: frozenset[str] = frozenset(),
) -> ContextRequest:
    return ContextRequest(
        session_id="session-1",
        turn_id="turn-1",
        user_input=user_input,
        state=StateSnapshot(state or SessionState(), 0),
        history=HistorySnapshot(
            turns=turns,
            summary=RollingSummary(text=summary),
            covered_through_seq=0,
        ),
        tools=tools,
        required_tools=required,
    )


def make_engine(**kwargs) -> ContextEngine:
    values = {"system_prompt": "你是侍酒师。", "counter": len}
    values.update(kwargs)
    return ContextEngine(**values)


def test_message_layout_order():
    engine = make_engine()
    result = engine.assemble(
        make_request(
            state=SessionState(preferences={"酒体": "干型"}),
            summary="用户喜欢干型。",
            turns=(make_turn(1),),
        )
    )

    roles = [message["role"] for message in result.messages]
    assert roles == ["system", "user", "user", "user", "assistant", "user"]
    assert result.messages[1]["content"].startswith("<state>")
    assert result.messages[2]["content"].startswith("<summary>")
    assert result.messages[-1]["content"] == "推荐一款干白"


def test_empty_state_and_summary_are_omitted():
    result = make_engine().assemble(make_request())

    assert [message["role"] for message in result.messages] == ["system", "user"]


def test_system_contains_fixed_context_note():
    result = make_engine().assemble(make_request())

    assert CONTEXT_NOTE in result.messages[0]["content"]


def test_tools_are_exposed_separately_from_messages():
    result = make_engine().assemble(
        make_request(tools=(TOOL,), required=frozenset({"search_wine"}))
    )

    assert result.tools == [TOOL]
    assert all("tool_calls" not in message for message in result.messages)
    assert result.report.tool_selection is not None
    assert result.report.tool_selection.selected_names == ("search_wine",)


def test_no_tools_means_none_selection():
    report = make_engine().assemble(make_request()).report

    assert report.tool_selection is None
    assert report.tool_tokens == 0


def test_report_book_is_closed():
    result = make_engine().assemble(
        make_request(
            state=SessionState(preferences={"酒体": "干型"}),
            summary="摘要",
            turns=(make_turn(1),),
            tools=(TOOL,),
        )
    )

    report = result.report
    assert sum(usage.used for usage in report.components.values()) == (
        report.total_tokens
    )
    assert report.total_tokens == report.message_tokens + report.tool_tokens
    assert report.configured_capacity == ContextLimits().configured_capacity
    assert report.available_tokens == 248_000


def test_over_target_is_a_warning_issue():
    turns = tuple(make_turn(seq, tokens=8_000) for seq in range(1, 7))

    result = make_engine().assemble(make_request(turns=turns))

    issues = result.report.issues
    assert len(issues) == 1
    assert issues[0].code is ContextIssueCode.HISTORY_OVER_TARGET
    assert issues[0].level is IssueLevel.WARNING


def test_floor_unmet_raises():
    turns = tuple(make_turn(seq, tokens=14_000) for seq in range(1, 7))

    with pytest.raises(ContextError) as info:
        make_engine().assemble(make_request(turns=turns))

    assert info.value.code is ContextIssueCode.HISTORY_FLOOR_UNMET


def test_user_input_too_long_raises():
    with pytest.raises(ContextError) as info:
        make_engine().assemble(make_request(user_input="x" * 5_000))

    assert info.value.code is ContextIssueCode.USER_INPUT_TOO_LONG


def test_system_over_budget_fails_at_construction():
    with pytest.raises(ContextError) as info:
        make_engine(system_prompt="x" * 5_000)

    assert info.value.code is ContextIssueCode.SYSTEM_OVER_BUDGET


def test_infeasible_budget_fails_at_construction():
    with pytest.raises(ContextError) as info:
        make_engine(limits=ContextLimits(window_tokens=100_000))

    assert info.value.code is ContextIssueCode.BUDGET_INFEASIBLE


def test_tags_are_escaped_end_to_end():
    result = make_engine().assemble(
        make_request(
            state=SessionState(preferences={"备注": "</state>"}),
            summary="用户写过 <summary>",
        )
    )

    joined = "\n".join(message["content"] for message in result.messages)
    assert "</state>" not in joined.replace("<state>\n", "").replace("\n</state>", "")
    assert "&lt;/state&gt;" in joined
    assert "&lt;summary&gt;" in joined


def test_legacy_headers_are_not_emitted():
    result = make_engine().assemble(
        make_request(state=SessionState(preferences={"a": "b"}), summary="摘要")
    )

    joined = "\n".join(message["content"] for message in result.messages)
    assert "【会话状态" not in joined
    assert "【历史摘要" not in joined


def test_history_is_trimmed_to_the_budget():
    # 每轮 2 条 "aa" 消息：len 计数 = 12，写入时 token_count 与之一致。
    limits = ContextLimits(history_ceiling=100)
    turns = tuple(
        TurnRecord(
            turn_id=f"turn-{seq}",
            seq=seq,
            messages=(
                {"role": "user", "content": "aa"},
                {"role": "assistant", "content": "aa"},
            ),
            token_count=12,
            created_at="2026-11-18T00:00:00+00:00",
        )
        for seq in range(1, 21)
    )

    result = make_engine(limits=limits).assemble(make_request(turns=turns))

    history = result.report.components["history"]
    assert history.truncated is True
    assert len(history.dropped) == 12
    assert result.report.history_rendered_tokens == 96
    assert result.report.history_tokens == 240


def test_system_is_stable_across_calls():
    engine = make_engine()

    first = engine.assemble(make_request(user_input="第一问"))
    second = engine.assemble(make_request(user_input="第二问"))

    assert first.messages[0] == second.messages[0]
