import pytest

from context.budget import BudgetExceededError, TokenBudget, estimate_tokens
from context.messages import MessageSequenceError, validate_message_sequence


def msg(role: str, content: str, **extra) -> dict:
    return {"role": role, "content": content, **extra}


def test_estimate_tokens_for_empty_cjk_and_ascii():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("hello") == 2


def test_count_message_includes_overhead_and_tool_calls():
    budget = TokenBudget(max_tokens=100, counter=len)

    assert budget.count_message(msg("user", "你好")) == 6

    tool_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city":"成都"}',
                }
            }
        ],
    }
    assert budget.count_message(tool_call) == (
        4 + len("get_weather") + 1 + len('{"city":"成都"}')
    )


def test_fit_keeps_all_when_under_budget():
    budget = TokenBudget(max_tokens=100, counter=len)
    messages = [msg("system", "sys"), msg("user", "hi")]

    result = budget.fit(messages)

    assert result.messages == messages
    assert result.dropped_indices == ()
    assert result.truncated is False
    assert result.original_tokens == result.final_tokens


def test_fit_drops_oldest_and_preserves_order():
    budget = TokenBudget(max_tokens=25, counter=len)
    messages = [
        msg("system", "sys"),
        msg("user", "old"),
        msg("assistant", "answer"),
        msg("user", "new"),
    ]

    result = budget.fit(messages)

    assert [m["role"] for m in result.messages] == ["system", "assistant", "user"]
    assert result.messages[-1]["content"] == "new"
    assert result.dropped_indices == (1,)
    assert result.final_tokens == 24
    assert result.truncated is True


def test_fit_respects_reserved_output():
    budget = TokenBudget(max_tokens=30, reserve_output=10, counter=len)
    messages = [
        msg("system", "sys"),
        msg("user", "old"),
        msg("assistant", "answer"),
    ]

    result = budget.fit(messages)

    assert budget.available_tokens == 20
    assert [m["role"] for m in result.messages] == ["system", "user"]
    assert result.dropped_indices == (2,)


def test_fit_raises_when_protected_messages_exceed_budget():
    budget = TokenBudget(max_tokens=20, counter=len)
    messages = [
        msg("system", "1234567890"),
        msg("user", "1234567890"),
    ]

    with pytest.raises(BudgetExceededError, match="超过"):
        budget.fit(messages)


def test_invalid_budget_configuration():
    with pytest.raises(ValueError):
        TokenBudget(max_tokens=0)
    with pytest.raises(ValueError):
        TokenBudget(max_tokens=10, reserve_output=-1)
    with pytest.raises(ValueError):
        TokenBudget(max_tokens=10, reserve_output=10)


def test_fit_empty_messages():
    budget = TokenBudget(max_tokens=20, counter=len)

    result = budget.fit([])

    assert result.messages == []
    assert result.original_tokens == 0
    assert result.final_tokens == 0
    assert result.dropped_indices == ()
    assert result.truncated is False


def test_fit_does_not_mutate_input():
    budget = TokenBudget(max_tokens=100, counter=len)
    messages = [msg("system", "sys"), msg("user", "hi")]
    before = [dict(message) for message in messages]

    result = budget.fit(messages)

    assert messages == before
    assert result.messages is not messages


def test_fit_without_user_keeps_system_and_newest_optional():
    budget = TokenBudget(max_tokens=15, counter=len)
    messages = [
        msg("system", "sys"),
        msg("assistant", "old"),
        msg("assistant", "new"),
    ]

    result = budget.fit(messages)

    assert [m["role"] for m in result.messages] == ["system", "assistant"]
    assert result.messages[-1]["content"] == "new"
    assert result.dropped_indices == (1,)


def tool_messages() -> list[dict]:
    return [
        msg("system", "sys"),
        msg("user", "old"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "get_weather",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "ok",
        },
        msg("assistant", "done"),
        msg("user", "new"),
    ]


def test_fit_keeps_tool_call_and_tool_result_together():
    budget = TokenBudget(max_tokens=50, counter=len)

    result = budget.fit(tool_messages())

    assert [m["role"] for m in result.messages] == [
        "system",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert result.dropped_indices == (1,)
    assert result.final_tokens == 46


def test_fit_does_not_leave_orphan_tool_messages():
    budget = TokenBudget(max_tokens=40, counter=len)

    result = budget.fit(tool_messages())

    roles = [m["role"] for m in result.messages]
    assert "tool" not in roles
    assert not any(m.get("tool_calls") for m in result.messages)
    assert result.dropped_indices == (2, 3)


def test_validate_message_sequence_accepts_tool_group():
    validate_message_sequence(tool_messages())


def test_validate_message_sequence_rejects_orphan_tool():
    messages = [
        msg("system", "sys"),
        msg("user", "hi"),
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]

    with pytest.raises(MessageSequenceError, match="没有 assistant"):
        validate_message_sequence(messages)


def test_validate_message_sequence_rejects_missing_tool_result():
    messages = [
        msg("system", "sys"),
        msg("user", "hi"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        msg("user", "new"),
    ]

    with pytest.raises(MessageSequenceError, match="不匹配"):
        validate_message_sequence(messages)


def test_validate_message_sequence_rejects_wrong_tool_order():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "get_weather", "arguments": "{}"},
                },
                {
                    "id": "call_2",
                    "function": {"name": "get_current_time", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "time"},
        {"role": "tool", "tool_call_id": "call_1", "content": "weather"},
        msg("assistant", "done"),
    ]

    with pytest.raises(MessageSequenceError, match="不匹配"):
        validate_message_sequence(messages)


def test_validate_message_sequence_requires_assistant_after_tools():
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        msg("user", "new"),
    ]

    with pytest.raises(MessageSequenceError, match="role 必须是 assistant"):
        validate_message_sequence(messages)


def test_fit_rejects_invalid_message_sequence():
    budget = TokenBudget(max_tokens=100, counter=len)
    messages = [
        msg("system", "sys"),
        msg("user", "hi"),
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]

    with pytest.raises(MessageSequenceError):
        budget.fit(messages)
