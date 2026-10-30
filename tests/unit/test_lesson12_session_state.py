import pytest
from pydantic import ValidationError

from state.models import Goal, GoalStatus, SessionState


def test_session_state_defaults_are_empty():
    state = SessionState()

    assert state.preferences == {}
    assert state.goals == []
    assert state.constraints == []
    assert state.progress == {}
    assert state.facts == {}


def test_session_state_accepts_full_payload():
    state = SessionState(
        preferences={"style": "dry", "budget": 500},
        goals=[{"text": "买两瓶干型酒"}],
        constraints=["预算不超过 500 元"],
        progress={"选酒": "in_progress"},
        facts={"产区": "波尔多"},
    )

    assert state.preferences["style"] == "dry"
    assert state.goals[0].text == "买两瓶干型酒"
    assert state.goals[0].status is GoalStatus.ACTIVE
    assert state.progress["选酒"] == "in_progress"
    assert state.facts["产区"] == "波尔多"


def test_goal_text_is_stripped_and_cannot_be_blank():
    goal = Goal(text="  买酒  ")
    assert goal.text == "买酒"

    with pytest.raises(ValidationError):
        Goal(text="   ")


def test_duplicate_goals_are_rejected():
    with pytest.raises(ValidationError, match="重复目标"):
        SessionState(
            goals=[
                {"text": "买酒"},
                {"text": "买酒"},
            ]
        )


def test_duplicate_constraints_are_rejected():
    with pytest.raises(ValidationError, match="重复约束"):
        SessionState(constraints=["预算 500", "预算 500"])


def test_blank_constraint_is_rejected():
    with pytest.raises(ValidationError):
        SessionState(constraints=["   "])


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        SessionState(unknown_field="x")


def test_state_round_trips_through_json():
    state = SessionState(
        preferences={"style": "dry"},
        goals=[{"text": "买酒", "status": "done"}],
        constraints=["预算 500"],
        progress={"选酒": "done"},
        facts={"产区": "波尔多"},
    )

    payload = state.model_dump(mode="json")
    restored = SessionState.model_validate(payload)

    assert restored == state
    assert payload["goals"][0]["status"] == "done"
