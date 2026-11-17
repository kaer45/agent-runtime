"""turn 级历史存储（v2）。

五张表：

- ``session_turns``：主表，``load`` 唯一读取的表；
- ``session_turns_archive``：冷数据，不自动进 prompt；
- ``session_summaries``：唯一滚动摘要 + ``covered_through_seq``；
- ``session_turn_counters``：``seq`` 的单调分配器，归档不重置；
- ``history_schema_meta``：schema 版本，用于旧库 fail fast。

写入事务一律 ``BEGIN IMMEDIATE``，``seq`` 分配和插入在同一事务内完成；
只有 SQLite ``locked`` / ``busy`` 做有限重试。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from core.tokens import TokenCounter, count_messages, estimate_tokens
from history.errors import (
    HistoryCoverageConflict,
    HistoryStoreError,
)
from history.turns import (
    HistorySnapshot,
    RollingSummary,
    TurnRecord,
    validate_turn_messages,
)

SCHEMA_VERSION = 2

SOFT_ARCHIVE_LIMIT = 600
HARD_ARCHIVE_LIMIT = 1000

_HISTORY_TABLES = frozenset(
    {
        "session_turns",
        "session_turns_archive",
        "session_summaries",
        "session_turn_counters",
        "history_schema_meta",
    }
)

_LEGACY_TABLES = frozenset({"session_messages", "session_messages_archive"})

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS session_turns (
        session_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        turn_id TEXT NOT NULL,
        messages_json TEXT NOT NULL,
        token_count INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (session_id, seq),
        UNIQUE (session_id, turn_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_turns_archive (
        session_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        turn_id TEXT NOT NULL,
        messages_json TEXT NOT NULL,
        token_count INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        archived_at TEXT NOT NULL,
        PRIMARY KEY (session_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_summaries (
        session_id TEXT PRIMARY KEY,
        summary_json TEXT NOT NULL,
        covered_through_seq INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_turn_counters (
        session_id TEXT PRIMARY KEY,
        next_seq INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS history_schema_meta (
        version INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_session_turns_session_seq
    ON session_turns(session_id, seq)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_session_turns_archive_session_seq
    ON session_turns_archive(session_id, seq)
    """,
)

_ARCHIVE_INSERT = """
INSERT OR IGNORE INTO session_turns_archive (
    session_id, seq, turn_id, messages_json, token_count, created_at, archived_at
)
SELECT session_id, seq, turn_id, messages_json, token_count, created_at, ?
FROM session_turns
WHERE session_id = ? AND seq <= ?
"""

_SUMMARY_UPSERT = """
INSERT INTO session_summaries (
    session_id, summary_json, covered_through_seq, updated_at
)
VALUES (?, ?, ?, ?)
ON CONFLICT(session_id) DO UPDATE SET
    summary_json = excluded.summary_json,
    covered_through_seq = excluded.covered_through_seq,
    updated_at = excluded.updated_at
"""


@dataclass(frozen=True)
class ArchiveResult:
    """一次归档的结果。"""

    archived_count: int
    remaining_count: int


class HistoryStore:
    """SQLite turn 级历史存储。

    ``load`` 只读主表；归档表是冷数据，不自动重新进入 prompt。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        counter: TokenCounter = estimate_tokens,
        timeout: float = 5.0,
        max_retries: int = 3,
    ):
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise TypeError("max_retries 必须是整数")
        if max_retries < 0:
            raise ValueError("max_retries 不能为负数")

        self.db_path = Path(db_path)
        self.counter = counter
        self.timeout = timeout
        self.max_retries = max_retries

    # ------------------------------------------------------------------ schema

    def initialize(self) -> None:
        """创建 schema 或校验版本；旧库一律 fail fast，不做静默迁移。"""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as conn:
                is_new = self._inspect_schema(conn)

                for statement in _SCHEMA_STATEMENTS:
                    conn.execute(statement)

                if is_new:
                    conn.execute(
                        "INSERT INTO history_schema_meta (version) VALUES (?)",
                        (SCHEMA_VERSION,),
                    )
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"初始化历史库失败：{exc}") from exc

    @staticmethod
    def _inspect_schema(conn: sqlite3.Connection) -> bool:
        """返回是否新库；不兼容时直接抛错。

        必须在 ``CREATE TABLE IF NOT EXISTS`` 之前调用，否则新表会掩盖旧库。
        """
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        history_tables = tables & (_HISTORY_TABLES | _LEGACY_TABLES)

        if not history_tables:
            return True

        if "history_schema_meta" not in tables:
            raise HistoryStoreError(
                "检测到旧版历史 schema（缺少 history_schema_meta）："
                "请删除或迁移数据库后重试"
            )

        if tables & _LEGACY_TABLES:
            raise HistoryStoreError(
                "检测到旧版历史表 session_messages：请删除或迁移数据库后重试"
            )

        rows = conn.execute("SELECT version FROM history_schema_meta").fetchall()
        if len(rows) != 1:
            raise HistoryStoreError(
                f"history_schema_meta 必须恰好一行，实际 {len(rows)} 行："
                "请删除后重建"
            )

        version = int(rows[0][0])
        if version != SCHEMA_VERSION:
            raise HistoryStoreError(
                f"历史 schema 版本 {version} 与当前 {SCHEMA_VERSION} 不兼容："
                "请删除或迁移数据库"
            )

        missing = _HISTORY_TABLES - tables
        if missing:
            raise HistoryStoreError(f"历史 schema 缺少表：{sorted(missing)}")

        return False

    # ------------------------------------------------------------------ 写入

    def append_turn_once(
        self,
        session_id: str,
        turn_id: str,
        messages: Sequence[dict],
    ) -> bool:
        """追加一轮；``turn_id`` 已存在时返回 ``False``。"""
        session_id = _normalize_id("session_id", session_id)
        turn_id = _normalize_id("turn_id", turn_id)

        messages = tuple(messages)
        validate_turn_messages(messages)

        token_count = count_messages(self.counter, messages)
        payload = json.dumps(list(messages), ensure_ascii=False)
        created_at = _now()

        def operation(conn: sqlite3.Connection) -> bool:
            existing = conn.execute(
                "SELECT 1 FROM session_turns "
                "WHERE session_id = ? AND turn_id = ?",
                (session_id, turn_id),
            ).fetchone()
            if existing is not None:
                return False

            row = conn.execute(
                "SELECT next_seq FROM session_turn_counters WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            next_seq = 1 if row is None else int(row[0])

            try:
                conn.execute(
                    "INSERT INTO session_turns ("
                    "session_id, seq, turn_id, messages_json, token_count, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        next_seq,
                        turn_id,
                        payload,
                        token_count,
                        created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                duplicate = conn.execute(
                    "SELECT 1 FROM session_turns "
                    "WHERE session_id = ? AND turn_id = ?",
                    (session_id, turn_id),
                ).fetchone()
                if duplicate is not None:
                    return False
                raise HistoryStoreError(
                    "seq 冲突："
                    f"session={session_id}, seq={next_seq}，不重试"
                ) from exc

            conn.execute(
                "INSERT INTO session_turn_counters (session_id, next_seq) "
                "VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "next_seq = excluded.next_seq",
                (session_id, next_seq + 1),
            )
            return True

        return bool(self._run_write(operation))

    def save_summary_cas(
        self,
        session_id: str,
        summary: RollingSummary,
        *,
        covered_through_seq: int,
        expected_covered_through_seq: int,
    ) -> None:
        """用 CAS 推进摘要覆盖水位；失败时不写任何内容。"""
        session_id = _normalize_id("session_id", session_id)
        if not isinstance(summary, RollingSummary):
            raise TypeError("summary 必须是 RollingSummary")

        _require_non_negative("covered_through_seq", covered_through_seq)
        _require_non_negative(
            "expected_covered_through_seq", expected_covered_through_seq
        )

        payload = json.dumps({"text": summary.text}, ensure_ascii=False)
        updated_at = _now()

        def operation(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT covered_through_seq FROM session_summaries "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            current = 0 if row is None else int(row[0])

            if current != expected_covered_through_seq:
                raise HistoryCoverageConflict(
                    "摘要覆盖冲突："
                    f"session={session_id}, current={current}, "
                    f"expected={expected_covered_through_seq}"
                )

            if covered_through_seq <= expected_covered_through_seq:
                raise HistoryStoreError(
                    "covered_through_seq 只能前进："
                    f"{covered_through_seq} <= {expected_covered_through_seq}"
                )

            exists = conn.execute(
                "SELECT 1 FROM session_turns WHERE session_id = ? AND seq = ?",
                (session_id, covered_through_seq),
            ).fetchone()
            if exists is None:
                raise HistoryStoreError(
                    f"covered_through_seq={covered_through_seq} "
                    f"在主表不存在：session={session_id}"
                )

            if not summary.text.strip():
                raise HistoryStoreError("推进覆盖率时摘要不能为空")

            conn.execute(
                _SUMMARY_UPSERT,
                (session_id, payload, covered_through_seq, updated_at),
            )

        self._run_write(operation)

    def archive_covered(self, session_id: str) -> ArchiveResult:
        """把 ``seq <= covered_through_seq`` 的 turn 移到归档表。"""
        session_id = _normalize_id("session_id", session_id)

        def operation(conn: sqlite3.Connection) -> ArchiveResult:
            row = conn.execute(
                "SELECT covered_through_seq FROM session_summaries "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            coverage = 0 if row is None else int(row[0])

            if coverage == 0:
                return ArchiveResult(
                    archived_count=0,
                    remaining_count=_count_turns(conn, session_id),
                )

            conn.execute(
                _ARCHIVE_INSERT,
                (_now(), session_id, coverage),
            )
            cursor = conn.execute(
                "DELETE FROM session_turns WHERE session_id = ? AND seq <= ?",
                (session_id, coverage),
            )
            return ArchiveResult(
                archived_count=max(cursor.rowcount, 0),
                remaining_count=_count_turns(conn, session_id),
            )

        return self._run_write(operation)

    def clear(self, session_id: str) -> bool:
        """删除该 session 的主表、归档、摘要和计数器；返回是否删掉内容。"""
        session_id = _normalize_id("session_id", session_id)

        def operation(conn: sqlite3.Connection) -> bool:
            deleted = 0
            for table in (
                "session_turns",
                "session_turns_archive",
                "session_summaries",
                "session_turn_counters",
            ):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE session_id = ?",  # noqa: S608
                    (session_id,),
                )
                deleted += max(cursor.rowcount, 0)
            return deleted > 0

        return bool(self._run_write(operation))

    # ------------------------------------------------------------------ 读取

    def load(
        self,
        session_id: str,
        *,
        limit_turns: int | None = None,
    ) -> HistorySnapshot:
        """读取未覆盖 turn 与摘要。

        ``limit_turns`` 只用于展示 / API 预览，不能用于压缩判断。
        """
        session_id = _normalize_id("session_id", session_id)
        limit = _normalize_limit(limit_turns)

        try:
            with self._connection() as conn:
                summary_row = conn.execute(
                    "SELECT summary_json, covered_through_seq "
                    "FROM session_summaries WHERE session_id = ?",
                    (session_id,),
                ).fetchone()

                if summary_row is None:
                    summary = RollingSummary()
                    coverage = 0
                else:
                    summary = self._decode_summary(summary_row[0], session_id)
                    coverage = int(summary_row[1])

                rows = conn.execute(
                    "SELECT seq, turn_id, messages_json, token_count, created_at "
                    "FROM session_turns "
                    "WHERE session_id = ? AND seq > ? "
                    "ORDER BY seq ASC",
                    (session_id, coverage),
                ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"读取历史失败：{exc}") from exc

        if limit is not None:
            rows = rows[-limit:]

        turns = tuple(self._decode_turn(row, session_id) for row in rows)
        return HistorySnapshot(
            turns=turns,
            summary=summary,
            covered_through_seq=coverage,
        )

    def count_turns(self, session_id: str) -> int:
        """主表 turn 总数（含已覆盖但尚未归档的 turn）。"""
        session_id = _normalize_id("session_id", session_id)

        try:
            with self._connection() as conn:
                return _count_turns(conn, session_id)
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"统计历史轮数失败：{exc}") from exc

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _decode_summary(payload: str, session_id: str) -> RollingSummary:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HistoryStoreError(
                f"摘要数据损坏：session={session_id}"
            ) from exc

        if not isinstance(data, dict):
            raise HistoryStoreError(
                f"摘要数据损坏：session={session_id}，不是 JSON 对象"
            )

        text = data.get("text", "")
        if not isinstance(text, str):
            raise HistoryStoreError(
                f"摘要数据损坏：session={session_id}，text 不是字符串"
            )
        return RollingSummary(text=text)

    @staticmethod
    def _decode_turn(row: tuple, session_id: str) -> TurnRecord:
        seq, turn_id, messages_json, token_count, created_at = row

        try:
            raw_messages = json.loads(messages_json)
        except json.JSONDecodeError as exc:
            raise HistoryStoreError(
                f"turn 数据损坏：session={session_id}, seq={seq}"
            ) from exc

        if not isinstance(raw_messages, list):
            raise HistoryStoreError(
                f"turn 数据损坏：session={session_id}, seq={seq}，"
                "messages_json 不是数组"
            )

        try:
            return TurnRecord(
                turn_id=turn_id,
                seq=int(seq),
                messages=tuple(raw_messages),
                token_count=int(token_count),
                created_at=created_at,
            )
        except (TypeError, ValueError) as exc:
            raise HistoryStoreError(
                f"turn 数据损坏：session={session_id}, seq={seq}：{exc}"
            ) from exc

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=self.timeout)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` 写事务：写锁在事务开始时就拿到。"""
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.timeout,
            isolation_level=None,
        )
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _run_write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        for attempt in range(self.max_retries + 1):
            try:
                with self._write_transaction() as conn:
                    return operation(conn)
            except sqlite3.OperationalError as exc:
                if attempt >= self.max_retries or not _is_busy(exc):
                    raise HistoryStoreError(
                        f"历史库写入失败：{exc}"
                    ) from exc
                time.sleep(0.05 * (attempt + 1))

        raise HistoryStoreError("历史库写入失败：重试次数已用完")  # pragma: no cover


def _count_turns(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM session_turns WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row[0])


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_id(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是字符串")

    value = value.strip()
    if not value:
        raise ValueError(f"{name} 不能为空")
    return value


def _normalize_limit(limit_turns: int | None) -> int | None:
    if limit_turns is None:
        return None

    if isinstance(limit_turns, bool) or not isinstance(limit_turns, int):
        raise TypeError("limit_turns 必须是正整数或 None")
    if limit_turns <= 0:
        raise ValueError("limit_turns 必须是正整数或 None")

    return limit_turns


def _require_non_negative(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数")
    if value < 0:
        raise ValueError(f"{name} 不能为负数")
