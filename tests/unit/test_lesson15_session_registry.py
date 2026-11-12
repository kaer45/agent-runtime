import asyncio
import tempfile
from pathlib import Path

import pytest

from state.models import SessionState
from state.registry import SessionStateRegistry
from state.store import StateSnapshot, StateStore


def sample_state(budget: int = 500) -> SessionState:
    return SessionState(
        preferences={"style": "dry", "budget": budget},
        goals=[{"text": "买两瓶干型酒"}],
        constraints=["预算不超过 500 元"],
        progress={"选酒": "in_progress"},
        facts={"产区": "波尔多"},
    )


@pytest.fixture
def registry() -> SessionStateRegistry:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson15-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    store = StateStore(db_path)
    store.initialize()

    try:
        yield SessionStateRegistry(store)
    finally:
        db_path.unlink(missing_ok=True)


def test_sessions_are_isolated(registry):
    registry.save_snapshot(
        "session-a",
        sample_state(budget=500),
        expected_version=0,
    )
    registry.save_snapshot(
        "session-b",
        sample_state(budget=800),
        expected_version=0,
    )

    snapshot_a = registry.load_snapshot("session-a")
    snapshot_b = registry.load_snapshot("session-b")
    assert snapshot_a is not None
    assert snapshot_b is not None
    assert snapshot_a.state.preferences["budget"] == 500
    assert snapshot_b.state.preferences["budget"] == 800


def test_missing_session_returns_none(registry):
    assert registry.exists("missing") is False
    assert registry.load_snapshot("missing") is None


def test_delete_removes_session_state(registry):
    registry.save_snapshot(
        "session-a",
        sample_state(),
        expected_version=0,
    )

    assert registry.delete("session-a") is True
    assert registry.delete("session-a") is False
    assert registry.exists("session-a") is False
    assert registry.load_snapshot("session-a") is None


def test_snapshot_carries_version(registry):
    version = registry.save_snapshot(
        "session-a",
        sample_state(),
        expected_version=0,
    )

    snapshot = registry.load_snapshot("session-a")
    assert snapshot is not None
    assert version == 1
    assert snapshot.version == 1


def test_load_returns_independent_state_objects(registry):
    registry.save_snapshot(
        "session-a",
        sample_state(budget=500),
        expected_version=0,
    )

    first = registry.load_snapshot("session-a")
    assert first is not None
    first.state.preferences["budget"] = 999

    second = registry.load_snapshot("session-a")
    assert second is not None
    assert second.state.preferences["budget"] == 500


def test_same_session_lock_is_reused(registry):
    async def main():
        first = await registry.lock("session-a")
        second = await registry.lock("session-a")
        other = await registry.lock("session-b")

        assert first is second
        assert first is not other

    asyncio.run(main())


def test_same_session_updates_are_serialized(registry):
    async def increment() -> None:
        async with registry.acquire("session-a"):
            snapshot = registry.load_snapshot("session-a")
            current = snapshot or StateSnapshot(
                state=SessionState(),
                version=0,
            )
            state = current.state
            count = state.preferences.get("count", 0)

            # 故意让出事件循环，放大“读-改-写”竞争。
            await asyncio.sleep(0.01)
            state.preferences["count"] = count + 1

            registry.save_snapshot(
                "session-a",
                state,
                expected_version=current.version,
            )

    async def main() -> None:
        await asyncio.gather(*(increment() for _ in range(10)))

    asyncio.run(main())

    snapshot = registry.load_snapshot("session-a")
    assert snapshot is not None
    assert snapshot.state.preferences["count"] == 10


def test_different_sessions_do_not_block_each_other(registry):
    async def main() -> None:
        a_started = asyncio.Event()
        release_a = asyncio.Event()

        async def hold_a() -> None:
            async with registry.acquire("session-a"):
                a_started.set()
                await release_a.wait()

        async def use_b() -> str:
            await a_started.wait()
            async with registry.acquire("session-b"):
                return "session-b"

        task = asyncio.create_task(hold_a())
        await a_started.wait()

        try:
            result = await asyncio.wait_for(use_b(), timeout=0.5)
            assert result == "session-b"
        finally:
            release_a.set()
            await task

    asyncio.run(main())


@pytest.mark.parametrize("session_id", ["", "   "])
def test_blank_session_id_is_rejected(registry, session_id):
    with pytest.raises(ValueError, match="session_id"):
        registry.load_snapshot(session_id)

    with pytest.raises(ValueError, match="session_id"):
        registry.save_snapshot(
            session_id,
            sample_state(),
            expected_version=0,
        )

    with pytest.raises(ValueError, match="session_id"):
        registry.exists(session_id)

    with pytest.raises(ValueError, match="session_id"):
        registry.delete(session_id)

    with pytest.raises(ValueError, match="session_id"):
        asyncio.run(registry.lock(session_id))
