"""按 session_id 管理状态读写和并发锁。

本模块只解决单进程内的会话隔离与同会话串行化；
版本号、冲突合并和 delta 应用留给第 16 课的 ``StateService``。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from state.models import SessionState
from state.store import StateStore

StateMutator = Callable[[SessionState], SessionState]


class SessionRegistry:
    """多会话状态入口：隔离、按会话加锁、原子更新。

    Example:
        registry = SessionRegistry(store)
        state = registry.load("session-1")

        async with registry.acquire("session-1"):
            state = registry.load("session-1")
            ...
            registry.save("session-1", state)
    """

    def __init__(self, store: StateStore):
        self.store = store
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def load(self, session_id: str) -> SessionState:
        """读取会话状态；不存在时返回空状态。"""
        state = self.store.load(session_id)
        return state if state is not None else SessionState()

    def save(self, session_id: str, state: SessionState) -> None:
        """保存会话状态快照。"""
        self.store.save(session_id, state)

    def exists(self, session_id: str) -> bool:
        """会话状态是否存在。"""
        return self.store.exists(session_id)

    def delete(self, session_id: str) -> bool:
        """删除会话状态。"""
        return self.store.delete(session_id)

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

    async def update(
        self,
        session_id: str,
        mutate: StateMutator,
    ) -> SessionState:
        """在会话锁内完成一次原子读-改-写。"""
        session_id = self._normalize_session_id(session_id)

        async with self.acquire(session_id):
            state = self.load(session_id)
            updated = mutate(state)

            if not isinstance(updated, SessionState):
                raise TypeError("mutate 必须返回 SessionState")

            self.save(session_id, updated)
            return updated

    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        if not isinstance(session_id, str):
            raise TypeError("session_id 必须是字符串")

        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id 不能为空")
        return session_id