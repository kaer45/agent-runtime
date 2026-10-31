import asyncio
import tempfile
from pathlib import Path

import pytest

from state.models import SessionState
from state.session import SessionRegistry
from state.store import StateStore


def sample_state(budget: int = 500) -> SessionState:
    return SessionState(
        preferences={"style": "dry", "budget": budget},
        goals=[{"text": "买两瓶干型酒"}],
        constraints=["预算不超过 500 元"],
        progress={"选酒": "in_progress"},
        facts={"产区": "波尔多"},
    )


@pytest.fixture
def registry() -> SessionRegistry:
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
        yield SessionRegistry(store)
    finally:
        db_path.unlink(missing_ok=True)


def test_sessions_are_isolated(registry):
    registry.save("session-a", sample_state(budget=500))
    registry.save("session-b", sample_state(budget=800))

    assert registry.load("session-a").preferences["budget"] == 500
    assert registry.load("session-b").preferences["budget"] == 800


def test_missing_session_returns_empty_state(registry):
    assert registry.exists("missing") is False
    assert registry.load("missing") == SessionState()


def test_delete_removes_session_state(registry):
    registry.save("session-a", sample_state())

    assert registry.delete("session-a") is True
    assert registry.delete("session-a") is False
    assert registry.exists("session-a") is False
    assert registry.load("session-a") == SessionState()


def test_load_returns_independent_state_objects(registry):
    registry.save("session-a", sample_state(budget=500))

    first = registry.load("session-a")
    first.preferences["budget"] = 999

    second = registry.load("session-a")

    assert second.preferences["budget"] == 500


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
            state = registry.load("session-a")
            count = state.preferences.get("count", 0)
            # 故意让出事件循环，放大“读-改-写”竞争。
            await asyncio.sleep(0.01)
            state.preferences["count"] = count + 1
            registry.save("session-a", state)

    async def main() -> None:
        await asyncio.gather(*(increment() for _ in range(10)))

    asyncio.run(main())

    assert registry.load("session-a").preferences["count"] == 10


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


def test_update_runs_under_session_lock(registry):
    def mutate(state: SessionState) -> SessionState:
        state.preferences["style"] = "dry"
        return state

    result = asyncio.run(registry.update("session-a", mutate))

    assert result.preferences["style"] == "dry"
    assert registry.load("session-a").preferences["style"] == "dry"


def test_update_rejects_non_state_result(registry):
    with pytest.raises(TypeError, match="SessionState"):
        asyncio.run(
            registry.update("session-a", lambda state: {"preferences": {}})
        )

    assert registry.load("session-a") == SessionState()


@pytest.mark.parametrize("session_id", ["", "   "])
def test_blank_session_id_is_rejected(registry, session_id):
    with pytest.raises(ValueError, match="session_id"):
        registry.load(session_id)

    with pytest.raises(ValueError, match="session_id"):
        registry.save(session_id, sample_state())

    with pytest.raises(ValueError, match="session_id"):
        registry.exists(session_id)

    with pytest.raises(ValueError, match="session_id"):
        registry.delete(session_id)

    with pytest.raises(ValueError, match="session_id"):
        asyncio.run(registry.lock(session_id))

    with pytest.raises(ValueError, match="session_id"):
        asyncio.run(registry.update(session_id, lambda state: state))
