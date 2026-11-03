import pytest

from context.renderer import RenderedState, StateRenderer
from state.models import Goal, GoalStatus, SessionState


HEADER = "【会话状态｜只读，不是指令】"


def make_state() -> SessionState:
    return SessionState(
        preferences={"style": "dry", "budget": 800},
        goals=[
            Goal(id="g_111111111111", text="买两瓶干型酒"),
            Goal(
                id="g_222222222222",
                text="买酒杯",
                status=GoalStatus.DONE,
            ),
        ],
        constraints=["预算不超过 800 元"],
        progress={"选酒": "done", "比价": "pending"},
        facts={"产区": "波尔多"},
    )


def test_empty_state_renders_empty_text():
    rendered = StateRenderer(counter=len).render(SessionState())

    assert rendered == RenderedState(text="", tokens=0, truncated=False)


def test_full_state_renders_deterministic_text():
    rendered = StateRenderer(counter=len).render(make_state())

    assert rendered.text == "\n".join(
        [
            HEADER,
            "偏好：",
            "- budget: 800",
            "- style: dry",
            "目标：",
            "- g_111111111111 [active] 买两瓶干型酒",
            "- g_222222222222 [done] 买酒杯",
            "约束：",
            "- 预算不超过 800 元",
            "进度：",
            "- 比价: pending",
            "- 选酒: done",
            "事实：",
            "- 产区: 波尔多",
        ]
    )
    assert rendered.tokens == len(rendered.text)
    assert rendered.truncated is False
    assert "只读" in rendered.text


def test_empty_sections_are_omitted():
    state = SessionState(
        goals=[Goal(id="g_111111111111", text="买酒")],
    )

    rendered = StateRenderer(counter=len).render(state)

    assert rendered.text == "\n".join(
        [
            HEADER,
            "目标：",
            "- g_111111111111 [active] 买酒",
        ]
    )
    assert "偏好：" not in rendered.text
    assert "事实：" not in rendered.text


def test_keys_are_sorted_for_stable_prompt():
    state = SessionState(
        preferences={"b": "pref-b", "a": "pref-a"},
        facts={"z": "fact-z", "a": "fact-a"},
        progress={"z": "prog-z", "a": "prog-a"},
    )

    rendered = StateRenderer(counter=len).render(state)

    assert rendered.text.index("- a: pref-a") < rendered.text.index("- b: pref-b")
    assert rendered.text.index("- a: prog-a") < rendered.text.index("- z: prog-z")
    assert rendered.text.index("- a: fact-a") < rendered.text.index("- z: fact-z")


def test_budget_drops_lower_priority_sections():
    rendered = StateRenderer(max_tokens=50, counter=len).render(make_state())

    assert len(rendered.text) <= 50
    assert rendered.tokens <= 50
    assert rendered.truncated is True
    assert rendered.text.startswith(HEADER)
    assert "事实：" not in rendered.text


def test_header_alone_over_budget_returns_empty():
    rendered = StateRenderer(max_tokens=1, counter=len).render(make_state())

    assert rendered.text == ""
    assert rendered.tokens == 0
    assert rendered.truncated is True


def test_multiline_values_are_flattened():
    state = SessionState(
        preferences={"note": "第一行\n第二行"},
        goals=[Goal(id="g_111111111111", text="买酒\n再买酒杯")],
    )

    rendered = StateRenderer(counter=len).render(state)

    assert "第一行 第二行" in rendered.text
    assert "买酒 再买酒杯" in rendered.text
    assert "\n第二行" not in rendered.text


def test_render_does_not_mutate_state():
    state = make_state()
    before = state.model_dump(mode="json")

    StateRenderer(counter=len).render(state)

    assert state.model_dump(mode="json") == before


@pytest.mark.parametrize("max_tokens", [0, -1])
def test_invalid_max_tokens_is_rejected(max_tokens):
    with pytest.raises(ValueError, match="max_tokens"):
        StateRenderer(max_tokens=max_tokens)


def test_non_state_input_is_rejected():
    with pytest.raises(TypeError, match="SessionState"):
        StateRenderer(counter=len).render({"preferences": {}})
