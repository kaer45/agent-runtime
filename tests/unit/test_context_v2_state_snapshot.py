import pytest

from state.models import SessionState
from state.models import StateSnapshot as SnapshotFromModels
from state.store import StateSnapshot as SnapshotFromStore


def test_snapshot_has_a_single_definition():
    """StateSnapshot 已移到 state.models，旧路径必须仍然指向同一个类。"""
    assert SnapshotFromStore is SnapshotFromModels


def test_snapshot_keeps_state_and_version():
    snapshot = SnapshotFromModels(state=SessionState(), version=3)

    assert snapshot.version == 3
    assert isinstance(snapshot.state, SessionState)


def test_snapshot_rejects_negative_version():
    with pytest.raises(ValueError, match="version"):
        SnapshotFromModels(state=SessionState(), version=-1)


def test_snapshot_rejects_non_session_state():
    with pytest.raises(TypeError, match="SessionState"):
        SnapshotFromModels(state={"preferences": {}}, version=0)
