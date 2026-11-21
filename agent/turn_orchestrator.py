"""一轮对话的编排层：读快照、装配上下文、生成回答、写回状态与历史。

职责边界（只做顺序、失败策略和事件）：

- 预算、消息布局、历史渲染由 ``ContextEngine`` 负责；
- 摘要、CAS、归档由 ``HistoryCompactor`` 负责；
- 状态增量由 ``TurnStateUpdater`` / ``StateService`` 负责。

本模块不做 token 计算、不渲染消息、不生成摘要。执行模式（TOOL / PLAN）
属于后续阶段，本期聊天路径只有一次模型调用、不带工具。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from agent.llm import LLM
from context.engine import ContextEngine
from context.errors import ContextError
from context.models import ContextRequest, ContextResult
from core.issues import ContextIssueCode
from events.models import EventType, RuntimeEvent
from history.compactor import CompactionResult, HistoryCompactor
from history.errors import (
    HistoryFloorUnmet,
    HistoryHardCapExceeded,
    HistoryStoreError,
)
from history.store import HARD_ARCHIVE_LIMIT, HistoryStore
from history.models import HistorySnapshot
from state.models import StateSnapshot
from state.service import StateService
from state.updater import StateUpdateResult, TurnStateUpdater

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnContext:
    """一轮开始时读取到的状态与历史快照。"""

    session_id: str
    turn_id: str
    state: StateSnapshot
    history: HistorySnapshot


@dataclass(frozen=True)
class TurnFinishResult:
    """一轮收尾的持久化结果；错误以 warning 形式返回，不撤回回答。"""

    state_update: StateUpdateResult | None
    state_error: str | None
    appended: bool
    compaction: CompactionResult | None
    warnings: tuple[str, ...]

    @property
    def state_failed(self) -> bool:
        return self.state_error is not None


class TurnOrchestrator:
    """把聊天路径的“读上下文 → 装配 → 生成 → 写回”串起来。"""

    def __init__(
        self,
        *,
        state_service: StateService,
        store: HistoryStore,
        engine: ContextEngine,
        compactor: HistoryCompactor,
        updater: TurnStateUpdater,
        llm: LLM,
    ):
        self.state_service = state_service
        self.store = store
        self.engine = engine
        self.compactor = compactor
        self.updater = updater
        self.llm = llm

    # ------------------------------------------------------------------ 开始

    async def begin_turn(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
    ) -> TurnContext:
        """硬上限闸门 → 等待上一轮更新 → 一次性加载状态和历史。"""
        session_id = self._normalize_id(session_id, "session_id")
        turn_id = (
            self._normalize_id(turn_id, "turn_id")
            if turn_id is not None
            else str(uuid.uuid4())
        )

        count = await asyncio.to_thread(self.store.count_turns, session_id)
        if count >= HARD_ARCHIVE_LIMIT:
            await self._ensure_hard_cap(session_id)

        await self.updater.wait(session_id)
        state = await self.state_service.get(session_id)
        history = await asyncio.to_thread(self.store.load, session_id)
        return TurnContext(
            session_id=session_id,
            turn_id=turn_id,
            state=state,
            history=history,
        )

    async def _ensure_hard_cap(self, session_id: str) -> None:
        """硬上限维护失败时映射成 ``ContextError``，阻塞本轮。"""
        try:
            await self.compactor.ensure_hard_cap(session_id)
        except HistoryFloorUnmet as exc:
            raise ContextError.from_code(
                ContextIssueCode.HISTORY_FLOOR_UNMET,
                f"最近 6 轮已超过 history_ceiling，压缩无法释放空间：{exc}",
                component="history",
                details={"reason": str(exc)},
            ) from exc
        except HistoryHardCapExceeded as exc:
            raise ContextError.from_code(
                ContextIssueCode.HISTORY_HARD_CAP_EXCEEDED,
                f"历史硬上限维护失败：{exc}",
                component="history",
                details={"reason": str(exc)},
            ) from exc

    # ------------------------------------------------------------------ 执行

    async def run_chat(
        self,
        context: TurnContext,
        user_input: str,
        *,
        run_id: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """装配一次上下文、调用一次模型，然后收尾。"""
        if not isinstance(user_input, str):
            raise TypeError("user_input 必须是 str")

        run_id = run_id or context.turn_id
        yield RuntimeEvent(
            type=EventType.RUN_STARTED,
            run_id=run_id,
            session_id=context.session_id,
        )

        try:
            result = self._assemble(context, user_input)
        except ContextError as exc:
            logger.warning(
                "上下文装配失败 turn=%s: %s", context.turn_id, exc
            )
            yield self._error_event(run_id, context, exc)
            return
        except Exception as exc:  # noqa: BLE001 - 意外错误也要结束本轮
            logger.exception("上下文装配异常 turn=%s", context.turn_id)
            yield self._error_event(run_id, context, exc)
            return

        logger.info(
            "上下文装配完成 turn=%s: %s",
            context.turn_id,
            result.report.to_log(),
        )

        try:
            answer = await self._answer(result.messages)
        except Exception as exc:  # noqa: BLE001 - 模型失败结束本轮
            logger.exception("聊天回答失败 turn=%s", context.turn_id)
            yield self._error_event(run_id, context, exc)
            return

        yield RuntimeEvent(
            type=EventType.FINAL_ANSWER,
            run_id=run_id,
            session_id=context.session_id,
            data={"content": answer},
        )

        finish = await self._safe_finish(context, user_input, answer)
        if finish.state_failed:
            yield RuntimeEvent(
                type=EventType.STATE_UPDATE_FAILED,
                run_id=run_id,
                session_id=context.session_id,
                data={
                    "turn_id": context.turn_id,
                    "error": finish.state_error,
                },
            )

        data: dict = {"context": result.report.to_log()}
        if finish.compaction is not None:
            data["compaction"] = _compaction_data(finish.compaction)
        if finish.warnings:
            data["warnings"] = list(finish.warnings)

        yield RuntimeEvent(
            type=EventType.RUN_FINISHED,
            run_id=run_id,
            session_id=context.session_id,
            data=data,
        )

    def _assemble(
        self,
        context: TurnContext,
        user_input: str,
    ) -> ContextResult:
        request = ContextRequest(
            session_id=context.session_id,
            turn_id=context.turn_id,
            user_input=user_input,
            state=context.state,
            history=context.history,
            tools=(),
            required_tools=frozenset(),
        )
        return self.engine.assemble(request)

    async def _answer(self, messages: list[dict]) -> str:
        """单次模型调用；工具循环属于后续执行模式阶段。"""
        response = await self.llm.chat(messages=list(messages))
        content = getattr(response, "content", None)
        return content or ""

    @staticmethod
    def _error_event(
        run_id: str,
        context: TurnContext,
        exc: Exception,
    ) -> RuntimeEvent:
        data: dict = {
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
        if isinstance(exc, ContextError):
            data["error_code"] = exc.code.value
            data["level"] = exc.issue.level.value

        return RuntimeEvent(
            type=EventType.RUN_FINISHED,
            run_id=run_id,
            session_id=context.session_id,
            data=data,
        )

    # ------------------------------------------------------------------ 收尾

    async def _safe_finish(
        self,
        context: TurnContext,
        user_input: str,
        answer: str,
    ) -> TurnFinishResult:
        try:
            return await self.finish_turn(context, user_input, answer)
        except Exception as exc:  # noqa: BLE001 - 回答已发出，收尾不撤回
            logger.exception("回合收尾异常 turn=%s", context.turn_id)
            return TurnFinishResult(
                state_update=None,
                state_error=None,
                appended=False,
                compaction=None,
                warnings=(f"回合收尾异常：{exc}",),
            )

    async def finish_turn(
        self,
        context: TurnContext,
        user_input: str,
        answer: str,
    ) -> TurnFinishResult:
        """更新状态、幂等追加本轮 turn，然后交给 compactor 维护历史。"""
        if not isinstance(user_input, str):
            raise TypeError("user_input 必须是 str")
        if not isinstance(answer, str):
            raise TypeError("answer 必须是 str")

        turn_messages = [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": answer},
        ]
        warnings: list[str] = []

        state_update, state_error = await self._update_state(
            context, turn_messages, warnings
        )

        try:
            appended = await asyncio.to_thread(
                self.store.append_turn_once,
                context.session_id,
                context.turn_id,
                turn_messages,
            )
        except HistoryStoreError as exc:
            logger.warning("历史追加失败 turn=%s: %s", context.turn_id, exc)
            warnings.append(f"历史追加失败：{exc}")
            return TurnFinishResult(
                state_update=state_update,
                state_error=state_error,
                appended=False,
                compaction=None,
                warnings=tuple(warnings),
            )

        compaction = await self._compact(context.session_id, warnings)

        return TurnFinishResult(
            state_update=state_update,
            state_error=state_error,
            appended=appended,
            compaction=compaction,
            warnings=tuple(warnings),
        )

    async def _update_state(
        self,
        context: TurnContext,
        turn_messages: list[dict],
        warnings: list[str],
    ) -> tuple[StateUpdateResult | None, str | None]:
        try:
            result = await self.updater.update(
                context.session_id,
                context.turn_id,
                turn_messages,
            )
        except Exception as exc:  # noqa: BLE001 - 状态失败不影响回答
            logger.exception("状态更新异常 turn=%s", context.turn_id)
            warnings.append(f"状态更新异常：{exc}")
            return None, str(exc)

        if result.failed:
            warnings.append(f"状态更新失败：{result.error}")
            return result, result.error

        return result, None

    async def _compact(
        self,
        session_id: str,
        warnings: list[str],
    ) -> CompactionResult | None:
        """压缩失败不阻塞本轮；硬上限错误等下一轮 begin_turn 再试。"""
        try:
            return await self.compactor.after_turn(session_id)
        except (HistoryFloorUnmet, HistoryHardCapExceeded) as exc:
            logger.warning("历史硬上限 turn: %s", exc)
            warnings.append(f"历史硬上限：{exc}")
        except HistoryStoreError as exc:
            logger.warning("历史压缩失败: %s", exc)
            warnings.append(f"历史压缩失败：{exc}")
        return None

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _normalize_id(value: str, name: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{name} 必须是字符串")
        value = value.strip()
        if not value:
            raise ValueError(f"{name} 不能为空")
        return value


def _compaction_data(result: CompactionResult) -> dict:
    """压缩结果进事件时只放稳定字段，不放内部对象。"""
    return {
        "decision": result.decision.value,
        "compacted": result.compacted,
        "covered_through_seq": result.covered_through_seq,
        "active_turns": result.metrics.active_turns,
        "issues": [
            {
                "code": issue.code.value,
                "level": issue.level.value,
                "message": issue.message,
            }
            for issue in result.issues
        ],
    }
