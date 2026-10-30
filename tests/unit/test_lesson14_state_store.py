import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from state.models import GoalStatus, SessionState
from state.store import StateStore, StateStoreError


def sample_state(budget: int = 500) -> SessionState:
    return SessionState(
        preferences={"style": "dry", "budget": budget},
        goals=[{"text": "买两瓶干型酒", "status": "done"}],
        constraints=["预算不超过 500 元"],
        progress={"选酒": "done"},
        facts={"产区": "波尔多"},
    )


@pytest.fixture
def db_path() -> Path:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson14-",
        suffix=".db",
        delete=False,
    ) as handle:
        path = Path(handle.name)

    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


@pytest.fixture
def store(db_path: Path) -> StateStore:
    store = StateStore(db_path)
    store.initialize()
    return store


def test_initialize_is_idempotent(store):
    store.initialize()

    state = sample_state()
    store.save("session-1", state)

    assert store.load("session-1") == state


def test_save_and_load_round_trip(store):
    state = sample_state()

    store.save("session-1", state)
    loaded = store.load("session-1")

    assert loaded == state
    assert loaded.preferences["style"] == "dry"
    assert loaded.goals[0].status is GoalStatus.DONE
    assert loaded.progress["选酒"] == "done"
    assert loaded.facts["产区"] == "波尔多"


def test_load_unknown_session_returns_none(store):
    assert store.load("missing") is None


def test_save_overwrites_existing_snapshot(store):
    store.save("session-1", sample_state(budget=500))
    store.save("session-1", sample_state(budget=800))

    loaded = store.load("session-1")

    assert loaded is not None
    assert loaded.preferences["budget"] == 800


def test_exists_and_delete(store):
    assert store.exists("session-1") is False

    store.save("session-1", sample_state())
    assert store.exists("session-1") is True

    assert store.delete("session-1") is True
    assert store.delete("session-1") is False
    assert store.exists("session-1") is False
    assert store.load("session-1") is None


def test_state_persists_across_store_instances(db_path):
    state = sample_state()

    first = StateStore(db_path)
    first.initialize()
    first.save("session-1", state)

    second = StateStore(db_path)
    second.initialize()

    assert second.load("session-1") == state


def test_initialize_creates_parent_directory():
    base = Path.cwd() / f"lesson14-{uuid.uuid4().hex}"
    db_path = base / "nested" / "data" / "state.db"

    try:
        store = StateStore(db_path)
        store.initialize()

        assert db_path.exists()
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.parametrize("session_id", ["", "   "])
def test_blank_session_id_is_rejected(store, session_id):
    with pytest.raises(ValueError, match="session_id"):
        store.save(session_id, sample_state())

    with pytest.raises(ValueError, match="session_id"):
        store.load(session_id)

    with pytest.raises(ValueError, match="session_id"):
        store.exists(session_id)

    with pytest.raises(ValueError, match="session_id"):
        store.delete(session_id)


def test_save_rejects_non_session_state(store):
    with pytest.raises(TypeError, match="SessionState"):
        store.save("session-1", {"preferences": {"style": "dry"}})


def test_corrupt_state_json_raises_store_error(store):
    store.save("session-1", sample_state())

    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        conn.execute(
            "UPDATE session_states SET state_json = ? WHERE session_id = ?",
            ("{not-json", "session-1"),
        )

    with pytest.raises(StateStoreError, match="状态数据损坏"):
        store.load("session-1")

    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        conn.execute(
            "UPDATE session_states SET state_json = ? WHERE session_id = ?",
            ('{"unknown_field": "x"}', "session-1"),
        )

    with pytest.raises(StateStoreError, match="状态数据损坏"):
        store.load("session-1")
