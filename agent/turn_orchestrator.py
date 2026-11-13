"""一轮对话的编排层：加载快照、组装上下文、生成回答、写回状态与历史。

本模块只负责“编排”，不实现 token 计算、消息渲染或状态 delta 提取；
这些分别由 ``ContextPlanner`` / ``ContextBuilder`` / ``StateExtractor`` 完成。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from agent.chat_responder import ChatResponder
from context.budget import BudgetExceededError
from context.builder import BuiltContext, ContextBuilder
from history.store import (
    HistorySnapshot,
    HistoryStore,
    HistoryStoreError,
    TrimPlan,
)
from context.planner import ContextPlan, ContextPlanner
from history.summarizer import HistorySummarizer, SummaryError
from events.models import EventType, RuntimeEvent
from state.service import StateService
from state.store import StateSnapshot
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
    trimmed: bool
    warnings: tuple[str, ...]

    @property
    def state_failed(self) -> bool:
        return self.state_error is not None


class TurnOrchestrator:
    """把聊天路径的“读上下文 → 生成 → 写回”串起来。"""

    def __init__(
        self,
        *,
        state_service: StateService,
        history_store: HistoryStore,
        planner: ContextPlanner,
        builder: ContextBuilder,
        summarizer: HistorySummarizer,
        updater: TurnStateUpdater,
        chat_responder: ChatResponder,
        keep_last: int | None = None,
    ):
        self.state_service = state_service
        self.history_store = history_store
        self.planner = planner
        self.builder = builder
        self.summarizer = summarizer
        self.updater = updater
        self.chat_responder = chat_responder
        self.keep_last = keep_last

    async def begin_turn(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
    ) -> TurnContext:
        """等待上一轮更新，然后一次性加载状态和历史。"""
        session_id = self._normalize_id(session_id, "session_id")
        turn_id = (
            self._normalize_id(turn_id, "turn_id")
            if turn_id is not None
            else str(uuid.uuid4())
        )

        await self.updater.wait(session_id)
        state = await self.state_service.get(session_id)
        history = await asyncio.to_thread(
            self.history_store.load,
            session_id,
        )
        return TurnContext(
            session_id=session_id,
            turn_id=turn_id,
            state=state,
            history=history,
        )

    async def run_chat(
        self,
        context: TurnContext,
        user_input: str,
        *,
        run_id: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """执行聊天回合，并在回答后完成状态/历史收尾。"""
        if not isinstance(user_input, str):
            raise TypeError("user_input 必须是 str")

        run_id = run_id or context.turn_id
        yield RuntimeEvent(
            type=EventType.RUN_STARTED,
            run_id=run_id,
            session_id=context.session_id,
        )

        try:
            built, prepare_warnings = await self._prepare_context(
                context,
                user_input,
            )
        except (BudgetExceededError, SummaryError, HistoryStoreError) as exc:
            logger.warning("上下文准备失败 turn=%s: %s", context.turn_id, exc)
            yield RuntimeEvent(
                type=EventType.RUN_FINISHED,
                run_id=run_id,
                session_id=context.session_id,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        try:
            answer = await self.chat_responder.answer(built.messages)
        except Exception as exc:
            logger.exception("聊天回答失败 turn=%s", context.turn_id)
            yield RuntimeEvent(
                type=EventType.RUN_FINISHED,
                run_id=run_id,
                session_id=context.session_id,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        yield RuntimeEvent(
            type=EventType.FINAL_ANSWER,
            run_id=run_id,
            session_id=context.session_id,
            data={"content": answer},
        )

        try:
            finish = await self.finish_turn(context, user_input, answer)
        except Exception as exc:
            # 回答已经发出，收尾异常只记录，不把这一轮判成失败。
            logger.exception("回合收尾异常 turn=%s", context.turn_id)
            finish = TurnFinishResult(
                state_update=None,
                state_error=None,
                appended=False,
                trimmed=False,
                warnings=(f"回合收尾异常：{exc}",),
            )

        warnings = list(prepare_warnings)
        warnings.extend(finish.warnings)
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

        data: dict = {}
        if warnings:
            data["warnings"] = warnings
        yield RuntimeEvent(
            type=EventType.RUN_FINISHED,
            run_id=run_id,
            session_id=context.session_id,
            data=data,
        )

    async def finish_turn(
        self,
        context: TurnContext,
        user_input: str,
        answer: str,
    ) -> TurnFinishResult:
        """更新状态、幂等追加历史，并在超过保留量时裁剪。"""
        if not isinstance(user_input, str):
            raise TypeError("user_input 必须是 str")
        if not isinstance(answer, str):
            raise TypeError("answer 必须是 str")

        turn_messages = [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": answer},
        ]
        warnings: list[str] = []

        state_update: StateUpdateResult | None = None
        state_error: str | None = None
        try:
            state_update = await self.updater.update(
                context.session_id,
                context.turn_id,
                turn_messages,
            )
            if state_update.failed:
                state_error = state_update.error
                warnings.append(f"状态更新失败：{state_update.error}")
        except Exception as exc:
            logger.exception("状态更新异常 turn=%s", context.turn_id)
            state_error = str(exc)
            warnings.append(f"状态更新异常：{exc}")

        try:
            appended = await asyncio.to_thread(
                self.history_store.append_once,
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
                trimmed=False,
                warnings=tuple(warnings),
            )

        trimmed = False
        try:
            plan = await asyncio.to_thread(
                self.history_store.plan_trim,
                context.session_id,
                keep_last=self.keep_last,
            )
            trimmed = await self._commit_trim(context, plan, warnings)
        except HistoryStoreError as exc:
            logger.warning("历史裁剪失败 turn=%s: %s", context.turn_id, exc)
            warnings.append(f"历史裁剪失败：{exc}")

        return TurnFinishResult(
            state_update=state_update,
            state_error=state_error,
            appended=appended,
            trimmed=trimmed,
            warnings=tuple(warnings),
        )

    async def _prepare_context(
        self,
        context: TurnContext,
        user_input: str,
    ) -> tuple[BuiltContext, tuple[str, ...]]:
        """规划预算、处理请求级裁剪，然后组装一次 messages。"""
        warnings: list[str] = []
        history = context.history

        plan = self.planner.plan(
            user_input=user_input,
            system_prompt=self.builder.system_prompt,
            summary=history.summary,
            history=history.messages,
            message_ids=history.message_ids,
            budget=self.builder.budget,
        )

        summary = history.summary
        uncovered_pairs = self._uncovered_dropped(context, plan)
        if uncovered_pairs:
            uncovered_ids = tuple(item[0] for item in uncovered_pairs)
            uncovered_messages = [item[1] for item in uncovered_pairs]
            try:
                summary = await self.summarizer.update(
                    history.summary,
                    uncovered_messages,
                )
            except SummaryError as exc:
                logger.warning(
                    "请求级摘要失败 turn=%s: %s",
                    context.turn_id,
                    exc,
                )
                warnings.append(f"请求级摘要失败：{exc}")
            else:
                try:
                    await asyncio.to_thread(
                        self.history_store.save_summary,
                        context.session_id,
                        summary,
                        covered_through_id=max(uncovered_ids),
                    )
                except HistoryStoreError as exc:
                    logger.warning(
                        "摘要落库失败 turn=%s: %s",
                        context.turn_id,
                        exc,
                    )
                    warnings.append(f"摘要落库失败：{exc}")

        built = self.builder.build(
            user_input,
            state=context.state.state,
            summary=summary,
            history=plan.kept_history,
            state_max_tokens=plan.state_max_tokens,
        )
        if built.dropped_messages:
            logger.warning(
                "ContextBuilder 兜底丢弃 %d 条历史 turn=%s",
                len(built.dropped_messages),
                context.turn_id,
            )
            warnings.append(
                f"ContextBuilder 兜底丢弃 {len(built.dropped_messages)} 条历史"
            )
        return built, tuple(warnings)

    async def _commit_trim(
        self,
        context: TurnContext,
        plan: TrimPlan,
        warnings: list[str],
    ) -> bool:
        if plan.is_empty:
            return False

        if plan.uncovered_count == 0:
            await asyncio.to_thread(
                self.history_store.commit_trim,
                plan,
                None,
            )
            return True

        current = await asyncio.to_thread(
            self.history_store.load,
            context.session_id,
        )
        try:
            summary = await self.summarizer.update(
                current.summary,
                list(plan.uncovered_messages),
            )
        except SummaryError as exc:
            logger.warning(
                "存储级摘要失败 turn=%s: %s",
                context.turn_id,
                exc,
            )
            warnings.append(f"存储级摘要失败：{exc}")
            return False

        await asyncio.to_thread(
            self.history_store.commit_trim,
            plan,
            summary,
        )
        return True

    @staticmethod
    def _uncovered_dropped(
        context: TurnContext,
        plan: ContextPlan,
    ) -> list[tuple[int, dict]]:
        covered = context.history.covered_through_id or 0
        return [
            (message_id, message)
            for message_id, message in zip(
                plan.dropped_message_ids,
                plan.dropped_history,
            )
            if message_id > covered
        ]

    @staticmethod
    def _normalize_id(value: str, name: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{name} 必须是字符串")
        value = value.strip()
        if not value:
            raise ValueError(f"{name} 不能为空")
        return value
