"""用 SQLite 持久化会话状态快照。

本模块负责三件事：

1. 把 ``SessionState`` 序列化成 JSON 存进 SQLite；
2. 维护每条会话状态的 ``version``；
3. 提供乐观锁保存接口 ``save_snapshot()``。

状态合并、delta 应用和业务语义校验由 ``StateService`` 负责；
多会话隔离和同会话串行化由 ``SessionRegistry`` 负责。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from state.models import SessionState

# version 默认 0 是为了兼容第 14/15 课创建的旧表：
# 旧行执行 ALTER TABLE 后 version=0，第一次 expected_version=0 保存时
# 会被 UPDATE 升级到 1；全新会话则走 INSERT，也直接写 1。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_states (
    session_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
)
"""

# 无条件写入：不关心冲突时使用。
# - 新行：version=1
# - 已有行：覆盖内容，版本加一
_UPSERT = """
INSERT INTO session_states (session_id, state_json, updated_at, version)
VALUES (?, ?, ?, 1)
ON CONFLICT(session_id) DO UPDATE SET
    state_json = excluded.state_json,
    updated_at = excluded.updated_at,
    version = session_states.version + 1
"""

# expected_version=0 且数据库里没有行时使用。
_INSERT = """
INSERT INTO session_states (session_id, state_json, updated_at, version)
VALUES (?, ?, ?, 1)
"""

# expected_version=0 且数据库里可能存在旧行（version=0）时使用。
# 如果这行确实是 version=0，就把它升级到 1，而不是重新 INSERT。
_UPDATE_ZERO = """
UPDATE session_states
SET state_json = ?, updated_at = ?, version = 1
WHERE session_id = ? AND version = 0
"""

# expected_version>0 时的乐观锁更新：
# 只有数据库版本仍等于调用方读到的版本，才允许更新。
_UPDATE_VERSIONED = """
UPDATE session_states
SET state_json = ?, updated_at = ?, version = version + 1
WHERE session_id = ? AND version = ?
"""


@dataclass(frozen=True)
class StateSnapshot:
    """从存储层读出的一份状态快照。"""

    state: SessionState
    version: int


class StateStoreError(RuntimeError):
    """状态保存/读取失败，或数据库中的状态已损坏。"""


class StateConflictError(StateStoreError):
    """乐观锁版本冲突：期望版本和数据库当前版本不一致。"""


class StateStore:
    """SQLite 状态存储。

    Example:
        store = StateStore("data/state.db")
        store.initialize()

        snapshot = store.load_snapshot("session-1")
        if snapshot is None:
            snapshot = StateSnapshot(SessionState(), version=0)

        new_version = store.save_snapshot(
            "session-1",
            new_state,
            expected_version=snapshot.version,
        )
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        """创建父目录、表和 version 列；可重复调用。

        旧版本（第 14/15 课）的表没有 version 列，这里用
        ``PRAGMA table_info`` 检测并自动 ALTER，避免已有数据库无法打开。
        """
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as conn:
                conn.execute(_SCHEMA)

                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(session_states)")
                }
                if "version" not in columns:
                    conn.execute(
                        "ALTER TABLE session_states "
                        "ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
                    )
        except (OSError, sqlite3.Error) as exc:
            raise StateStoreError(f"初始化状态库失败：{exc}") from exc

    def save(
        self,
        session_id: str,
        state: SessionState,
        *,
        expected_version: int | None = None,
    ) -> int:
        """保存状态，兼容旧调用方式。

        Args:
            session_id: 会话 ID。
            state: 要保存的状态。
            expected_version: 可选的期望版本；不传则无条件写入。

        Returns:
            保存后的新版本号。
        """
        return self.save_snapshot(
            session_id,
            state,
            expected_version=expected_version,
        )

    def load(self, session_id: str) -> SessionState | None:
        """读取状态；不存在返回 ``None``。

        这是第 14/15 课保留的兼容接口，只需要状态本身时使用。
        需要版本号时用 ``load_snapshot()``。
        """
        snapshot = self.load_snapshot(session_id)
        return snapshot.state if snapshot is not None else None

    def load_snapshot(self, session_id: str) -> StateSnapshot | None:
        """读取状态和版本；不存在返回 ``None``。"""
        session_id = self._normalize_session_id(session_id)

        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT state_json, version "
                    "FROM session_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError(f"读取状态失败：{exc}") from exc

        if row is None:
            return None

        try:
            return StateSnapshot(
                state=SessionState.model_validate(json.loads(row[0])),
                version=int(row[1]),
            )
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise StateStoreError(f"状态数据损坏：{exc}") from exc

    def save_snapshot(
        self,
        session_id: str,
        state: SessionState,
        expected_version: int | None = None,
    ) -> int:
        """保存状态并返回新版本号。

        ``expected_version`` 有三种语义：

        - ``None``：无条件写入。新行版本 1，已有行版本加一；
        - ``0``：认为会话不存在，创建新状态；如果数据库中已有更高版本，
          抛 ``StateConflictError``；
        - ``N > 0``：乐观锁更新，只有数据库当前版本仍为 N 时才成功。
        """
        session_id = self._normalize_session_id(session_id)
        if not isinstance(state, SessionState):
            raise TypeError("state 必须是 SessionState")
        if expected_version is not None:
            if (
                not isinstance(expected_version, int)
                or isinstance(expected_version, bool)
                or expected_version < 0
            ):
                raise ValueError("expected_version 必须是非负整数或 None")

        try:
            payload = state.model_dump_json()
        except Exception as exc:
            raise StateStoreError(f"状态序列化失败：{exc}") from exc

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        try:
            with self._connection() as conn:
                # 路径 1：无条件写入。
                if expected_version is None:
                    conn.execute(_UPSERT, (session_id, payload, now))
                    row = conn.execute(
                        "SELECT version FROM session_states WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    return int(row[0])

                # 路径 2：创建新状态。
                # 先 UPDATE version=0，是为了兼容旧表迁移后已有的 version=0 行；
                # 如果更新不到，再 INSERT。INSERT 撞主键说明别人已经创建了
                # 更高版本，属于冲突。
                if expected_version == 0:
                    cursor = conn.execute(
                        _UPDATE_ZERO,
                        (payload, now, session_id),
                    )
                    if cursor.rowcount == 0:
                        try:
                            conn.execute(_INSERT, (session_id, payload, now))
                        except sqlite3.IntegrityError as exc:
                            raise StateConflictError(
                                f"状态版本冲突：session={session_id}, "
                                "expected=0"
                            ) from exc
                    return 1

                # 路径 3：基于已知版本更新。
                # 版本检查放在 UPDATE 的 WHERE 里，检查 + 更新是原子的；
                # rowcount=0 表示数据库版本已经不是调用方读到的版本。
                cursor = conn.execute(
                    _UPDATE_VERSIONED,
                    (payload, now, session_id, expected_version),
                )
                if cursor.rowcount == 0:
                    raise StateConflictError(
                        f"状态版本冲突：session={session_id}, "
                        f"expected={expected_version}"
                    )
                return expected_version + 1
        except sqlite3.Error as exc:
            # 注意：StateConflictError 不是 sqlite3.Error，不会被这里吞掉。
            raise StateStoreError(f"保存状态失败：{exc}") from exc

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
        """每次操作一个连接：成功提交，异常回滚，最后一定关闭。"""
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
