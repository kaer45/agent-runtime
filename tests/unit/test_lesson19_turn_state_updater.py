import asyncio
import tempfile
from pathlib import Path

import pytest

from state.extractor import StateDelta, StateExtractionError
from state.models import SessionState
from state.service import StateService
from state.session import SessionRegistry
from state.store import StateStore
from state.updater import StateUpdateResult, TurnStateUpdater


@pytest.fixture
def service() -> StateService:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson19-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    store = StateStore(db_path)
    store.initialize()

    try:
        yield StateService(SessionRegistry(store))
    finally:
        db_path.unlink(missing_ok=True)


class FakeExtractor:
    def __init__(self, deltas=()):
        self.deltas = list(deltas)
        self.calls = 0
        self.states = []

    async def extract(self, state: SessionState, messages):
        self.calls += 1
        self.states.append((state, list(messages)))
        if not self.deltas:
            raise AssertionError("FakeExtractor 没有更多 delta")
        item = self.deltas.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class BlockingExtractor(FakeExtractor):
    def __init__(self, deltas=()):
        super().__init__(deltas)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def extract(self, state: SessionState, messages):
        self.started.set()
        await self.release.wait()
        return await super().extract(state, messages)


class ConcurrencyExtractor:
    def __init__(self, deltas):
        self.deltas = list(deltas)
        self.active = 0
        self.max_active = 0

    async def extract(self, state: SessionState, messages):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return self.deltas.pop(0)


def make_updater(service, extractor) -> TurnStateUpdater:
    return TurnStateUpdater(extractor, service)


def test_applies_delta_and_bumps_version(service):
    extractor = FakeExtractor([StateDelta(preferences_set={"style": "dry"})])
    updater = make_updater(service, extractor)

    result = asyncio.run(
        updater.update("session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}])
    )

    assert isinstance(result, StateUpdateResult)
    assert result.ok is True
    assert result.applied is True
    assert result.duplicate is False
    assert result.error is None
    assert result.snapshot is not None
    assert result.snapshot.version == 1
    assert result.snapshot.state.preferences == {"style": "dry"}
    assert result.delta.preferences_set == {"style": "dry"}
    assert extractor.calls == 1


def test_empty_delta_is_a_noop(service):
    extractor = FakeExtractor([StateDelta()])
    updater = make_updater(service, extractor)

    result = asyncio.run(
        updater.update("session-1", "turn-1", [{"role": "user", "content": "随便聊聊"}])
    )

    assert result.ok is True
    assert result.applied is False
    assert result.duplicate is False
    assert result.snapshot is not None
    assert result.snapshot.version == 0
    assert service.registry.exists("session-1") is False


def test_same_turn_id_is_idempotent(service):
    extractor = FakeExtractor([StateDelta(preferences_set={"style": "dry"})])
    updater = make_updater(service, extractor)

    async def main():
        first = await updater.update(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        second = await updater.update(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        return first, second

    first, second = asyncio.run(main())

    assert first.applied is True
    assert first.duplicate is False
    assert second.duplicate is True
    assert second.snapshot == first.snapshot
    assert extractor.calls == 1

    snapshot = asyncio.run(service.get("session-1"))
    assert snapshot.version == 1


def test_concurrent_same_turn_id_applies_once(service):
    extractor = FakeExtractor([StateDelta(progress_set={"选酒": "done"})])
    updater = make_updater(service, extractor)

    async def main():
        return await asyncio.gather(
            updater.update("session-1", "turn-1", [{"role": "user", "content": "完成"}]),
            updater.update("session-1", "turn-1", [{"role": "user", "content": "完成"}]),
        )

    results = asyncio.run(main())

    assert extractor.calls == 1
    assert sum(1 for result in results if result.duplicate) == 1
    assert sum(
        1 for result in results if result.applied and not result.duplicate
    ) == 1

    snapshot = asyncio.run(service.get("session-1"))
    assert snapshot.version == 1


def test_same_session_updates_are_serialized(service):
    extractor = ConcurrencyExtractor(
        [
            StateDelta(progress_set={"a": "1"}),
            StateDelta(progress_set={"b": "1"}),
        ]
    )
    updater = make_updater(service, extractor)

    async def main():
        return await asyncio.gather(
            updater.update("session-1", "turn-1", [{"role": "user", "content": "a"}]),
            updater.update("session-1", "turn-2", [{"role": "user", "content": "b"}]),
        )

    results = asyncio.run(main())

    assert all(result.applied for result in results)
    assert extractor.max_active == 1

    snapshot = asyncio.run(service.get("session-1"))
    assert snapshot.version == 2
    assert snapshot.state.progress == {"a": "1", "b": "1"}


def test_extraction_failure_is_returned_and_can_retry(service):
    extractor = FakeExtractor(
        [
            StateExtractionError("模型超时"),
            StateDelta(preferences_set={"style": "dry"}),
        ]
    )
    updater = make_updater(service, extractor)

    async def main():
        first = await updater.update(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        second = await updater.update(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        return first, second

    first, second = asyncio.run(main())

    assert first.failed is True
    assert "模型超时" in first.error
    assert first.snapshot is not None
    assert first.snapshot.version == 0
    assert second.applied is True
    assert extractor.calls == 2

    snapshot = asyncio.run(service.get("session-1"))
    assert snapshot.version == 1


def test_stale_expected_version_fails_without_extracting(service):
    extractor = FakeExtractor([StateDelta(preferences_set={"style": "dry"})])
    updater = make_updater(service, extractor)

    result = asyncio.run(
        updater.update(
            "session-1",
            "turn-1",
            [{"role": "user", "content": "我喜欢干型"}],
            expected_version=5,
        )
    )

    assert result.failed is True
    assert "状态版本冲突" in result.error
    assert result.snapshot is not None
    assert result.snapshot.version == 0
    assert extractor.calls == 0
    assert service.registry.exists("session-1") is False


def test_schedule_does_not_block(service):
    extractor = BlockingExtractor([StateDelta(preferences_set={"style": "dry"})])
    updater = make_updater(service, extractor)

    async def main():
        task = updater.schedule(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        await asyncio.wait_for(extractor.started.wait(), timeout=1)
        assert task.done() is False

        extractor.release.set()
        return await task

    result = asyncio.run(main())

    assert result.applied is True
    assert result.snapshot.version == 1


def test_wait_ensures_read_your_writes(service):
    extractor = BlockingExtractor([StateDelta(preferences_set={"style": "dry"})])
    updater = make_updater(service, extractor)

    async def main():
        task = updater.schedule(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        await asyncio.wait_for(extractor.started.wait(), timeout=1)

        async def release():
            extractor.release.set()

        asyncio.create_task(release())
        await updater.wait("session-1")
        assert task.done() is True

        return await service.get("session-1")

    snapshot = asyncio.run(main())

    assert snapshot.version == 1
    assert snapshot.state.preferences == {"style": "dry"}


def test_background_failure_is_captured(service):
    extractor = FakeExtractor([StateExtractionError("提取失败")])
    updater = make_updater(service, extractor)

    async def main():
        task = updater.schedule(
            "session-1", "turn-1", [{"role": "user", "content": "你好"}]
        )
        return await task

    result = asyncio.run(main())

    assert result.failed is True
    assert "提取失败" in result.error
    assert result.snapshot is not None
    assert result.snapshot.version == 0


def test_schedule_same_turn_id_still_applies_once(service):
    extractor = BlockingExtractor([StateDelta(preferences_set={"style": "dry"})])
    updater = make_updater(service, extractor)

    async def main():
        first = updater.schedule(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        second = updater.schedule(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        await asyncio.wait_for(extractor.started.wait(), timeout=1)
        extractor.release.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(main())

    assert extractor.calls == 1
    assert sum(1 for result in results if result.duplicate) == 1

    snapshot = asyncio.run(service.get("session-1"))
    assert snapshot.version == 1


@pytest.mark.parametrize(
    ("session_id", "turn_id"),
    [("", "turn-1"), ("session-1", "")],
)
def test_invalid_ids_are_rejected(service, session_id, turn_id):
    updater = make_updater(service, FakeExtractor())

    with pytest.raises(ValueError):
        asyncio.run(updater.update(session_id, turn_id, []))
