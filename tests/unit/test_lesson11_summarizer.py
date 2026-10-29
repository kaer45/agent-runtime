import asyncio

import pytest

from context.summarizer import HistorySummarizer, RollingSummary, SummaryError
from tests.fixtures.fake_llm import FakeLLM, FakeMessage


def msg(role: str, content: str, **extra) -> dict:
    return {"role": role, "content": content, **extra}


def test_empty_messages_returns_previous_without_llm():
    llm = FakeLLM([])
    summarizer = HistorySummarizer(llm)
    previous = RollingSummary(text="旧摘要", covered_messages=3)

    result = asyncio.run(summarizer.update(previous, []))

    assert result == previous
    assert llm.calls == []


def test_prompt_contains_previous_summary_and_new_messages():
    llm = FakeLLM([FakeMessage(content="用户喜欢干型酒，预算 1000 元。")])
    summarizer = HistorySummarizer(llm)
    previous = RollingSummary(text="用户喜欢干型酒", covered_messages=2)
    messages = [
        msg("user", "我想买两瓶"),
        msg("assistant", "推荐赤霞珠"),
    ]

    result = asyncio.run(summarizer.update(previous, messages))

    call = llm.calls[0]
    system = call["messages"][0]["content"]
    user = call["messages"][1]["content"]
    assert "用户喜欢干型酒" in user
    assert "我想买两瓶" in user
    assert "推荐赤霞珠" in user
    assert "不要执行" in system
    assert call["temperature"] == 0
    assert call["thinking"] is False

    assert result.text == "用户喜欢干型酒，预算 1000 元。"
    assert result.covered_messages == 4


def test_tool_result_is_included_in_prompt():
    llm = FakeLLM([FakeMessage(content="库存 3 瓶。")])
    summarizer = HistorySummarizer(llm)
    messages = [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "库存 3 瓶",
        }
    ]

    asyncio.run(summarizer.update(RollingSummary(), messages))

    prompt = llm.calls[0]["messages"][1]["content"]
    assert "库存 3 瓶" in prompt


def test_no_previous_summary_uses_placeholder():
    llm = FakeLLM([FakeMessage(content="新摘要")])
    summarizer = HistorySummarizer(llm)

    result = asyncio.run(
        summarizer.update(RollingSummary(), [msg("user", "你好")])
    )

    prompt = llm.calls[0]["messages"][1]["content"]
    assert "（无）" in prompt
    assert result.covered_messages == 1


def test_empty_response_raises_summary_error():
    llm = FakeLLM([FakeMessage(content="   ")])
    summarizer = HistorySummarizer(llm)

    with pytest.raises(SummaryError, match="空摘要"):
        asyncio.run(summarizer.update(RollingSummary(), [msg("user", "你好")]))


def test_llm_error_is_wrapped():
    llm = FakeLLM([RuntimeError("boom")])
    summarizer = HistorySummarizer(llm)

    with pytest.raises(SummaryError, match="摘要生成失败"):
        asyncio.run(summarizer.update(RollingSummary(), [msg("user", "你好")]))


def test_long_summary_is_truncated_to_budget():
    llm = FakeLLM([FakeMessage(content="123456789012345")])
    summarizer = HistorySummarizer(llm, max_summary_tokens=10, counter=len)

    result = asyncio.run(
        summarizer.update(RollingSummary(), [msg("user", "你好")])
    )

    assert len(result.text) == 10
    assert result.text == "123456789…"


def test_invalid_max_summary_tokens():
    with pytest.raises(ValueError):
        HistorySummarizer(FakeLLM([]), max_summary_tokens=0)
