"""明显是闲聊的规则预筛，只做字符串匹配，不依赖 Intent/LLM。"""

from dataclasses import dataclass
import re

MAX_CHAT_RULE_LENGTH = 16

TASK_HINTS = (
    "查", "天气", "时间", "酿酒", "帮我",
    "计算", "搜索", "今天", "明天", "写", "翻译",
)

@dataclass(frozen=True)
class ChatRule:
    name: str
    pattern: re.Pattern[str]

CHAT_RULES = (
    ChatRule("greeting", re.compile(
        r"^(你好|您好|嗨|哈喽|hi|hello|hey)(呀|啊|哦|哈|吗)?[!！。.？?~～…\s]*$",
        re.IGNORECASE,
    )),
    ChatRule("thanks", re.compile(
        r"^(谢谢|感谢|多谢|thanks|thank you)(你|啦|了)?[!！。.？?~～…\s]*$",
        re.IGNORECASE,
    )),
    ChatRule("goodbye", re.compile(
        r"^(再见|拜拜|bye|goodbye)(啦|了)?[!！。.？?~～…\s]*$",
        re.IGNORECASE,
    )),
)

def match_chat_rule(text: str) -> str | None:
    """命中则返回规则名，否则返回 None。"""
    normalized = text.strip()
    if not normalized or len(normalized) > MAX_CHAT_RULE_LENGTH:
        return None
    if any(hint in normalized for hint in TASK_HINTS):
        return None
    for rule in CHAT_RULES:
        if rule.pattern.fullmatch(normalized):
            return rule.name
    return None