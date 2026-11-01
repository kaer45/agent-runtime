import asyncio
import tempfile
from pathlib import Path

import pytest

from state.extractor import StateDelta
from state.models import Goal, GoalStatus, SessionState
from state.service import StateApplyError, StateService, apply_delta
from state.session import SessionRegistry
from state.store import StateConflictError, StateStore


@pytest.fixture
def service() -> StateService:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson16-",
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


def test_get_missing_session_returns_version_zero(service):
    snapshot = asyncio.run(service.get("session-1"))

    assert snapshot.version == 0
    assert snapshot.state == SessionState()
    assert service.registry.exists("session-1") is False


def test_apply_creates_new_session_with_version_one(service):
    delta = StateDelta(
        preferences_set={"style": "dry"},
        goals_add=["买两瓶干型酒"],
    )

    snapshot = asyncio.run(service.apply("session-1", delta))

    assert snapshot.version == 1
    assert snapshot.state.preferences["style"] == "dry"
    assert snapshot.state.goals[0].text == "买两瓶干型酒"

    loaded = asyncio.run(service.get("session-1"))
    assert loaded.version == 1
    assert loaded.state == snapshot.state


def test_apply_all_delta_operations(service):
    async def main() -> None:
        await service.apply(
            "session-1",
            StateDelta(
                preferences_set={"style": "dry", "budget": 500},
                facts_set={"产区": "波尔多"},
                progress_set={"选酒": "in_progress", "比价": "pending"},
                constraints_add=["预算不超过 500 元"],
                goals_add=["买酒", "买酒杯"],
            ),
        )

        snapshot = await service.apply(
            "session-1",
            StateDelta(
                preferences_set={"budget": 800},
                preferences_remove=["style"],
                facts_set={"年份": "2019"},
                facts_remove=["产区"],
                progress_set={"选酒": "done"},
                progress_remove=["比价"],
                constraints_add=["不送人"],
                constraints_remove=["预算不超过 500 元"],
                goals_add=["再买两瓶"],
                goals_complete=["买酒"],
                goals_abandon=["买酒杯"],
            ),
        )

        assert snapshot.version == 2
        assert snapshot.state.preferences == {"budget": 800}
        assert snapshot.state.facts == {"年份": "2019"}
        assert snapshot.state.progress == {"选酒": "done"}
        assert snapshot.state.constraints == ["不送人"]
        assert [goal.text for goal in snapshot.state.goals] == [
            "买酒",
            "买酒杯",
            "再买两瓶",
        ]
        assert snapshot.state.goals[0].status is GoalStatus.DONE
        assert snapshot.state.goals[1].status is GoalStatus.ABANDONED
        assert snapshot.state.goals[2].status is GoalStatus.ACTIVE

    asyncio.run(main())


def test_empty_delta_does_not_create_or_bump(service):
    async def main() -> None:
        missing = await service.apply("missing", StateDelta())
        assert missing.version == 0
        assert service.registry.exists("missing") is False

        first = await service.apply(
            "session-1",
            StateDelta(preferences_set={"style": "dry"}),
        )
        second = await service.apply("session-1", StateDelta())

        assert first.version == 1
        assert second.version == 1
        assert second.state == first.state

    asyncio.run(main())


def test_expected_version_mismatch_raises_conflict(service):
    async def main() -> None:
        first = await service.apply(
            "session-1",
            StateDelta(preferences_set={"style": "dry"}),
        )
        second = await service.apply(
            "session-1",
            StateDelta(preferences_set={"budget": 500}),
            expected_version=first.version,
        )

        with pytest.raises(StateConflictError, match="版本冲突"):
            await service.apply(
                "session-1",
                StateDelta(preferences_set={"budget": 800}),
                expected_version=first.version,
            )

        current = await service.get("session-1")
        assert second.version == 2
        assert current.version == 2
        assert current.state.preferences == {"style": "dry", "budget": 500}

    asyncio.run(main())


def test_complete_missing_goal_raises_apply_error(service):
    async def main() -> None:
        await service.apply("session-1", StateDelta(goals_add=["买酒"]))

        with pytest.raises(StateApplyError, match="不存在"):
            await service.apply(
                "session-1",
                StateDelta(goals_complete=["不存在的目标"]),
            )

        current = await service.get("session-1")
        assert current.version == 1
        assert current.state.goals[0].status is GoalStatus.ACTIVE

    asyncio.run(main())


def test_complete_abandoned_goal_raises_apply_error(service):
    async def main() -> None:
        await service.apply("session-1", StateDelta(goals_add=["买酒"]))
        await service.apply("session-1", StateDelta(goals_abandon=["买酒"]))

        with pytest.raises(StateApplyError, match="放弃"):
            await service.apply(
                "session-1",
                StateDelta(goals_complete=["买酒"]),
            )

    asyncio.run(main())


def test_duplicate_goal_and_constraint_are_idempotent(service):
    delta = StateDelta(
        goals_add=["买酒"],
        constraints_add=["预算不超过 500 元"],
    )

    async def main() -> None:
        await service.apply("session-1", delta)
        snapshot = await service.apply("session-1", delta)

        assert snapshot.version == 1
        assert [goal.text for goal in snapshot.state.goals] == ["买酒"]
        assert snapshot.state.constraints == ["预算不超过 500 元"]

    asyncio.run(main())


def test_state_and_version_persist_across_service_instances(service):
    async def main() -> None:
        await service.apply(
            "session-1",
            StateDelta(
                preferences_set={"style": "dry"},
                goals_add=["买酒"],
            ),
        )

        other = StateService(SessionRegistry(service.registry.store))
        snapshot = await other.get("session-1")

        assert snapshot.version == 1
        assert snapshot.state.preferences["style"] == "dry"
        assert snapshot.state.goals[0].text == "买酒"

    asyncio.run(main())


def test_concurrent_apply_serializes_versions(service):
    async def add_goal(index: int):
        return await service.apply(
            "session-1",
            StateDelta(goals_add=[f"目标-{index}"]),
        )

    async def main() -> None:
        snapshots = await asyncio.gather(*(add_goal(i) for i in range(10)))

        assert sorted(snapshot.version for snapshot in snapshots) == list(
            range(1, 11)
        )

        current = await service.get("session-1")
        assert current.version == 10
        assert len(current.state.goals) == 10

    asyncio.run(main())


def test_apply_delta_does_not_mutate_input():
    state = SessionState(
        preferences={"style": "dry"},
        goals=[Goal(text="买酒")],
    )
    delta = StateDelta(
        preferences_set={"budget": 800},
        preferences_remove=["style"],
        constraints_add=["不送人"],
        goals_complete=["买酒"],
    )

    updated = apply_delta(state, delta)

    assert state.preferences == {"style": "dry"}
    assert state.constraints == []
    assert state.goals[0].status is GoalStatus.ACTIVE

    assert updated.preferences == {"budget": 800}
    assert updated.constraints == ["不送人"]
    assert updated.goals[0].status is GoalStatus.DONE


def test_store_snapshot_save_detects_stale_version(service):
    store = service.registry.store
    state = SessionState(preferences={"style": "dry"})

    assert store.save_snapshot("session-1", state, expected_version=0) == 1
    assert store.save_snapshot("session-1", state, expected_version=1) == 2

    with pytest.raises(StateConflictError, match="版本冲突"):
        store.save_snapshot("session-1", state, expected_version=1)


def test_blank_session_id_is_rejected(service):
    with pytest.raises(ValueError, match="session_id"):
        asyncio.run(service.get(""))

    with pytest.raises(ValueError, match="session_id"):
        asyncio.run(service.apply("   ", StateDelta()))
