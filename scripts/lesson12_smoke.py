from state.models import GoalStatus, SessionState


def main() -> None:
    state = SessionState(
        preferences={"style": "dry", "budget": 500},
        goals=[{"text": "买两瓶干型酒"}],
        constraints=["预算不超过 500 元"],
        progress={"选酒": "in_progress"},
        facts={"产区": "波尔多"},
    )

    print(state.model_dump_json(indent=2))

    state.goals[0].status = GoalStatus.DONE
    print("目标状态:", state.goals[0].status.value)


if __name__ == "__main__":
    main()
