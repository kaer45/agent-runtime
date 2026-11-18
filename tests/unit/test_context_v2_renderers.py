import pytest

from context.state_renderer import StateRenderer
from context.summary_renderer import ELLIPSIS, SummaryRenderer
from state.models import Goal, SessionState


# --------------------------------------------------------------- StateRenderer


def test_empty_state_renders_nothing():
    rendered = StateRenderer(counter=len).render(SessionState(), 1_000)

    assert rendered.text == ""
    assert rendered.tokens == 0
    assert rendered.truncated is False
    assert rendered.dropped_sections == ()


def test_state_sections_follow_fixed_order():
    state = SessionState(
        preferences={"酒体": "干型"},
        goals=[Goal(text="找一瓶勃艮第")],
        constraints=["预算 500 以内"],
    )

    rendered = StateRenderer(counter=len).render(state, 1_000)

    lines = rendered.text.splitlines()
    assert lines[0] == "偏好"
    assert "目标" in lines
    assert "约束" in lines
    assert lines.index("偏好") < lines.index("目标") < lines.index("约束")


def test_state_truncates_by_whole_section():
    state = SessionState(
        preferences={"a": "1"},
        goals=[Goal(text="目标")],
    )
    renderer = StateRenderer(counter=len)
    full = renderer.render(state, 1_000)

    rendered = renderer.render(state, len("偏好") + 1 + len("- a: 1"))

    assert "偏好" in rendered.text
    assert "目标" not in rendered.text
    assert rendered.truncated is True
    assert rendered.dropped_sections == ("goals",)
    assert rendered.tokens <= len(full.text)


def test_state_with_no_room_drops_everything():
    state = SessionState(preferences={"a": "1"})

    rendered = StateRenderer(counter=len).render(state, 0)

    assert rendered.text == ""
    assert rendered.truncated is True
    assert rendered.dropped_sections == ("preferences",)


def test_state_rejects_wrong_type():
    with pytest.raises(TypeError):
        StateRenderer(counter=len).render({"preferences": {}}, 100)


# ------------------------------------------------------------- SummaryRenderer


def test_short_summary_is_unchanged():
    rendered = SummaryRenderer(counter=len).render("用户喜欢干型。", 100)

    assert rendered.text == "用户喜欢干型。"
    assert rendered.tokens == len("用户喜欢干型。")
    assert rendered.truncated is False


def test_empty_summary_renders_nothing():
    rendered = SummaryRenderer(counter=len).render("   ", 100)

    assert rendered.text == ""
    assert rendered.tokens == 0
    assert rendered.truncated is False


def test_truncates_at_sentence_boundary():
    text = "第一句。第二句。第三句。"

    rendered = SummaryRenderer(counter=len).render(text, len("第一句。"))

    assert rendered.text == "第一句。"
    assert rendered.truncated is True


def test_falls_back_to_char_prefix_with_ellipsis():
    text = "没有任何句号的超长摘要内容"

    rendered = SummaryRenderer(counter=len).render(text, 6)

    assert rendered.text.endswith(ELLIPSIS)
    assert rendered.tokens <= 6
    assert rendered.truncated is True


def test_no_room_returns_empty_truncated():
    rendered = SummaryRenderer(counter=len).render("任意内容", 0)

    assert rendered.text == ""
    assert rendered.tokens == 0
    assert rendered.truncated is True


@pytest.mark.parametrize("max_tokens", [-1, "10", True])
def test_rejects_bad_budget(max_tokens):
    with pytest.raises((TypeError, ValueError)):
        SummaryRenderer(counter=len).render("x", max_tokens)
