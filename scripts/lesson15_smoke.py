import asyncio
import tempfile
from pathlib import Path

from state.models import SessionState
from state.session import SessionRegistry
from state.store import StateStore


async def bump(registry: SessionRegistry, session_id: str, times: int) -> None:
    for _ in range(times):
        async with registry.acquire(session_id):
            state = registry.load(session_id)
            count = state.preferences.get("count", 0)
            await asyncio.sleep(0.001)
            state.preferences["count"] = count + 1
            print(f"{session_id}:{state.preferences["count"]}")
            registry.save(session_id, state)


async def main() -> None:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson15-smoke-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    try:
        store = StateStore(db_path)
        store.initialize()
        registry = SessionRegistry(store)

        registry.save(
            "alice",
            SessionState(preferences={"budget": 500}, facts={"产区": "波尔多"}),
        )
        registry.save(
            "bob",
            SessionState(preferences={"budget": 800}, facts={"产区": "勃艮第"}),
        )

        await asyncio.gather(
            bump(registry, "alice", 5),
            bump(registry, "bob", 3),
        )

        for session_id in ("alice", "bob"):
            state = registry.load(session_id)
            print(
                session_id,
                "budget=", state.preferences["budget"],
                "count=", state.preferences["count"],
                "产区=", state.facts["产区"],
            )
    finally:
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
