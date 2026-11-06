"""按会话持久化消息历史与滚动摘要。

状态快照由 ``StateStore`` 保存，本模块只保存消息流水账和摘要；
两者可以共用同一个 SQLite 文件，但表和维护职责分开。

``covered_through_id`` 是“摘要覆盖水位线”：
所有 id <= 它的消息都已经进入当前摘要，但不一定已经从表中删除。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from context.summarizer import RollingSummary

ALLOWED_ROLES = frozenset({"system", "user", "assistant", "tool"})

_SCHEMA_MESSAGES = """
CREATE TABLE IF NOT EXISTS session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_SCHEMA_MESSAGE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_session_messages_session
ON session_messages(session_id, id)
"""

_SCHEMA_SUMMARIES = """
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT PRIMARY KEY,
    summary_json TEXT NOT NULL,
    covered_through_id INTEGER,
    updated_at TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class TrimPlan:
    """待执行的历史消息删除计划。

    ``drop_through_id`` 是要删除的最大消息 id；``uncovered_messages``
    是其中还没有进入摘要、必须先摘要的消息。
    """

    session_id: str
    keep_last: int
    drop_through_id: int | None
    drop_count: int # 删除消息数量
    covered_count: int # 要被删除的且已经被摘要覆盖的消息数量
    uncovered_count: int # 要被删除且未被覆盖的消息数量，需要先摘要再删除
    uncovered_ids: tuple[int, ...] # 未被覆盖的消息 id，对应数据库中的 id 主键
    uncovered_messages: tuple[dict, ...] # 未被覆盖的消息
    remaining: int # 保留来的数量，一般两种取值，如果一直没有达到裁剪，那么就是total，否则是keep_last
    target_covered_through_id: int | None #目标摘要覆盖消息 id

    @property
    def is_empty(self) -> bool:
        return self.drop_count == 0


@dataclass(frozen=True)
class HistorySnapshot:
    """某个会话的历史消息和滚动摘要。"""

    messages: list[dict]
    message_ids: list[int]
    summary: RollingSummary
    covered_through_id: int | None


class HistoryStoreError(RuntimeError):
    """历史读写失败，或数据库中的历史数据损坏。"""


class HistoryTrimError(HistoryStoreError):
    """裁剪计划本身不满足提交条件。"""


class HistoryTrimConflictError(HistoryTrimError):
    """裁剪计划与数据库当前状态不一致：消息已被删除、追加，或水位线不满足。"""


class HistoryStore:
    """SQLite 历史存储：消息按会话隔离，摘要按会话覆盖。"""

    def __init__(
        self,
        db_path: str | Path,
        max_messages_per_session: int = 100,
    ):
        if (
            not isinstance(max_messages_per_session, int)
            or isinstance(max_messages_per_session, bool)
            or max_messages_per_session <= 0
        ):
            raise ValueError("max_messages_per_session 必须是正整数")

        self.db_path = Path(db_path)
        self.max_messages_per_session = max_messages_per_session

    def initialize(self) -> None:
        """创建消息表和摘要表；可重复调用。"""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as conn:
                conn.execute(_SCHEMA_MESSAGES)
                conn.execute(_SCHEMA_MESSAGE_INDEX)
                conn.execute(_SCHEMA_SUMMARIES)
        except (OSError, sqlite3.Error) as exc:
            raise HistoryStoreError(f"初始化历史库失败：{exc}") from exc

    def append(
        self,
        session_id: str,
        messages: Sequence[dict],
    ) -> None:
        """只追加消息，不裁剪、不更新摘要、不推进水位线。"""
        session_id = self._normalize_session_id(session_id)
        items = self._normalize_messages(messages)

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        try:
            with self._connection() as conn:
                for message in items:
                    conn.execute(
                        "INSERT INTO session_messages "
                        "(session_id, payload_json, created_at) "
                        "VALUES (?, ?, ?)",
                        (
                            session_id,
                            json.dumps(message, ensure_ascii=False),
                            now,
                        ),
                    )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise HistoryStoreError(f"追加历史失败：{exc}") from exc

    def load(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> HistorySnapshot:
        """读取历史和摘要；不存在的会话返回空快照。"""
        session_id = self._normalize_session_id(session_id)
        if limit is not None and (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
        ):
            raise ValueError("limit 必须是正整数或 None")

        try:
            with self._connection() as conn:
                if limit is None:
                    rows = conn.execute(
                        "SELECT id, payload_json FROM session_messages "
                        "WHERE session_id = ? ORDER BY id ASC",
                        (session_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, payload_json FROM ("
                        "  SELECT id, payload_json FROM session_messages "
                        "  WHERE session_id = ? ORDER BY id DESC LIMIT ?"
                        ") ORDER BY id ASC",
                        (session_id, limit),
                    ).fetchall()

                summary_row = conn.execute(
                    "SELECT summary_json, covered_through_id "
                    "FROM session_summaries WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"读取历史失败：{exc}") from exc

        try:
            message_ids = [int(row[0]) for row in rows]
            messages = [json.loads(row[1]) for row in rows]
            summary = (
                self._decode_summary(summary_row[0])
                if summary_row is not None
                else RollingSummary()
            )
            covered_through_id = (
                int(summary_row[1])
                if summary_row is not None and summary_row[1] is not None
                else None
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise HistoryStoreError(f"历史数据损坏：{exc}") from exc

        return HistorySnapshot(
            messages=messages,
            message_ids=message_ids,
            summary=summary,
            covered_through_id=covered_through_id,
        )

    def save_summary(
        self,
        session_id: str,
        summary: RollingSummary,
        *,
        covered_through_id: int | None = None,
    ) -> None:
        """覆盖摘要正文，可选地把覆盖水位线推进到 N。

        ``covered_through_id=None`` 表示只更新正文，不改变水位线。
        """
        session_id = self._normalize_session_id(session_id)
        if not isinstance(summary, RollingSummary):
            raise TypeError("summary 必须是 RollingSummary")

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self._connection() as conn:
                current, max_id = self._coverage_state(conn, session_id)
                if current is not None and current > 0 and not summary.text.strip():
                    raise ValueError("已有覆盖水位线时，摘要不能为空")

                if covered_through_id is not None:
                    if (
                        not isinstance(covered_through_id, int)
                        or isinstance(covered_through_id, bool)
                        or covered_through_id < 0
                    ):
                        raise ValueError("covered_through_id 必须是非负整数")
                    if current is not None and covered_through_id < current:
                        raise ValueError("covered_through_id 不能倒退")
                    max_allowed = max(max_id or 0, current or 0)
                    if covered_through_id > max_allowed:
                        raise ValueError("covered_through_id 不能超过最大消息 id")
                    if (
                        covered_through_id > (current or 0)
                        and not summary.text.strip()
                    ):
                        raise ValueError("推进覆盖水位线时必须提供非空摘要")

                self._save_summary(
                    conn,
                    session_id,
                    summary,
                    now,
                    covered_through_id=covered_through_id,
                )
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"保存摘要失败：{exc}") from exc

    def count(self, session_id: str) -> int:
        """当前会话保留的消息数量。"""
        session_id = self._normalize_session_id(session_id)
        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM session_messages WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"统计历史失败：{exc}") from exc
        return int(row[0])

    def clear(self, session_id: str) -> bool:
        """删除会话的消息和摘要；返回是否删除了任何内容。"""
        session_id = self._normalize_session_id(session_id)
        try:
            with self._connection() as conn:
                messages = conn.execute(
                    "DELETE FROM session_messages WHERE session_id = ?",
                    (session_id,),
                ).rowcount
                summary = conn.execute(
                    "DELETE FROM session_summaries WHERE session_id = ?",
                    (session_id,),
                ).rowcount
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"删除历史失败：{exc}") from exc
        return messages > 0 or summary > 0

    def plan_trim(
        self,
        session_id: str,
        *,
        keep_last: int | None = None,
    ) -> TrimPlan:
        """只读地计算最旧的、准备删除的消息。"""
        session_id = self._normalize_session_id(session_id)
        if keep_last is None:
            keep_last = self.max_messages_per_session
        if (
            not isinstance(keep_last, int)
            or isinstance(keep_last, bool)
            or keep_last <= 0
        ):
            raise ValueError("keep_last 必须是正整数")

        try:
            with self._connection() as conn:
                total = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM session_messages "
                        "WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0]
                )
                current_covered, _ = self._coverage_state(conn, session_id)

                if total <= keep_last:
                    return TrimPlan(
                        session_id=session_id,
                        keep_last=keep_last,
                        drop_through_id=None,
                        drop_count=0,
                        covered_count=0,
                        uncovered_count=0,
                        uncovered_ids=(),
                        uncovered_messages=(),
                        remaining=total,
                        target_covered_through_id=current_covered,
                    )

                rows = conn.execute(
                    "SELECT id, payload_json FROM session_messages "
                    "WHERE session_id = ? ORDER BY id ASC LIMIT ?",
                    (session_id, total - keep_last),
                ).fetchall()

                drop_ids = [int(row[0]) for row in rows]
                drop_messages = [json.loads(row[1]) for row in rows]
                covered = current_covered or 0
                uncovered_rows = [
                    (message_id, message)
                    for message_id, message in zip(drop_ids, drop_messages)
                    if message_id > covered
                ]
                uncovered_ids = tuple(item[0] for item in uncovered_rows)
                uncovered_messages = tuple(item[1] for item in uncovered_rows)
                drop_through_id = drop_ids[-1]
                target = max(covered, drop_through_id)

                return TrimPlan(
                    session_id=session_id,
                    keep_last=keep_last,
                    drop_through_id=drop_through_id,
                    drop_count=len(drop_ids),
                    covered_count=len(drop_ids) - len(uncovered_rows),
                    uncovered_count=len(uncovered_rows),
                    uncovered_ids=uncovered_ids,
                    uncovered_messages=uncovered_messages,
                    remaining=total - len(drop_ids),
                    target_covered_through_id=target,
                )
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise HistoryStoreError(f"生成 TrimPlan 失败：{exc}") from exc

    def commit_trim(
        self,
        plan: TrimPlan,
        summary: RollingSummary | None = None,
    ) -> None:
        """原子地删除 plan 中的消息，必要时保存摘要并推进水位线。"""
        if not isinstance(plan, TrimPlan):
            raise TypeError("plan 必须是 TrimPlan")
        if summary is not None and not isinstance(summary, RollingSummary):
            raise TypeError("summary 必须是 RollingSummary 或 None")

        if plan.is_empty:
            return

        session_id = self._normalize_session_id(plan.session_id)
        if plan.drop_through_id is None:
            raise HistoryTrimError("非空计划的 drop_through_id 不能为空")

        if plan.uncovered_count > 0:
            if summary is None:
                raise HistoryTrimError("有未覆盖消息时必须提供摘要")
            if not summary.text.strip():
                raise HistoryTrimError("有未覆盖消息时摘要不能为空")
            if plan.target_covered_through_id is None:
                raise HistoryTrimError("未覆盖计划缺少目标水位线")
            if plan.target_covered_through_id < plan.drop_through_id:
                raise HistoryTrimError(
                    f"目标水位线 {plan.target_covered_through_id} "
                    f"未覆盖要删除的最大 id {plan.drop_through_id}"
                )
        elif summary is not None:
            raise HistoryTrimError("全部消息已覆盖时不需要新摘要")

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        try:
            with self._connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM session_messages "
                    "WHERE session_id = ? AND id <= ?",
                    (session_id, plan.drop_through_id),
                )
                if cursor.rowcount != plan.drop_count:
                    raise HistoryTrimConflictError(
                        f"删除消息数量不匹配：计划 {plan.drop_count}，"
                        f"实际 {cursor.rowcount}"
                    )

                if plan.uncovered_count > 0:
                    self._save_summary(
                        conn,
                        session_id,
                        summary,
                        now,
                        covered_through_id=plan.target_covered_through_id,
                    )
        except HistoryTrimError:
            raise
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"提交裁剪失败：{exc}") from exc

    @staticmethod
    def _coverage_state(
        conn: sqlite3.Connection,
        session_id: str,
    ) -> tuple[int | None, int | None]:
        summary_row = conn.execute(
            "SELECT covered_through_id FROM session_summaries "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        max_row = conn.execute(
            "SELECT MAX(id) FROM session_messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        current = (
            int(summary_row[0])
            if summary_row is not None and summary_row[0] is not None
            else None
        )
        max_id = int(max_row[0]) if max_row is not None and max_row[0] is not None else None
        return current, max_id

    @staticmethod
    def _save_summary(
        conn: sqlite3.Connection,
        session_id: str,
        summary: RollingSummary,
        now: str,
        covered_through_id: int | None,
    ) -> None:
        payload = json.dumps(
            {
                "text": summary.text,
                "covered_messages": summary.covered_messages,
            },
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT INTO session_summaries "
            "(session_id, summary_json, covered_through_id, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "summary_json = excluded.summary_json, "
            "covered_through_id = COALESCE("
            "excluded.covered_through_id, session_summaries.covered_through_id"
            "), "
            "updated_at = excluded.updated_at",
            (session_id, payload, covered_through_id, now),
        )

    @staticmethod
    def _decode_summary(payload: str) -> RollingSummary:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("summary 必须是 JSON object")
        return RollingSummary(
            text=str(data.get("text", "")),
            covered_messages=int(data.get("covered_messages", 0)),
        )

    @staticmethod
    def _normalize_messages(messages: Sequence[dict]) -> list[dict]:
        if isinstance(messages, (str, bytes)):
            raise TypeError("messages 必须是消息序列")
        try:
            items = list(messages)
        except TypeError as exc:
            raise TypeError("messages 必须是消息序列") from exc

        for index, message in enumerate(items):
            if not isinstance(message, dict):
                raise TypeError(f"messages[{index}] 必须是 dict")
            role = message.get("role")
            if role not in ALLOWED_ROLES:
                raise ValueError(f"messages[{index}] 的 role 不合法: {role!r}")
        return items

    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        if not isinstance(session_id, str):
            raise TypeError("session_id 必须是字符串")
        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id 不能为空")
        return session_id

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()
