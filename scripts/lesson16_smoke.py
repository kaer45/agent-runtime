import asyncio
import tempfile
from pathlib import Path

from state.extractor import StateDelta
from state.service import StateService
from state.session import SessionRegistry
from state.store import StateConflictError, StateStore


async def main() -> None:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson16-smoke-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    try:
        store = StateStore(db_path)
        store.initialize()
        service = StateService(SessionRegistry(store))

        first = await service.apply(
            "demo-session",
            StateDelta(
                preferences_set={"style": "dry", "budget": 500},
                facts_set={"产区": "波尔多"},
                goals_add=["买两瓶干型酒"],
                constraints_add=["预算不超过 500 元"],
            ),
        )
        print("第一次 apply: version =", first.version)
        print(first.state.model_dump_json(indent=2))

        second = await service.apply(
            "demo-session",
            StateDelta(
                preferences_set={"budget": 800},
                constraints_remove=["预算不超过 500 元"],
                constraints_add=["预算不超过 800 元"],
                goals_complete=["买两瓶干型酒"],
            ),
            expected_version=first.version,
        )
        print("第二次 apply: version =", second.version)
        print(second.state.model_dump_json(indent=2))

        try:
            await service.apply(
                "demo-session",
                StateDelta(goals_add=["再买两瓶"]),
                expected_version=first.version,
            )
        except StateConflictError as exc:
            print("旧版本写入被拒绝:", exc)

        # 新 service 实例从同一个 SQLite 读取，验证版本持久化。
        reopened = StateService(SessionRegistry(store))
        current = await reopened.get("demo-session")
        print("重新读取: version =", current.version)
        print("当前预算:", current.state.preferences["budget"])
        print("目标状态:", current.state.goals[0].status.value)
    finally:
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
