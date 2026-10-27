import pytest

from agent.chat_rules import match_chat_rule


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("你好", "greeting"),
        ("您好呀", "greeting"),
        ("你好吗", "greeting"),
        ("hello", "greeting"),
        ("Hi!", "greeting"),
        ("谢谢", "thanks"),
        ("谢谢啦", "thanks"),
        ("感谢！", "thanks"),
        ("Thanks!", "thanks"),
        ("thank you", "thanks"),
        ("再见", "goodbye"),
        ("bye", "goodbye"),
    ],
)
def test_chat_rule_hits(text, expected):
    assert match_chat_rule(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "你好，帮我查成都天气",
        "今天成都适合酿酒吗",
        "曹操的儿子叫什么",
        "你好" + "啊" * 20,
        "你好，今天",
        "",
        "   ",
    ],
)
def test_chat_rule_misses(text):
    assert match_chat_rule(text) is None
