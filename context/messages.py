"""校验消息序列是否满足 OpenAI Chat Completions 的工具调用顺序。

这里只校验与工具调用相关的结构不变量：
- assistant 的 tool_calls 后面必须紧接对应的 tool 消息；
- tool 消息不能出现没有 assistant 的孤儿；
- tool 结果之后必须回到 assistant（或结束）；
- 多个 tool 结果必须按 tool_calls 的顺序返回。
"""

from __future__ import annotations

from typing import Sequence

ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})


class MessageSequenceError(ValueError):
    """消息序列不满足 OpenAI 工具调用协议。"""


def validate_message_sequence(messages: Sequence[dict]) -> None:
    """校验消息序列，失败时抛出 ``MessageSequenceError``。"""
    index = 0

    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict):
            raise MessageSequenceError(f"messages[{index}] 必须是 dict")

        role = message.get("role")
        if role not in ALLOWED_ROLES:
            raise MessageSequenceError(
                f"messages[{index}] 的 role 不合法: {role!r}"
            )

        if role == "tool":
            raise MessageSequenceError(
                f"messages[{index}] 是 tool，但前面没有 assistant tool_calls"
            )

        tool_calls = message.get("tool_calls")
        if role != "assistant" or not tool_calls:
            index += 1
            continue

        if not isinstance(tool_calls, list):
            raise MessageSequenceError(
                f"messages[{index}].tool_calls 必须是 list"
            )

        expected_ids: list[str] = []
        for call_index, call in enumerate(tool_calls):
            if not isinstance(call, dict):
                raise MessageSequenceError(
                    f"messages[{index}].tool_calls[{call_index}] 必须是 dict"
                )

            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id:
                raise MessageSequenceError(
                    f"messages[{index}].tool_calls[{call_index}] 缺少合法 id"
                )

            function = call.get("function")
            if not isinstance(function, dict):
                raise MessageSequenceError(
                    f"messages[{index}].tool_calls[{call_index}] 缺少 function"
                )
            if not isinstance(function.get("name"), str) or not function["name"]:
                raise MessageSequenceError(
                    f"messages[{index}].tool_calls[{call_index}] 缺少 function.name"
                )
            if not isinstance(function.get("arguments"), str):
                raise MessageSequenceError(
                    f"messages[{index}].tool_calls[{call_index}] "
                    f"的 function.arguments 必须是 JSON 字符串"
                )

            expected_ids.append(call_id)

        if len(expected_ids) != len(set(expected_ids)):
            raise MessageSequenceError(
                f"messages[{index}].tool_calls 存在重复 id"
            )

        cursor = index + 1
        actual_ids: list[str] = []
        while (
            cursor < len(messages)
            and isinstance(messages[cursor], dict)
            and messages[cursor].get("role") == "tool"
        ):
            tool_call_id = messages[cursor].get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise MessageSequenceError(
                    f"messages[{cursor}] 的 tool_call_id 不合法"
                )
            actual_ids.append(tool_call_id)
            cursor += 1

        if actual_ids != expected_ids:
            raise MessageSequenceError(
                f"messages[{index}] 的 tool_calls 与 tool 结果不匹配："
                f"期望 {expected_ids}，实际 {actual_ids}"
            )

        if cursor < len(messages):
            next_message = messages[cursor]
            if (
                not isinstance(next_message, dict)
                or next_message.get("role") != "assistant"
            ):
                raise MessageSequenceError(
                    f"messages[{cursor}] 紧跟在 tool 结果后，role 必须是 assistant"
                )

        index = cursor
