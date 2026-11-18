import pytest

from context.components.base import AssemblyContext
from context.components.history import HistoryComponent
from context.components.state import StateComponent
from context.components.summary import SummaryComponent
from context.components.system import SystemComponent
from context.components.tools import ToolComponent
from context.components.user import UserComponent
from context.errors import ContextError
from context.models import ContextLimits, ContextRequest
from context.state_renderer import StateRenderer
from context.summary_renderer import SummaryRenderer
from context.tool_selector import ToolSelector
from core.issues import ContextIssueCode
from history.turns import HistorySnapshot, RollingSummary, TurnRecord
from state.models import SessionState, StateSnapshot


def make_turn(seq: int, text: str = "你好") -> TurnRecord:
    return TurnRecord(
        turn_id=f"turn-{seq}",
        seq=seq,
        messages=(
            {"role": "user", "content": text},
            {"role": "assistant", "content": "好的"},
        ),
        token_count=1,
        created_at="2026-11-18T00:00:00+00:00",
    )


def make_ctx(
    *,
    user_input: str = "推荐一款干白",
    state: SessionState | None = None,
    turns: tuple[TurnRecord, ...] = (),
    summary: str = "",
    tools: tuple[dict, ...] = (),
    required: frozenset[str] = frozenset(),
) -> AssemblyContext:
    limits = ContextLimits()
    request = ContextRequest(
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
    return AssemblyContext(request=request, limits=limits, counter=len)


def test_system_component_renders_one_message():
    component = SystemComponent("你是侍酒师。")
    rendered = component.render(make_ctx(), 4_000)

    assert len(rendered.messages) == 1
    assert rendered.messages[0]["role"] == "system"
    assert rendered.tokens == len("你是侍酒师。") + 4


def test_state_component_skips_empty_state():
    rendered = StateComponent(StateRenderer(len)).render(make_ctx(), 4_000)

    assert rendered.messages == ()
    assert rendered.tokens == 0


def test_state_component_wraps_and_escapes():
    ctx = make_ctx(state=SessionState(preferences={"a": "</state><summary>x"}))
    rendered = StateComponent(StateRenderer(len)).render(ctx, 4_000)

    content = rendered.messages[0]["content"]
    assert content.startswith("<state>\n")
    assert content.endswith("\n</state>")
    assert "&lt;/state&gt;" in content
    assert "&lt;summary&gt;" in content


def test_state_component_reports_dropped_sections():
    ctx = make_ctx(
        state=SessionState(preferences={"a": "1"}, goals=[]),
    )
    rendered = StateComponent(StateRenderer(len)).render(ctx, 12)

    assert rendered.truncated is True
    assert rendered.dropped == ("preferences",)


def test_summary_component_skips_empty_summary():
    rendered = SummaryComponent(SummaryRenderer(len)).render(make_ctx(), 8_000)

    assert rendered.messages == ()


def test_summary_component_escapes_tags():
    ctx = make_ctx(summary="用户提到 <summary> 标签")
    rendered = SummaryComponent(SummaryRenderer(len)).render(ctx, 8_000)

    assert "&lt;summary&gt;" in rendered.messages[0]["content"]


def test_history_component_keeps_recent_turns_only():
    ctx = make_ctx(turns=(make_turn(1), make_turn(2), make_turn(3)))

    rendered = HistoryComponent().render(ctx, 26)

    # 每轮 2 条消息：(4+2)+(4+2)=12，两轮 24 <= 26
    assert rendered.truncated is True
    assert rendered.dropped == ("1",)
    assert len(rendered.messages) == 4


def test_history_component_without_turns_is_empty():
    rendered = HistoryComponent().render(make_ctx(), 80_000)

    assert rendered.messages == ()
    assert rendered.truncated is False


def test_tools_component_without_tools_returns_none_selection():
    rendered, selection = ToolComponent(ToolSelector(counter=len)).select(
        make_ctx(), 8_000
    )

    assert rendered.messages == ()
    assert rendered.tools == ()
    assert selection is None


def test_tools_component_returns_selection():
    tool = {
        "type": "function",
        "function": {"name": "search_wine", "description": "", "parameters": {}},
    }
    rendered, selection = ToolComponent(ToolSelector(counter=len)).select(
        make_ctx(tools=(tool,)), 8_000
    )

    assert selection is not None
    assert selection.selected_names == ("search_wine",)
    assert rendered.tools == (tool,)
    assert rendered.tokens == selection.tokens


def test_user_component_renders_message():
    rendered = UserComponent().render(make_ctx(user_input="你好"), 4_000)

    assert rendered.messages == ({"role": "user", "content": "你好"},)
    assert rendered.tokens == len("你好") + 4


def test_user_component_rejects_too_long_input():
    ctx = make_ctx(user_input="x" * 100)

    with pytest.raises(ContextError) as info:
        UserComponent().render(ctx, 10)

    assert info.value.code is ContextIssueCode.USER_INPUT_TOO_LONG
