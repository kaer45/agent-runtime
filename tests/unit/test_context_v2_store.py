import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from history.errors import HistoryCoverageConflict, HistoryStoreError
from history.store import ArchiveResult, HistoryStore, SCHEMA_VERSION
from history.models import RollingSummary


def user(text: str = "你好") -> dict:
    return {"role": "user", "content": text}


def assistant(text: str = "好的") -> dict:
    return {"role": "assistant", "content": text}


def simple_turn(text: str = "你好") -> list[dict]:
    return [user(text), assistant()]


@pytest.fixture
def store(tmp_path) -> HistoryStore:
    instance = HistoryStore(tmp_path / "history.db", counter=len)
    instance.initialize()
    return instance


def append(store: HistoryStore, turn_id: str, text: str = "你好") -> bool:
    return store.append_turn_once("session-1", turn_id, simple_turn(text))


# --------------------------------------------------------------------- schema


def test_initialize_creates_all_tables(tmp_path):
    path = tmp_path / "history.db"
    HistoryStore(path).initialize()

    conn = sqlite3.connect(path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    version = conn.execute("SELECT version FROM history_schema_meta").fetchall()
    conn.close()

    assert {
        "session_turns",
        "session_turns_archive",
        "session_summaries",
        "session_turn_counters",
        "history_schema_meta",
    } <= tables
    assert version == [(SCHEMA_VERSION,)]


def test_initialize_is_idempotent(store):
    store.initialize()
    store.initialize()

    assert store.count_turns("session-1") == 0


def test_old_schema_without_meta_fails_fast(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session_messages (id INTEGER PRIMARY KEY, session_id TEXT)"
    )
    conn.commit()
    conn.close()

    with pytest.raises(HistoryStoreError, match="旧版"):
        HistoryStore(path).initialize()

    conn = sqlite3.connect(path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    assert tables == {"session_messages"}


def test_legacy_table_alongside_meta_fails_fast(tmp_path):
    path = tmp_path / "mixed.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE history_schema_meta (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO history_schema_meta VALUES (2)")
    conn.execute("CREATE TABLE session_messages (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(HistoryStoreError, match="旧版"):
        HistoryStore(path).initialize()


def test_wrong_schema_version_fails_fast(tmp_path):
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE history_schema_meta (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO history_schema_meta VALUES (1)")
    conn.commit()
    conn.close()

    with pytest.raises(HistoryStoreError, match="版本"):
        HistoryStore(path).initialize()


def test_empty_meta_fails_fast(tmp_path):
    path = tmp_path / "empty-meta.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE history_schema_meta (version INTEGER NOT NULL)")
    conn.execute("CREATE TABLE session_turns (session_id TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(HistoryStoreError, match="恰好一行"):
        HistoryStore(path).initialize()


def test_missing_table_with_current_version_fails_fast(tmp_path):
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE history_schema_meta (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO history_schema_meta VALUES (2)")
    conn.commit()
    conn.close()

    with pytest.raises(HistoryStoreError, match="缺少表"):
        HistoryStore(path).initialize()


# --------------------------------------------------------------------- append


def test_first_turn_gets_seq_one(store):
    assert append(store, "turn-1") is True

    snapshot = store.load("session-1")
    assert [turn.seq for turn in snapshot.turns] == [1]


def test_seq_increases_per_session(store):
    append(store, "turn-1")
    append(store, "turn-2")
    append(store, "turn-3")

    snapshot = store.load("session-1")
    assert [turn.seq for turn in snapshot.turns] == [1, 2, 3]


def test_seq_is_isolated_between_sessions(store):
    append(store, "turn-1")
    store.append_turn_once("session-2", "other-1", simple_turn())

    assert [turn.seq for turn in store.load("session-2").turns] == [1]


def test_duplicate_turn_id_is_idempotent(store):
    assert append(store, "turn-1") is True
    assert append(store, "turn-1") is False

    assert store.count_turns("session-1") == 1


def test_token_count_is_stored_from_the_shared_counter(store):
    append(store, "turn-1", "hello")

    turn = store.load("session-1").turns[0]
    # len 计数器：两条消息各 4 结构开销 + content 长度
    assert turn.token_count == (4 + len("hello")) + (4 + len("好的"))


def test_invalid_turn_is_rejected_before_writing(store):
    with pytest.raises(HistoryStoreError, match="user"):
        store.append_turn_once(
            "session-1",
            "turn-1",
            [assistant(), assistant()],
        )

    assert store.count_turns("session-1") == 0


def test_blank_ids_are_rejected(store):
    with pytest.raises(ValueError, match="session_id"):
        store.append_turn_once(" ", "turn-1", simple_turn())

    with pytest.raises(ValueError, match="turn_id"):
        store.append_turn_once("session-1", " ", simple_turn())


def test_concurrent_appends_allocate_unique_seq(store):
    count = 8

    def worker(index: int) -> bool:
        return append(store, f"turn-{index}", f"问题{index}")

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(worker, range(count)))

    assert all(results)
    snapshot = store.load("session-1")
    assert [turn.seq for turn in snapshot.turns] == list(range(1, count + 1))


# ----------------------------------------------------------------------- load


def test_load_of_unknown_session_is_empty(store):
    snapshot = store.load("missing")

    assert snapshot.turns == ()
    assert snapshot.summary == RollingSummary()
    assert snapshot.covered_through_seq == 0


def test_load_returns_turns_in_ascending_order(store):
    for index in range(3):
        append(store, f"turn-{index}")

    assert [turn.seq for turn in store.load("session-1").turns] == [1, 2, 3]


def test_load_limit_turns_returns_latest_n_in_ascending_order(store):
    for index in range(3):
        append(store, f"turn-{index}")

    snapshot = store.load("session-1", limit_turns=2)

    assert [turn.seq for turn in snapshot.turns] == [2, 3]


@pytest.mark.parametrize("limit", [0, -1, True, "2"])
def test_load_rejects_bad_limit(store, limit):
    with pytest.raises((TypeError, ValueError), match="limit_turns"):
        store.load("session-1", limit_turns=limit)


def test_load_rejects_corrupted_messages(store):
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "INSERT INTO session_turns "
        "(session_id, seq, turn_id, messages_json, token_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("session-1", 1, "turn-1", "{not json", 5, "2026-11-17"),
    )
    conn.commit()
    conn.close()

    with pytest.raises(HistoryStoreError, match="损坏"):
        store.load("session-1")


def test_load_rejects_corrupted_summary(store):
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "INSERT INTO session_summaries "
        "(session_id, summary_json, covered_through_seq, updated_at) "
        "VALUES (?, ?, ?, ?)",
        ("session-1", "[]", 0, "2026-11-17"),
    )
    conn.commit()
    conn.close()

    with pytest.raises(HistoryStoreError, match="损坏"):
        store.load("session-1")


# ---------------------------------------------------------------- CAS summary


def test_save_summary_cas_advances_coverage(store):
    for index in range(3):
        append(store, f"turn-{index}")

    store.save_summary_cas(
        "session-1",
        RollingSummary(text="前两轮概括"),
        covered_through_seq=2,
        expected_covered_through_seq=0,
    )

    snapshot = store.load("session-1")
    assert snapshot.covered_through_seq == 2
    assert [turn.seq for turn in snapshot.turns] == [3]
    assert snapshot.summary.text == "前两轮概括"


def test_save_summary_cas_conflict_writes_nothing(store):
    for index in range(3):
        append(store, f"turn-{index}")

    store.save_summary_cas(
        "session-1",
        RollingSummary(text="第一轮"),
        covered_through_seq=1,
        expected_covered_through_seq=0,
    )

    with pytest.raises(HistoryCoverageConflict):
        store.save_summary_cas(
            "session-1",
            RollingSummary(text="过期摘要"),
            covered_through_seq=3,
            expected_covered_through_seq=0,
        )

    snapshot = store.load("session-1")
    assert snapshot.covered_through_seq == 1
    assert snapshot.summary.text == "第一轮"


def test_coverage_can_only_move_forward(store):
    append(store, "turn-1")
    append(store, "turn-2")
    store.save_summary_cas(
        "session-1",
        RollingSummary(text="第一轮"),
        covered_through_seq=1,
        expected_covered_through_seq=0,
    )

    with pytest.raises(HistoryStoreError, match="只能前进"):
        store.save_summary_cas(
            "session-1",
            RollingSummary(text="x"),
            covered_through_seq=1,
            expected_covered_through_seq=1,
        )


def test_coverage_must_point_to_existing_turn(store):
    append(store, "turn-1")

    with pytest.raises(HistoryStoreError, match="不存在"):
        store.save_summary_cas(
            "session-1",
            RollingSummary(text="x"),
            covered_through_seq=9,
            expected_covered_through_seq=0,
        )


def test_advancing_coverage_requires_non_empty_summary(store):
    append(store, "turn-1")

    with pytest.raises(HistoryStoreError, match="不能为空"):
        store.save_summary_cas(
            "session-1",
            RollingSummary(text="   "),
            covered_through_seq=1,
            expected_covered_through_seq=0,
        )


# -------------------------------------------------------------------- archive


def test_archive_without_coverage_is_noop(store):
    append(store, "turn-1")
    append(store, "turn-2")

    result = store.archive_covered("session-1")

    assert result == ArchiveResult(archived_count=0, remaining_count=2)


def test_archive_moves_covered_turns(store):
    for index in range(4):
        append(store, f"turn-{index}")

    store.save_summary_cas(
        "session-1",
        RollingSummary(text="前三轮"),
        covered_through_seq=3,
        expected_covered_through_seq=0,
    )

    result = store.archive_covered("session-1")

    assert result == ArchiveResult(archived_count=3, remaining_count=1)
    assert [turn.seq for turn in store.load("session-1").turns] == [4]


def test_archive_is_idempotent(store):
    for index in range(3):
        append(store, f"turn-{index}")

    store.save_summary_cas(
        "session-1",
        RollingSummary(text="覆盖"),
        covered_through_seq=3,
        expected_covered_through_seq=0,
    )

    first = store.archive_covered("session-1")
    second = store.archive_covered("session-1")

    assert first.archived_count == 3
    assert second == ArchiveResult(archived_count=0, remaining_count=0)


def test_archive_keeps_rows_in_archive_table(store):
    append(store, "turn-1")
    store.save_summary_cas(
        "session-1",
        RollingSummary(text="覆盖"),
        covered_through_seq=1,
        expected_covered_through_seq=0,
    )
    store.archive_covered("session-1")

    conn = sqlite3.connect(store.db_path)
    archived = conn.execute(
        "SELECT seq FROM session_turns_archive WHERE session_id = ?",
        ("session-1",),
    ).fetchall()
    conn.close()

    assert archived == [(1,)]


def test_archive_does_not_reset_seq_counter(store):
    for index in range(3):
        append(store, f"turn-{index}")

    store.save_summary_cas(
        "session-1",
        RollingSummary(text="覆盖"),
        covered_through_seq=3,
        expected_covered_through_seq=0,
    )
    store.archive_covered("session-1")

    append(store, "turn-4")

    assert [turn.seq for turn in store.load("session-1").turns] == [4]


# ---------------------------------------------------------------------- clear


def test_clear_removes_everything(store):
    append(store, "turn-1")
    store.save_summary_cas(
        "session-1",
        RollingSummary(text="覆盖"),
        covered_through_seq=1,
        expected_covered_through_seq=0,
    )
    store.archive_covered("session-1")

    assert store.clear("session-1") is True
    assert store.clear("session-1") is False


def test_clear_keeps_other_sessions(store):
    append(store, "turn-1")
    store.append_turn_once("session-2", "turn-1", simple_turn())

    store.clear("session-1")

    assert store.count_turns("session-1") == 0
    assert store.count_turns("session-2") == 1
