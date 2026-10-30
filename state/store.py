"""用 SQLite 持久化会话状态快照。

本模块只负责保存/读取 ``SessionState``；状态合并、版本和冲突处理
留给第 16 课的 ``StateService``，多会话隔离留给第 15 课。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from state.models import SessionState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_states (
    session_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_UPSERT = """
INSERT INTO session_states (session_id, state_json, updated_at)
VALUES (?, ?, ?)
ON CONFLICT(session_id) DO UPDATE SET
    state_json = excluded.state_json,
    updated_at = excluded.updated_at
"""


class StateStoreError(RuntimeError):
    """状态保存/读取失败，或数据库中的状态已损坏。"""


class StateStore:
    """SQLite 状态存储。

    Example:
        store = StateStore("data/state.db")
        store.initialize()
        store.save("session-1", state)
        restored = store.load("session-1")
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        """创建父目录和表；可重复调用。"""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as conn:
                conn.execute(_SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            raise StateStoreError(f"初始化状态库失败：{exc}") from exc

    def save(self, session_id: str, state: SessionState) -> None:
        """保存或覆盖一个会话的状态快照。"""
        session_id = self._normalize_session_id(session_id)
        if not isinstance(state, SessionState):
            raise TypeError("state 必须是 SessionState")

        try:
            payload = state.model_dump_json()
        except Exception as exc:
            raise StateStoreError(f"状态序列化失败：{exc}") from exc

        try:
            with self._connection() as conn:
                conn.execute(
                    _UPSERT,
                    (
                        session_id,
                        payload,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
        except sqlite3.Error as exc:
            raise StateStoreError(f"保存状态失败：{exc}") from exc

    def load(self, session_id: str) -> SessionState | None:
        """读取会话状态；不存在返回 ``None``。"""
        session_id = self._normalize_session_id(session_id)

        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT state_json FROM session_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError(f"读取状态失败：{exc}") from exc

        if row is None:
            return None

        try:
            return SessionState.model_validate(json.loads(row[0]))
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise StateStoreError(f"状态数据损坏：{exc}") from exc

    def exists(self, session_id: str) -> bool:
        """会话状态是否存在。"""
        session_id = self._normalize_session_id(session_id)

        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM session_states WHERE session_id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError(f"检查状态失败：{exc}") from exc

        return row is not None

    def delete(self, session_id: str) -> bool:
        """删除会话状态；返回是否真的删掉了一行。"""
        session_id = self._normalize_session_id(session_id)

        try:
            with self._connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM session_states WHERE session_id = ?",
                    (session_id,),
                )
                deleted = cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise StateStoreError(f"删除状态失败：{exc}") from exc

        return deleted

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        if not isinstance(session_id, str):
            raise TypeError("session_id 必须是字符串")

        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id 不能为空")
        return session_id