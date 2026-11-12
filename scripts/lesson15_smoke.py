import asyncio
import tempfile
from pathlib import Path

from state.models import SessionState
from state.registry import SessionStateRegistry
from state.store import StateSnapshot, StateStore


async def bump(
    registry: SessionStateRegistry,
    session_id: str,
    times: int,
) -> None:
    for _ in range(times):
        async with registry.acquire(session_id):
            snapshot = registry.load_snapshot(session_id)
            current = snapshot or StateSnapshot(
                state=SessionState(),
                version=0,
            )
            state = current.state
            count = state.preferences.get("count", 0)
            await asyncio.sleep(0.001)
            state.preferences["count"] = count + 1
            print(f"{session_id}:{state.preferences['count']}")
            registry.save_snapshot(
                session_id,
                state,
                expected_version=current.version,
            )


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
        registry = SessionStateRegistry(store)

        registry.save_snapshot(
            "alice",
            SessionState(
                preferences={"budget": 500},
                facts={"产区": "波尔多"},
            ),
            expected_version=0,
        )
        registry.save_snapshot(
            "bob",
            SessionState(
                preferences={"budget": 800},
                facts={"产区": "勃艮第"},
            ),
            expected_version=0,
        )

        await asyncio.gather(
            bump(registry, "alice", 5),
            bump(registry, "bob", 3),
        )

        for session_id in ("alice", "bob"):
            snapshot = registry.load_snapshot(session_id)
            state = snapshot.state if snapshot is not None else SessionState()
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
