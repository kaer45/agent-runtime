import json

import pytest

from context.errors import ContextError
from context.tool_selector import ToolSelector, count_schema
from core.issues import ContextIssueCode
from state.models import Goal, SessionState


def schema(name: str, description: str = "") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def cost(item: dict, counter=len) -> int:
    return count_schema(counter, item)


def select(**kwargs):
    values = {
        "user_input": "",
        "state": SessionState(),
        "tools": (),
        "required_tools": frozenset(),
        "ceiling": 10_000,
    }
    values.update(kwargs)
    return ToolSelector(counter=len).select(**values)


def test_empty_tools_yield_empty_selection():
    selection = select()

    assert selection.tools == ()
    assert selection.selected_names == ()
    assert selection.reason == "empty"
    assert selection.tokens == 0


def test_all_tools_are_exposed_when_budget_is_enough():
    tools = (schema("a"), schema("b"))

    selection = select(tools=tools)

    assert selection.selected_names == ("a", "b")
    assert selection.reason == "ranked"


def test_whitelist_filters_candidates_and_reports_them_as_dropped():
    tools = (schema("a"), schema("b"))
    selector = ToolSelector(allowed_tools=frozenset({"a"}), counter=len)

    selection = selector.select(
        user_input="",
        state=SessionState(),
        tools=tools,
        required_tools=frozenset(),
        ceiling=10_000,
    )

    assert selection.selected_names == ("a",)
    assert selection.dropped_names == ("b",)
    assert set(selection.scores) == {"a"}


def test_required_tool_missing_raises():
    with pytest.raises(ContextError) as info:
        select(tools=(schema("a"),), required_tools=frozenset({"missing"}))

    assert info.value.code is ContextIssueCode.TOOLS_REQUIRED_MISSING


def test_required_tool_filtered_by_whitelist_raises():
    selector = ToolSelector(allowed_tools=frozenset({"a"}), counter=len)

    with pytest.raises(ContextError):
        selector.select(
            user_input="",
            state=SessionState(),
            tools=(schema("a"), schema("b")),
            required_tools=frozenset({"b"}),
            ceiling=10_000,
        )


def test_user_input_name_hit_ranks_first():
    tools = (schema("wine_search", "搜索"), schema("brew"))

    selection = select(tools=tools, user_input="帮我 wine_search 一下")

    assert selection.selected_names[0] == "wine_search"
    assert selection.scores["wine_search"] > selection.scores["brew"]


def test_state_hit_raises_score():
    tools = (schema("decant", "醒酒"), schema("brew"))
    state = SessionState(goals=[Goal(text="需要 decant 服务")])

    selection = select(tools=tools, state=state)

    assert selection.selected_names[0] == "decant"


def test_ties_keep_registry_order():
    tools = (schema("b"), schema("a"), schema("c"))

    selection = select(tools=tools)

    assert selection.selected_names == ("b", "a", "c")


def test_score_zero_tools_are_still_eligible():
    tools = (schema("a"), schema("b"))

    selection = select(tools=tools)

    assert selection.scores == {"a": 0.0, "b": 0.0}
    assert selection.selected_names == ("a", "b")


def test_max_tools_trims_lowest_scores():
    tools = (schema("a"), schema("b"))
    selector = ToolSelector(max_tools=1, counter=len)

    selection = selector.select(
        user_input="",
        state=SessionState(),
        tools=tools,
        required_tools=frozenset(),
        ceiling=10_000,
    )

    assert selection.selected_names == ("a",)
    assert selection.dropped_names == ("b",)
    assert selection.reason == "budget_trim"


def test_budget_skips_a_candidate_that_does_not_fit():
    big = schema("big", "x" * 400)
    small = schema("small")

    selection = select(
        tools=(big, small),
        user_input="big",
        ceiling=cost(small) + 10,
    )

    # big 分数更高，但放不下；应该跳过它继续拿 small。
    assert selection.selected_names == ("small",)
    assert selection.dropped_names == ("big",)
    assert selection.tokens == cost(small)


def test_required_that_does_not_fit_raises():
    big = schema("big", "x" * 400)

    with pytest.raises(ContextError) as info:
        select(
            tools=(big,),
            required_tools=frozenset({"big"}),
            ceiling=10,
        )

    assert info.value.code is ContextIssueCode.TOOLS_REQUIRED_MISSING


def test_required_only_reason():
    selection = select(
        tools=(schema("a"),),
        required_tools=frozenset({"a"}),
    )

    assert selection.reason == "required_only"


def test_selected_and_dropped_cover_every_registered_tool():
    tools = (schema("a"), schema("b"), schema("c"))
    selector = ToolSelector(allowed_tools=frozenset({"a", "b"}), max_tools=1, counter=len)

    selection = selector.select(
        user_input="",
        state=SessionState(),
        tools=tools,
        required_tools=frozenset(),
        ceiling=10_000,
    )

    assert set(selection.selected_names) | set(selection.dropped_names) == {
        "a",
        "b",
        "c",
    }


def test_schema_tokens_are_counted_from_json_text():
    item = schema("a", "描述")

    assert count_schema(len, item) == len(
        json.dumps(item, ensure_ascii=False, sort_keys=True)
    )


@pytest.mark.parametrize("max_tools", [-1, "2", True])
def test_rejects_bad_max_tools(max_tools):
    with pytest.raises((TypeError, ValueError)):
        ToolSelector(max_tools=max_tools)
