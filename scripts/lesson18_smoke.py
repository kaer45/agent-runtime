from context.budget import TokenBudget
from context.builder import ContextBuilder
from context.summarizer import RollingSummary
from state.models import Goal, GoalStatus, SessionState


SYSTEM = "你是私人酒庄的葡萄酒酿酒大师。"


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
    summary = RollingSummary(
        text="用户喜欢干型酒，预算 800 元，已经选过波尔多产区。",
        covered_messages=6,
    )
    history = [
        {"role": "user", "content": "我想买两瓶干型酒。"},
        {"role": "assistant", "content": "可以先看波尔多产区的干型酒。"},
    ]

    builder = ContextBuilder(
        system_prompt=SYSTEM,
        budget=TokenBudget(max_tokens=260, reserve_output=40),
        state_max_tokens=80,
    )
    built = builder.build(
        "今晚喝什么？",
        state=state,
        summary=summary,
        history=history,
    )

    print("messages:", len(built.messages))
    print("state tokens:", built.state_tokens)
    print("summary tokens:", built.summary_tokens)
    print("original tokens:", built.original_tokens)
    print("final tokens:", built.final_tokens)
    print("dropped:", built.dropped_indices)
    print("truncated:", built.truncated)
    print("=" * 50)
    print(built.messages[0]["content"])
    print("=" * 50)
    for index, message in enumerate(built.messages):
        content = str(message.get("content") or "")
        print(f"[{index}] {message['role']}: {content[:60]}")

    print("=" * 50)
    small = ContextBuilder(
        system_prompt=SYSTEM,
        budget=TokenBudget(max_tokens=90),
        state_max_tokens=30,
    ).build(
        "今晚喝什么？",
        state=state,
        summary=summary,
        history=history,
    )
    print("small final tokens:", small.final_tokens)
    print("small dropped:", small.dropped_indices)
    print("small state truncated:", small.state_truncated)
    print("small roles:", [message["role"] for message in small.messages])


if __name__ == "__main__":
    main()
