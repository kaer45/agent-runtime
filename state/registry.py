"""按 session_id 管理会话状态读写和并发锁。

本模块只解决单进程内的会话隔离与同会话串行化；
版本号、冲突合并和 delta 应用留给第 16 课的 ``StateService``。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from state.models import SessionState
from state.store import StateSnapshot, StateStore


class SessionStateRegistry:
    """多会话状态入口：隔离、按会话加锁、快照读写。

    Example:
        registry = SessionStateRegistry(store)

        async with registry.acquire("session-1"):
            snapshot = registry.load_snapshot("session-1")
            current = snapshot or StateSnapshot(SessionState(), version=0)
            ...
            registry.save_snapshot(
                "session-1",
                new_state,
                expected_version=current.version,
            )
    """

    def __init__(self, store: StateStore):
        self.store = store
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def exists(self, session_id: str) -> bool:
        """会话状态是否存在。"""
        return self.store.exists(session_id)

    def delete(self, session_id: str) -> bool:
        """删除会话状态。"""
        return self.store.delete(session_id)

    def load_snapshot(self, session_id: str) -> StateSnapshot | None:
        """读取状态和版本；不存在返回 None。"""
        return self.store.load_snapshot(session_id)

    def save_snapshot(
        self,
        session_id: str,
        state: SessionState,
        expected_version: int | None,
    ) -> int:
        """按期望版本保存状态，返回新版本号。"""
        return self.store.save_snapshot(
            session_id,
            state,
            expected_version=expected_version,
        )

    @property
    def lock_count(self) -> int:
        return len(self._locks)

    async def lock(self, session_id: str) -> asyncio.Lock:
        """获取某个会话的锁；同一 session 永远返回同一个对象。"""
        session_id = self._normalize_session_id(session_id)

        async with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    @asynccontextmanager
    async def acquire(self, session_id: str) -> AsyncIterator[None]:
        """在会话锁内执行一段临界区。"""
        lock = await self.lock(session_id)
        async with lock:
            yield

    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        if not isinstance(session_id, str):
            raise TypeError("session_id 必须是字符串")

        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id 不能为空")
        return session_id