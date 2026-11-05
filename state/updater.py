"""回合结束后提取并应用应用状态增量，处理幂等与后台执行。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Sequence

from state.extractor import StateDelta, StateExtractionError, StateExtractor
from state.models import SessionState
from state.service import StateApplyError, StateService
from state.store import StateSnapshot, StateStoreError

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class StateUpdateResult:
    turn_id: str
    delta: StateDelta
    snapshot: StateSnapshot
    applied: bool
    duplicate: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def failed(self) -> bool:
        return self.error is not None


class TurnStateUpdater:
    """把一轮对话的 StateDelta 提取并安全地应用到状态。

    - 同一 session 的提取 + 应用串行；
    - 同一 (session_id, turn_id) 只应用一次；
    - schedule() 提供非阻塞入口，wait() 用于读己之写。
    """

    def __init__(self, extractor: StateExtractor, service: StateService):
        self.extractor = extractor
        self.service = service
        self._locks: dict[str,asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._results: dict[tuple[str,str],StateUpdateResult] = {}
        self._tasks: dict[str,set[asyncio.Task]] = {}

    async def update(
        self,
        session_id: str,
        turn_id: str,
        messages: Sequence[dict],
        *,
        expected_version: int | None = None
    ) -> StateUpdateResult:
        # 1. 规范化参数
        session_id = self._normalize_id(session_id, "session_id")
        turn_id = self._normalize_id(turn_id, "turn_id")
        messages = self._normalize_messages(messages)

        # 2. 在会话锁内完成状态加载、状态增量提取、状态应用。
        async with await self._lock_for(session_id):
            # 2.1 获取幂等键
            key = (session_id, turn_id)
            cached = self._results.get(key)
            if cached is not None:
                # 如果撞了幂等键，修改缓存的 duplicate=True 返回，告诉调用方这次应用撞幂等键
                return replace(cached, duplicate=True)

            # 2.2 读取会话状态快照
            try:
                snapshot = await self.service.get(session_id)
            except StateStoreError as exc:
                return self._failure(turn_id, None, exc)

            if expected_version is not None and snapshot.version != expected_version:
                return StateUpdateResult(
                    turn_id=turn_id,
                    delta=StateDelta(),
                    snapshot=snapshot,
                    applied=False,
                    error=(
                        f"状态版本冲突：期望 {expected_version}，"
                        f"当前 {snapshot.version}"
                    ),
                )

            # 2.3 根据状态快照和最新一轮消息提取状态增量
            try:
                delta = await self.extractor.extract(snapshot.state, messages)
            except StateExtractionError as exc:
                logger.warning("状态提取失败 turn=%s: %s", turn_id, exc)
                return self._failure(turn_id, snapshot, exc)

            # 2.4 如果为空，那么这次不应用，同时缓存结果，最后返回
            if delta.is_empty():
                result = StateUpdateResult(
                    turn_id=turn_id,
                    delta=delta,
                    snapshot=snapshot,
                    applied=False,
                )
                self._results[key] = result
                return result

            # 2.5 尝试应用增量到状态上
            try:
                updated = await self.service.apply(
                    session_id=session_id,
                    delta=delta,
                    expected_version=expected_version,
                )
            except (StateApplyError, StateStoreError) as exc:
                logger.warning("状态应用失败 turn=%s: %s", turn_id, exc)
                return self._failure(turn_id, snapshot, exc)

            # 2.6 应用成功，将结果缓存并返回
            result = StateUpdateResult(
                turn_id=turn_id,
                delta=delta,
                snapshot=updated,
                applied=True,
            )
            self._results[key] = result
            return result


    def schedule(
        self,
        session_id: str,
        turn_id: str,
        messages: Sequence[dict],
        *,
        expected_version: int | None = None,
    ) -> asyncio.Task[StateUpdateResult]:
        session_id = self._normalize_id(session_id, "session_id")
        turn_id = self._normalize_id(turn_id, "turn_id")
        messages = self._normalize_messages(messages)

        task = asyncio.create_task(
            self.update(
                session_id=session_id,
                turn_id=turn_id,
                messages=messages,
                expected_version=expected_version
            )
        )
        # 参数在进函数之前就要先求值，所以这里 set() 一定会执行，如果_tasks[session_id]已经存在直接返回，
        # 否则使用创建好的 set()，那么如果对于耗费资源的操作，比如读文件、建连接，就不要使用这种写法。
        self._tasks.setdefault(session_id,set()).add(task)

        # Task 未完成时注册：回调挂在 Task 上，Task 完成的那一刻触发。
        # Task 已完成时注册：add_done_callback 发现已完成，用 call_soon 把回调排到事件循环下一轮，尽快执行。
        task.add_done_callback(
            lambda finished: self._on_task_done(session_id, finished)
        )
        return task

    async def wait(self, session_id: str) -> None:
        """等待某个会话当前挂起的后台更新完成。"""
        session_id = self._normalize_id(session_id, "session_id")
        tasks = list(self._tasks.get(session_id,()))
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)

    async def drain(self) -> None:
        """等待所有会话的后台更新完成，用于关闭/测试。"""
        while True:
            tasks = [
                task
                for pending in self._tasks.values() # self._tasks.values() 返回所有 value 的视图，只读{Task1, Task2}, {Task3}
                for task in pending
            ]
            if not tasks:
                return
            await asyncio.gather(*tasks,return_exceptions=True)


    def _on_task_done(self, session_id: str, task: asyncio.Task) -> None:
        """执行完 Task 后的回调函数，主要处理异常和日志记录"""
        pending = self._tasks.get(session_id)
        if pending is not None:
            pending.discard(task)

        # 1. 如果是调用方自己取消，不算异常，直接返回
        if task.cancelled():
            return

        # 2. Task 内部抛出异常
        try:
            result = task.result()
        except Exception:
            logger.exception("后台状态更新任务异常 session=%s", session_id)
            return

        # 3. 业务异常，记录日志信息
        if result.failed:
            logger.warning(
                "后台状态更新失败 session=%s turn=%s: %s",
                session_id,
                result.turn_id,
                result.error,
            )

    
            
            



    async def _lock_for(self, session_id: str) -> asyncio.Lock:
        """获取一次会话的锁"""
        async with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    @staticmethod
    def _failure(
        turn_id: str,
        snapshot: StateSnapshot | None,
        exc: Exception,
    ) -> StateUpdateResult:
        return StateUpdateResult(
            turn_id=turn_id,
            delta=StateDelta(),
            snapshot=snapshot,
            applied=False,
            error=str(exc),
        )

    @staticmethod
    def _normalize_id(value: str, name: str) -> str:
        """规范化 id，进行类型校验、去掉首尾空白。"""
        if not isinstance(value, str):
            raise TypeError(f"{name} 必须是字符串")
        value=value.strip()
        if not value:
            raise ValueError(f"{name} 不能为空")
        return value

    @staticmethod
    def _normalize_messages(messages: Sequence[dict]) -> list[dict]:
        if isinstance(messages, (str, bytes)):
            raise TypeError("messages 必须是消息序列")
        try:
            return list(messages)
        except TypeError as exc:
            raise TypeError("messages 必须是消息序列") from exc