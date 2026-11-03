from context.renderer import StateRenderer
from state.models import Goal, GoalStatus, SessionState


def main() -> None:
    state = SessionState(
        preferences={"style": "dry", "budget": 800},
        goals=[
            Goal(id="g_111111111111", text="买两瓶干型酒"),
            Goal(
                id="g_222222222222",
                text="选酒杯",
                status=GoalStatus.DONE,
            ),
        ],
        constraints=["预算不超过 800 元"],
        progress={"选酒": "done"},
        facts={"产区": "波尔多"},
    )

    renderer = StateRenderer(max_tokens=600)
    rendered = renderer.render(state)

    print("tokens:", rendered.tokens)
    print("truncated:", rendered.truncated)
    print(rendered.text)

    print("=" * 50)
    small = StateRenderer(max_tokens=60).render(state)
    print("small tokens:", small.tokens)
    print("small truncated:", small.truncated)
    print(small.text)


if __name__ == "__main__":
    main()
