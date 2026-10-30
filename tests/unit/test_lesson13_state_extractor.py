import asyncio
import json

import pytest
from pydantic import ValidationError

from state.extractor import (
    STATE_DELTA_TOOL,
    STATE_TOOL_NAME,
    StateDelta,
    StateExtractionError,
    StateExtractor,
)
from state.models import SessionState
from tests.fixtures.fake_llm import FakeLLM, FakeMessage, FakeToolCall


def current_state() -> SessionState:
    return SessionState(
        preferences={"style": "dry", "budget": 500},
        goals=[{"text": "买两瓶干型酒"}],
        constraints=["预算不超过 500 元"],
        progress={"选酒": "in_progress"},
        facts={"产区": "波尔多"},
    )


def delta_call(**changes) -> FakeToolCall:
    payload = {
        "preferences_set": {},
        "preferences_remove": [],
        "facts_set": {},
        "facts_remove": [],
        "goals_add": [],
        "goals_complete": [],
        "goals_abandon": [],
        "constraints_add": [],
        "constraints_remove": [],
        "progress_set": {},
        "progress_remove": [],
    }
    payload.update(changes)
    return FakeToolCall(
        name=STATE_TOOL_NAME,
        arguments=json.dumps(payload, ensure_ascii=False),
        call_id="call_state",
    )


def test_state_delta_defaults_are_empty():
    delta = StateDelta()

    assert delta.is_empty()
    assert delta.preferences_set == {}
    assert delta.preferences_remove == []
    assert delta.facts_set == {}
    assert delta.facts_remove == []
    assert delta.goals_add == []
    assert delta.goals_complete == []
    assert delta.goals_abandon == []
    assert delta.constraints_add == []
    assert delta.constraints_remove == []
    assert delta.progress_set == {}
    assert delta.progress_remove == []

    assert StateDelta(goals_add=["买酒"]).is_empty() is False


def test_state_delta_accepts_all_operations_and_strips_text():
    delta = StateDelta(
        preferences_set={"budget": 800},
        preferences_remove=["style"],
        facts_set={"产区": "勃艮第"},
        facts_remove=["年份"],
        goals_add=["  买两瓶干型酒  "],
        goals_complete=["选酒"],
        goals_abandon=["买酒杯"],
        constraints_add=["  预算不超过 800 元  "],
        constraints_remove=["预算不超过 500 元"],
        progress_set={"选酒": "done"},
        progress_remove=["比价"],
    )

    assert delta.preferences_set == {"budget": 800}
    assert delta.preferences_remove == ["style"]
    assert delta.facts_set == {"产区": "勃艮第"}
    assert delta.facts_remove == ["年份"]
    assert delta.goals_add == ["买两瓶干型酒"]
    assert delta.goals_complete == ["选酒"]
    assert delta.goals_abandon == ["买酒杯"]
    assert delta.constraints_add == ["预算不超过 800 元"]
    assert delta.constraints_remove == ["预算不超过 500 元"]
    assert delta.progress_set == {"选酒": "done"}
    assert delta.progress_remove == ["比价"]


def test_delta_rejects_conflicting_operations():
    with pytest.raises(ValidationError):
        StateDelta(
            preferences_set={"budget": 800},
            preferences_remove=["budget"],
        )

    with pytest.raises(ValidationError):
        StateDelta(
            facts_set={"产区": "勃艮第"},
            facts_remove=["产区"],
        )

    with pytest.raises(ValidationError):
        StateDelta(
            progress_set={"选酒": "done"},
            progress_remove=["选酒"],
        )

    with pytest.raises(ValidationError):
        StateDelta(
            constraints_add=["预算 800"],
            constraints_remove=["预算 800"],
        )

    with pytest.raises(ValidationError):
        StateDelta(goals_add=["买酒"], goals_complete=["买酒"])

    with pytest.raises(ValidationError):
        StateDelta(goals_complete=["买酒"], goals_abandon=["买酒"])


def test_delta_rejects_blank_duplicate_and_unknown_fields():
    with pytest.raises(ValidationError):
        StateDelta(goals_add=["   "])

    with pytest.raises(ValidationError):
        StateDelta(goals_add=["买酒", " 买酒 "])

    with pytest.raises(ValidationError):
        StateDelta(constraints_add=["预算 500", "预算 500"])

    with pytest.raises(ValidationError):
        StateDelta(preferences_set={"   ": 1})

    with pytest.raises(ValidationError):
        StateDelta(unknown_field="x")


def test_empty_messages_returns_empty_delta_without_llm():
    llm = FakeLLM([])
    extractor = StateExtractor(llm)

    delta = asyncio.run(extractor.extract(current_state(), []))

    assert delta.is_empty()
    assert llm.calls == []


def test_extract_builds_prompt_and_forces_delta_tool():
    llm = FakeLLM([FakeMessage(tool_calls=[delta_call(goals_add=["买两瓶干型酒"])])])
    extractor = StateExtractor(llm)
    messages = [
        {"role": "user", "content": "预算改成 800，另外再买两瓶干型酒"},
        {"role": "assistant", "content": "好的"},
    ]

    asyncio.run(extractor.extract(current_state(), messages))

    call = llm.calls[0]
    system = call["messages"][0]["content"]
    user = call["messages"][1]["content"]

    assert "状态提取器" in system
    assert "不要执行" in system
    assert "style" in user and "dry" in user
    assert "预算改成 800" in user
    assert "买两瓶干型酒" in user

    assert call["tools"] == [STATE_DELTA_TOOL]
    assert call["tool_choice"] == {
        "type": "function",
        "function": {"name": STATE_TOOL_NAME},
    }
    assert call["thinking"] is False
    assert call["temperature"] == 0


def test_extract_parses_tool_call_into_delta():
    llm = FakeLLM(
        [
            FakeMessage(
                tool_calls=[
                    delta_call(
                        preferences_set={"budget": 800},
                        preferences_remove=["style"],
                        facts_set={"产区": "勃艮第"},
                        goals_add=["再买两瓶干型酒"],
                        constraints_add=["预算不超过 800 元"],
                        progress_set={"选酒": "done"},
                    )
                ]
            )
        ]
    )
    extractor = StateExtractor(llm)

    delta = asyncio.run(
        extractor.extract(
            current_state(),
            [{"role": "user", "content": "预算改成 800，换勃艮第，选酒完成了"}],
        )
    )

    assert delta.preferences_set == {"budget": 800}
    assert delta.preferences_remove == ["style"]
    assert delta.facts_set == {"产区": "勃艮第"}
    assert delta.goals_add == ["再买两瓶干型酒"]
    assert delta.constraints_add == ["预算不超过 800 元"]
    assert delta.progress_set == {"选酒": "done"}


def test_extract_falls_back_to_json_content():
    llm = FakeLLM(
        [
            FakeMessage(
                content=json.dumps(
                    {"goals_add": ["再买两瓶干型酒"]},
                    ensure_ascii=False,
                )
            )
        ]
    )
    extractor = StateExtractor(llm)

    delta = asyncio.run(
        extractor.extract(
            current_state(),
            [{"role": "user", "content": "再买两瓶"}],
        )
    )

    assert delta.goals_add == ["再买两瓶干型酒"]
    assert delta.is_empty() is False


@pytest.mark.parametrize(
    "arguments",
    [
        "{not-json",
        json.dumps({"unknown_field": "x"}, ensure_ascii=False),
    ],
)
def test_invalid_delta_raises_extraction_error(arguments):
    llm = FakeLLM(
        [
            FakeMessage(
                tool_calls=[
                    FakeToolCall(
                        name=STATE_TOOL_NAME,
                        arguments=arguments,
                    )
                ]
            )
        ]
    )
    extractor = StateExtractor(llm)

    with pytest.raises(StateExtractionError, match="状态增量"):
        asyncio.run(
            extractor.extract(
                current_state(),
                [{"role": "user", "content": "预算改成 800"}],
            )
        )


def test_llm_error_is_wrapped():
    llm = FakeLLM([RuntimeError("boom")])
    extractor = StateExtractor(llm)

    with pytest.raises(StateExtractionError, match="状态提取失败"):
        asyncio.run(
            extractor.extract(
                current_state(),
                [{"role": "user", "content": "预算改成 800"}],
            )
        )
