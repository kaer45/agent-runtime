import tempfile
from pathlib import Path

from state.models import SessionState
from state.store import StateStore


def main() -> None:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson14-smoke-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    try:
        store = StateStore(db_path)
        store.initialize()

        state = SessionState(
            preferences={"style": "dry", "budget": 800},
            goals=[{"text": "买两瓶干型酒", "status": "done"}],
            constraints=["预算不超过 800 元"],
            progress={"选酒": "done"},
            facts={"产区": "勃艮第"},
        )

        store.save("demo-session", state)

        # 新建一个 store，证明数据确实落到了 SQLite 文件。
        reopened = StateStore(db_path)
        reopened.initialize()
        loaded = reopened.load("demo-session")

        print("是否读到状态:", loaded is not None)
        if loaded is not None:
            print(loaded.model_dump_json(indent=2))

        print("删除前是否存在:", store.exists("demo-session"))
        print("是否删除成功:", store.delete("demo-session"))
        print("删除后是否存在:", store.exists("demo-session"))
    finally:
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
