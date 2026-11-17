"""文本与消息的 token 计数。

本模块只提供最基础的 token 计数能力，不感知上下文策略或模型厂商。
``estimate_tokens`` 是默认实现，生产环境可以通过 ``TokenCounter``
注入厂商 tokenizer。

消息计数的口径放在这里，而不是放在 ``context`` 里：历史存储写入
``turn.token_count`` 时也要用同一套算法，history 不能反向依赖 context。
"""

from typing import Callable, Sequence

TokenCounter = Callable[[str], int]

# 每条消息的固定结构开销（role / 分隔符等）。
MESSAGE_OVERHEAD = 4


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 字符按 1，其他字符按约 4 字符 1 token。

    这不是精确 tokenizer，只用于预算规划。生产环境应留安全余量，
    或注入厂商 tokenizer。
    """
    if not text:
        return 0

    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def count_text(counter: TokenCounter, text: str) -> int:
    """纯文本计数；``None`` 按空串处理。"""
    return counter(text or "")


def message_text(message: dict) -> str:
    """一条消息里参与计数的文本：content + tool_calls 的名称和参数。"""
    parts: list[str] = []

    content = message.get("content")
    if content is not None:
        parts.append(str(content))

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        parts.append(str(function.get("name") or ""))
        parts.append(str(function.get("arguments") or ""))

    return "\n".join(parts)


def count_message(counter: TokenCounter, message: dict) -> int:
    """一条消息的 token 数，含固定结构开销。"""
    return MESSAGE_OVERHEAD + count_text(counter, message_text(message))


def count_messages(counter: TokenCounter, messages: Sequence[dict]) -> int:
    """一组消息的 token 数。"""
    return sum(count_message(counter, message) for message in messages)
