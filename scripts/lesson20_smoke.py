import tempfile
from pathlib import Path

from context.history import HistoryStore
from context.summarizer import RollingSummary
from state.models import SessionState
from state.store import StateStore


def main() -> None:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson20-smoke-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    state_store = StateStore(db_path)
    state_store.initialize()
    state_store.save(
        "demo",
        SessionState(
            preferences={"style": "dry", "budget": 800},
            constraints=["预算不超过 800 元"],
        ),
    )

    history = HistoryStore(db_path, max_messages_per_session=3)
    history.initialize()
    history.append(
        "demo",
        [
            {"role": "user", "content": "我喜欢干型酒。"},
            {"role": "assistant", "content": "可以先看波尔多。"},
        ],
    )
    history.save_summary(
        "demo",
        RollingSummary(
            text="用户喜欢干型酒，预算 800 元。",
            covered_messages=2,
        ),
        covered_through_id=2,
    )
    history.append(
        "demo",
        [
            {"role": "user", "content": "推荐一款。"},
            {"role": "assistant", "content": "试试上梅多克。"},
            {"role": "user", "content": "预算改成 1000。"},
        ],
    )

    try:
        plan = history.plan_trim("demo", keep_last=3)
        print("before trim:", history.count("demo"))
        print("drop through:", plan.drop_through_id)
        print("drop count:", plan.drop_count)
        print("covered:", plan.covered_count)
        print("uncovered:", plan.uncovered_count)

        history.commit_trim(plan, summary=None)

        snapshot = history.load("demo")
        print("messages:", history.count("demo"))
        print("summary:", snapshot.summary.text)
        print("covered_messages:", snapshot.summary.covered_messages)
        print("covered_through_id:", snapshot.covered_through_id)
        print("roles:", [message["role"] for message in snapshot.messages])
        print("contents:", [message["content"] for message in snapshot.messages])

        reopened_state = StateStore(db_path)
        reopened_state.initialize()
        state = reopened_state.load("demo")
        print("state version:", reopened_state.load_snapshot("demo").version)
        print("state:", state.model_dump_json())

        print("clear:", history.clear("demo"))
        print("after clear:", history.count("demo"))
    finally:
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
