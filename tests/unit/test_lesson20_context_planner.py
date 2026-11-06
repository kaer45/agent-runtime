import pytest

from context.budget import BudgetExceededError, TokenBudget
from context.planner import ContextPlan, ContextPlanner
from context.summarizer import RollingSummary


SYSTEM = "SYS"
USER = "hi"
FIXED = 4 + len(SYSTEM) + 4 + len(USER)


def make_planner(state_max=20, summary_max=10, safety=0):
    return ContextPlanner(
        state_max_tokens=state_max,
        summary_max_tokens=summary_max,
        safety_tokens=safety,
    )


def make_history(count, size=10):
    return [
        {"role": "user", "content": "x" * size}
        for _ in range(count)
    ]


def test_plan_allocates_quotas_and_keeps_recent_history():
    history = make_history(5)
    budget = TokenBudget(
        max_tokens=FIXED + 20 + 10 + 56,
        counter=len,
    )
    planner = make_planner()

    plan = planner.plan(
        user_input=USER,
        system_prompt=SYSTEM,
        summary=RollingSummary(),
        history=history,
        message_ids=[1, 2, 3, 4, 5],
        budget=budget,
    )

    assert isinstance(plan, ContextPlan)
    assert plan.state_max_tokens == 20
    assert plan.summary_max_tokens == 10
    assert plan.history_max_tokens == 56
    assert plan.history_tokens == 56
    assert plan.kept_history == tuple(history[1:])
    assert plan.kept_message_ids == (2, 3, 4, 5)
    assert plan.dropped_history == (history[0],)
    assert plan.dropped_message_ids == (1,)


def test_plan_keeps_tool_call_group_atomic():
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": "{}"},
            }
        ],
    }
    tool = {"role": "tool", "tool_call_id": "call_1", "content": "晴"}
    history = [
        {"role": "user", "content": "旧消息"},
        assistant,
        tool,
    ]

    counter = len
    budget = TokenBudget(max_tokens=1, counter=counter)
    group_cost = budget.count_message(assistant) + budget.count_message(tool)
    budget = TokenBudget(
        max_tokens=FIXED + 1 + 1 + group_cost,
        counter=counter,
    )
    planner = make_planner(state_max=1, summary_max=1)

    plan = planner.plan(
        user_input=USER,
        system_prompt=SYSTEM,
        summary=RollingSummary(),
        history=history,
        message_ids=[1, 2, 3],
        budget=budget,
    )

    assert plan.kept_message_ids == (2, 3)
    assert plan.dropped_message_ids == (1,)
    assert plan.kept_history == (assistant, tool)


def test_plan_rejects_summary_over_quota():
    budget = TokenBudget(max_tokens=FIXED + 1 + 1 + 1, counter=len)
    planner = make_planner(state_max=1, summary_max=1)

    with pytest.raises(BudgetExceededError, match="摘要"):
        planner.plan(
            user_input=USER,
            system_prompt=SYSTEM,
            summary=RollingSummary(text="很长的摘要"),
            history=[],
            message_ids=[],
            budget=budget,
        )


def test_plan_rejects_mismatched_message_ids():
    planner = make_planner()

    with pytest.raises(ValueError, match="长度"):
        planner.plan(
            user_input=USER,
            system_prompt=SYSTEM,
            summary=RollingSummary(),
            history=[{"role": "user", "content": "x"}],
            message_ids=[],
            budget=TokenBudget(max_tokens=100, counter=len),
        )


def test_plan_empty_history():
    planner = make_planner()
    budget = TokenBudget(max_tokens=100, counter=len)

    plan = planner.plan(
        user_input=USER,
        system_prompt=SYSTEM,
        summary=RollingSummary(),
        history=[],
        message_ids=[],
        budget=budget,
    )

    assert plan.kept_history == ()
    assert plan.dropped_history == ()
    assert plan.history_tokens == 0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"state_max_tokens": 0}, "state_max_tokens"),
        ({"summary_max_tokens": 0}, "summary_max_tokens"),
        ({"safety_tokens": -1}, "safety_tokens"),
    ],
)
def test_invalid_planner_limits(kwargs, message):
    with pytest.raises(ValueError, match=message):
        ContextPlanner(**kwargs)


def test_plan_summary_text_quota_excludes_header():
    planner = make_planner(state_max=1, summary_max=30)
    budget = TokenBudget(max_tokens=100, counter=len)

    plan = planner.plan(
        user_input=USER,
        system_prompt=SYSTEM,
        summary=RollingSummary(text="摘要"),
        history=[],
        message_ids=[],
        budget=budget,
    )

    assert plan.summary_text_max_tokens < plan.summary_max_tokens
