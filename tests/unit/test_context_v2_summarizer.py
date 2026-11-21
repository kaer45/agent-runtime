import asyncio
from types import SimpleNamespace

import pytest

from history.errors import SummaryError
from history.summarizer import (
    LOCAL_TOKEN_LADDER,
    SUMMARY_INPUT_CEILING,
    HistorySummarizer,
    model_token_ladder,
)
from history.models import RollingSummary, TurnRecord
from core.tokens import estimate_tokens


def make_turn(seq: int, text: str = "aa") -> TurnRecord:
    return TurnRecord(
        turn_id=f"turn-{seq}",
        seq=seq,
        messages=(
            {"role": "user", "content": text},
            {"role": "assistant", "content": text},
        ),
        token_count=1,
        created_at="2026-11-18T00:00:00+00:00",
    )


def response(content: str = "摘要", finish_reason: str | None = "stop"):
    return SimpleNamespace(
        message=SimpleNamespace(content=content),
        finish_reason=finish_reason,
        usage=None,
    )


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat_with_meta(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_summarizer(responses, **kwargs) -> tuple[HistorySummarizer, FakeLLM]:
    llm = FakeLLM(responses)
    kwargs.setdefault("counter", len)
    return HistorySummarizer(llm, **kwargs), llm


def test_model_ladder_is_seventy_percent():
    assert model_token_ladder() == (1400, 2800, 5600)
    assert model_token_ladder(LOCAL_TOKEN_LADDER) == (1400, 2800, 5600)


def test_input_ceiling_uses_min_of_policy_and_window():
    assert make_summarizer([])[0].input_ceiling == SUMMARY_INPUT_CEILING

    summarizer, _ = make_summarizer([], available_tokens=50_000)
    assert summarizer.input_ceiling == 50_000 - 5_600


def test_fit_turns_takes_oldest_prefix():
    summarizer, _ = make_summarizer([])
    turns = (make_turn(1), make_turn(2), make_turn(3))

    first = summarizer._prompt_tokens(RollingSummary(), turns[:1])
    second = summarizer._prompt_tokens(RollingSummary(), turns[:2])

    assert summarizer.fit_turns(RollingSummary(), turns, first) == turns[:1]
    assert summarizer.fit_turns(RollingSummary(), turns, second) == turns[:2]
    assert summarizer.fit_turns(RollingSummary(), turns, first - 1) == ()


def test_fit_turns_returns_empty_when_nothing_fits():
    summarizer, _ = make_summarizer([])
    turns = (make_turn(1),)
    base = summarizer._prompt_tokens(RollingSummary(), ())

    assert summarizer.fit_turns(RollingSummary(), turns, base) == ()


def test_fit_turns_without_turns():
    summarizer, _ = make_summarizer([])

    assert summarizer.fit_turns(RollingSummary(), (), 1_000) == ()


def test_fit_turns_never_exceeds_the_ceiling():
    """counter 不可加时，前缀也必须真的放得下。"""
    summarizer = HistorySummarizer(FakeLLM([]), counter=estimate_tokens)
    turns = tuple(make_turn(seq, "abcde" * 7) for seq in range(1, 9))
    exact = summarizer._prompt_tokens(RollingSummary(), turns[:4])
    ceiling = exact - 1

    selected = summarizer.fit_turns(RollingSummary(), turns, ceiling)

    assert summarizer._prompt_tokens(RollingSummary(), selected) <= ceiling


def test_update_returns_summary_and_asks_for_model_budget():
    summarizer, llm = make_summarizer([response("新摘要")])

    result = asyncio.run(
        summarizer.update(RollingSummary(text="旧"), (make_turn(1),))
    )

    assert result == RollingSummary(text="新摘要")
    assert len(llm.calls) == 1
    assert llm.calls[0]["max_tokens"] == 1400
    assert llm.calls[0]["temperature"] == 0


def test_update_without_turns_returns_previous_without_calling():
    summarizer, llm = make_summarizer([])
    previous = RollingSummary(text="旧")

    assert asyncio.run(summarizer.update(previous, ())) == previous
    assert llm.calls == []


def test_local_truncation_upgrades_only_local_budget():
    summarizer, llm = make_summarizer(
        [response("x" * 2_025), response("短摘要")]
    )

    result = asyncio.run(summarizer.update(RollingSummary(), (make_turn(1),)))

    assert result.text == "短摘要"
    assert len(llm.calls) == 2
    # model 档位不动，仍是第一档。
    assert [call["max_tokens"] for call in llm.calls] == [1400, 1400]


def test_model_truncation_upgrades_both_budgets():
    summarizer, llm = make_summarizer(
        [response("摘要", finish_reason="length"), response("短摘要")]
    )

    result = asyncio.run(summarizer.update(RollingSummary(), (make_turn(1),)))

    assert result.text == "短摘要"
    assert [call["max_tokens"] for call in llm.calls] == [1400, 2800]


def test_attempts_exhausted_raises():
    summarizer, llm = make_summarizer(
        [
            response("摘要", finish_reason="length"),
            response("摘要", finish_reason="length"),
            response("摘要", finish_reason="length"),
        ]
    )

    with pytest.raises(SummaryError):
        asyncio.run(summarizer.update(RollingSummary(), (make_turn(1),)))

    assert len(llm.calls) == 3


def test_exception_consumes_attempt_without_upgrade():
    summarizer, llm = make_summarizer([RuntimeError("boom"), response("好")])

    result = asyncio.run(summarizer.update(RollingSummary(), (make_turn(1),)))

    assert result.text == "好"
    assert [call["max_tokens"] for call in llm.calls] == [1400, 1400]


def test_empty_content_consumes_attempt():
    summarizer, llm = make_summarizer([response("   "), response("好")])

    result = asyncio.run(summarizer.update(RollingSummary(), (make_turn(1),)))

    assert result.text == "好"
    assert len(llm.calls) == 2


def test_prompt_over_window_raises_before_calling():
    summarizer, llm = make_summarizer([], available_tokens=1_000)

    with pytest.raises(SummaryError, match="超窗"):
        asyncio.run(summarizer.update(RollingSummary(), (make_turn(1),)))

    assert llm.calls == []


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"available_tokens": 0}, ValueError),
        ({"available_tokens": "x"}, TypeError),
        ({"input_ceiling": 0}, ValueError),
        ({"max_attempts": 0}, ValueError),
    ],
)
def test_rejects_bad_constructor_args(kwargs, error):
    with pytest.raises(error):
        HistorySummarizer(FakeLLM([]), **kwargs)
