import pytest

from history.errors import HistoryTurnInvalid
from history.models import validate_turn_messages


def user(text: str = "你好") -> dict:
    return {"role": "user", "content": text}


def assistant(text: str = "好的") -> dict:
    return {"role": "assistant", "content": text}


def tool_call(call_id: str = "call_1") -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "brew", "arguments": "{}"},
            }
        ],
    }


def tool_result(call_id: str = "call_1") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": "{}"}


def test_simple_turn_is_valid():
    validate_turn_messages([user(), assistant()])


def test_turn_with_tool_pairing_is_valid():
    validate_turn_messages([user(), tool_call(), tool_result(), assistant()])


def test_empty_turn_is_rejected():
    with pytest.raises(HistoryTurnInvalid, match="不能为空"):
        validate_turn_messages([])


def test_first_message_must_be_user():
    with pytest.raises(HistoryTurnInvalid, match="第一条"):
        validate_turn_messages([assistant(), assistant()])


def test_turn_allows_only_one_user():
    with pytest.raises(HistoryTurnInvalid, match="只允许一条 user"):
        validate_turn_messages([user(), user(), assistant()])


def test_system_message_is_rejected():
    with pytest.raises(HistoryTurnInvalid, match="system"):
        validate_turn_messages(
            [user(), {"role": "system", "content": "x"}, assistant()]
        )


def test_last_message_must_be_final_assistant():
    with pytest.raises(HistoryTurnInvalid, match="最后一条"):
        validate_turn_messages([user(), tool_call(), tool_result()])


def test_last_assistant_must_not_have_tool_calls():
    with pytest.raises(HistoryTurnInvalid, match="最后一条"):
        validate_turn_messages([user(), tool_call()])


def test_orphan_tool_result_is_rejected():
    with pytest.raises(HistoryTurnInvalid, match="tool 消息"):
        validate_turn_messages([user(), tool_result(), assistant()])


def test_tool_result_must_match_call_order():
    messages = [
        user(),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "x"}},
                {"id": "b", "type": "function", "function": {"name": "y"}},
            ],
        },
        tool_result("b"),
        tool_result("a"),
        assistant(),
    ]

    with pytest.raises(HistoryTurnInvalid, match="顺序"):
        validate_turn_messages(messages)


def test_missing_tool_result_is_rejected():
    messages = [
        user(),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "x"}},
                {"id": "b", "type": "function", "function": {"name": "y"}},
            ],
        },
        tool_result("a"),
        assistant(),
    ]

    with pytest.raises(HistoryTurnInvalid, match="一一对应"):
        validate_turn_messages(messages)


def test_tool_message_requires_tool_call_id():
    messages = [
        user(),
        tool_call(),
        {"role": "tool", "content": "{}"},
        assistant(),
    ]

    with pytest.raises(HistoryTurnInvalid, match="tool_call_id"):
        validate_turn_messages(messages)


def test_tool_call_requires_id():
    messages = [
        user(),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"type": "function", "function": {"name": "x"}}],
        },
        assistant(),
    ]

    with pytest.raises(HistoryTurnInvalid, match="id"):
        validate_turn_messages(messages)


def test_duplicate_tool_call_ids_are_rejected():
    messages = [
        user(),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "x"}},
                {"id": "a", "type": "function", "function": {"name": "y"}},
            ],
        },
        tool_result("a"),
        assistant(),
    ]

    with pytest.raises(HistoryTurnInvalid, match="重复"):
        validate_turn_messages(messages)


def test_non_dict_message_is_rejected():
    with pytest.raises(HistoryTurnInvalid, match="dict"):
        validate_turn_messages([user(), "assistant"])


def test_non_sequence_is_rejected():
    with pytest.raises(TypeError, match="序列"):
        validate_turn_messages({"role": "user"})
