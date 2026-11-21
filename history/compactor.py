"""历史压缩（v2）：触发、摘要、CAS、归档与硬上限维护。

职责边界：

- ``analyze`` / ``decide`` 只决定“该不该压”，放在 ``history/metrics.py``；
- ``HistorySummarizer`` 只生成摘要，不读写数据库；
- 本模块把两者串成一次事务：生成摘要 → CAS 推进水位 → 按阈值归档。

失败策略（protocol §7.3、§9.3）：

- 摘要失败、CAS 冲突、软归档失败只追加 issue，不阻塞本轮；
- 只有硬上限维护失败抛 ``HistoryFloorUnmet`` / ``HistoryHardCapExceeded``，
  由 ``TurnOrchestrator`` 映射成 ``ContextError``。

锁与并发：同一 session 用 ``asyncio.Lock`` 串行，跨进程冲突由 CAS 兜底；
一个 compactor 实例只在一个事件循环里使用。

所有压缩判断必须用**全量** active turns，不得传 ``limit_turns``，否则
``active_turns`` / ``history_tokens`` / ``last6_tokens`` 全部失真。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from context.models import ContextLimits
from core.issues import ContextIssue, ContextIssueCode
from core.tokens import TokenCounter, count_text, estimate_tokens
from history.errors import (
    HistoryCoverageConflict,
    HistoryFloorUnmet,
    HistoryHardCapExceeded,
    HistoryStoreError,
    SummaryError,
)
from history.metrics import (
    COMPACTION_FLOOR_TURNS,
    CompactionDecision,
    HistoryMetrics,
    analyze,
    decide,
)
from history.store import (
    HARD_ARCHIVE_LIMIT,
    SOFT_ARCHIVE_LIMIT,
    HistoryStore,
)
from history.summarizer import HistorySummarizer
from history.models import HistorySnapshot, TurnRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompactionResult:
    """一次 ``after_turn`` 的结果。

    ``metrics`` 是压缩前的分析结果；``covered_through_seq`` 是执行后的
    实际水位（没推进时就是原水位）。
    """

    decision: CompactionDecision
    compacted: bool
    covered_through_seq: int
    metrics: HistoryMetrics
    issues: tuple[ContextIssue, ...]


class HistoryCompactor:
    """按固定触发顺序压缩历史，并维护软 / 硬归档线。"""

    def __init__(
        self,
        *,
        store: HistoryStore,
        summarizer: HistorySummarizer,
        limits: ContextLimits,
        counter: TokenCounter = estimate_tokens,
    ):
        if not isinstance(store, HistoryStore):
            raise TypeError("store 必须是 HistoryStore")
        if not isinstance(summarizer, HistorySummarizer):
            raise TypeError("summarizer 必须是 HistorySummarizer")
        if not isinstance(limits, ContextLimits):
            raise TypeError("limits 必须是 ContextLimits")
        if limits.history_ceiling <= 0:
            raise ValueError("limits.history_ceiling 必须大于 0")
        if not callable(counter):
            raise TypeError("counter 必须是可调用对象")

        self.store = store
        self.summarizer = summarizer
        self.limits = limits
        self.counter = counter

        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ 入口

    async def after_turn(self, session_id: str) -> CompactionResult:
        """本轮回答写库后执行一次判断、压缩和归档检查。"""
        key = _session_key(session_id)

        async with self._lock_for(key):
            return await self._after_turn_locked(key)

    async def ensure_hard_cap(self, session_id: str) -> None:
        """硬上限维护；不满足条件时立即返回。

        只负责拿锁，维护流程在 ``_ensure_hard_cap_locked``。持锁路径里
        不要再调这个方法，否则同一把锁重入会死锁。
        """
        key = _session_key(session_id)

        async with self._lock_for(key):
            await self._ensure_hard_cap_locked(key)

    # ------------------------------------------------------------------ 判断

    async def _after_turn_locked(self, session_id: str) -> CompactionResult:
        snapshot = self.store.load(session_id)
        ceiling = self.limits.history_ceiling
        metrics = analyze(snapshot.turns, ceiling)
        decision = decide(metrics, ceiling)

        issues: list[ContextIssue] = []
        coverage = snapshot.covered_through_seq
        compacted = False

        if decision is CompactionDecision.FLOOR_UNMET:
            logger.warning(
                "历史压缩跳过：最近 %d 轮 %d tokens 超过 history_ceiling=%d"
                "（session=%s）",
                min(metrics.active_turns, COMPACTION_FLOOR_TURNS),
                metrics.last6_tokens,
                ceiling,
                session_id,
            )
        elif decision is CompactionDecision.COMPACT:
            coverage, compacted = await self._compact_locked(
                session_id, snapshot, issues
            )
        elif decision is CompactionDecision.OVER_TARGET:
            issues.append(
                ContextIssue.from_code(
                    ContextIssueCode.HISTORY_OVER_TARGET,
                    "未覆盖历史 "
                    f"{metrics.history_tokens:,} / {ceiling:,}"
                    f"（{metrics.history_utilization:.0%}），"
                    f"当前 {metrics.active_turns} 轮不能压缩",
                    component="history",
                    details={
                        "history_tokens": metrics.history_tokens,
                        "history_utilization": metrics.history_utilization,
                        "active_turns": metrics.active_turns,
                    },
                )
            )

        await self._archive_check_locked(session_id, issues)

        result = CompactionResult(
            decision=decision,
            compacted=compacted,
            covered_through_seq=coverage,
            metrics=metrics,
            issues=tuple(issues),
        )

        logger.info(
            "历史压缩完成 session=%s decision=%s compacted=%s "
            "coverage=%d active_turns=%d issues=%d",
            session_id,
            decision.value,
            compacted,
            coverage,
            metrics.active_turns,
            len(issues),
        )
        return result

    async def _compact_locked(
        self,
        session_id: str,
        snapshot: HistorySnapshot,
        issues: list[ContextIssue],
    ) -> tuple[int, bool]:
        """生成摘要并推进水位；返回 ``(coverage, compacted)``。"""
        target_turns = self._fit_target_turns(snapshot)

        if not target_turns:
            summary_tokens = count_text(self.counter, snapshot.summary.text)
            input_ceiling = self._input_ceiling()
            issues.append(
                ContextIssue.from_code(
                    ContextIssueCode.SUMMARY_INPUT_TOO_LONG,
                    "摘要输入放不下任何 turn："
                    f"旧摘要 {summary_tokens} tokens，输入上限 {input_ceiling}",
                    component="summary",
                    details={
                        "summary_tokens": summary_tokens,
                        "input_ceiling": input_ceiling,
                        "pending_turns": len(
                            snapshot.turns[:-COMPACTION_FLOOR_TURNS]
                        ),
                    },
                )
            )
            return snapshot.covered_through_seq, False

        target_seq = target_turns[-1].seq

        try:
            summary = await self.summarizer.update(snapshot.summary, target_turns)
            self.store.save_summary_cas(
                session_id,
                summary,
                covered_through_seq=target_seq,
                expected_covered_through_seq=snapshot.covered_through_seq,
            )
        except HistoryCoverageConflict as exc:
            issues.append(
                ContextIssue.from_code(
                    ContextIssueCode.SUMMARY_CAS_CONFLICT,
                    "摘要覆盖水位已被其他进程推进，本次摘要丢弃，不重试："
                    f"expected={snapshot.covered_through_seq}, "
                    f"target={target_seq}",
                    component="summary",
                    details={
                        "expected_covered_through_seq": (
                            snapshot.covered_through_seq
                        ),
                        "target_covered_through_seq": target_seq,
                        "reason": str(exc),
                    },
                )
            )
            return snapshot.covered_through_seq, False
        except SummaryError as exc:
            issues.append(
                ContextIssue.from_code(
                    ContextIssueCode.SUMMARY_GENERATION_FAILED,
                    f"摘要生成失败，保留旧摘要与旧水位：{exc}",
                    component="summary",
                    details={"stage": "generate", "reason": str(exc)},
                )
            )
            return snapshot.covered_through_seq, False
        except HistoryStoreError as exc:
            issues.append(
                ContextIssue.from_code(
                    ContextIssueCode.SUMMARY_GENERATION_FAILED,
                    f"摘要保存失败，保留旧摘要与旧水位：{exc}",
                    component="summary",
                    details={"stage": "save", "reason": str(exc)},
                )
            )
            return snapshot.covered_through_seq, False

        return target_seq, True

    # ------------------------------------------------------------------ 归档

    async def _archive_check_locked(
        self,
        session_id: str,
        issues: list[ContextIssue],
    ) -> None:
        count = self.store.count_turns(session_id)

        if count >= SOFT_ARCHIVE_LIMIT:
            try:
                self.store.archive_covered(session_id)
            except HistoryStoreError as exc:
                issues.append(
                    ContextIssue.from_code(
                        ContextIssueCode.HISTORY_ARCHIVE_FAILED,
                        f"软归档失败，本轮回答继续，下一轮重试：{exc}",
                        component="history",
                        details={
                            "stage": "soft_archive",
                            "turn_count": count,
                            "reason": str(exc),
                        },
                    )
                )

        if count >= HARD_ARCHIVE_LIMIT:
            await self._ensure_hard_cap_locked(session_id)

    async def _ensure_hard_cap_locked(self, session_id: str) -> None:
        """硬上限维护：归档 → 强制压缩 → 再归档 → 仍超限则失败。"""
        count = self.store.count_turns(session_id)
        if count < HARD_ARCHIVE_LIMIT:
            return

        self._archive_quietly(session_id)
        count = self.store.count_turns(session_id)
        if count < HARD_ARCHIVE_LIMIT:
            return

        await self._force_compact_locked(session_id)

        self._archive_quietly(session_id)
        count = self.store.count_turns(session_id)
        if count >= HARD_ARCHIVE_LIMIT:
            raise HistoryHardCapExceeded(
                "硬上限维护后主表仍有 "
                f"{count} 轮（hard_limit={HARD_ARCHIVE_LIMIT}）："
                f"session={session_id}"
            )

    async def _force_compact_locked(self, session_id: str) -> None:
        """忽略 60% 水位，强制压缩最后 6 轮之前的内容。"""
        snapshot = self.store.load(session_id)
        ceiling = self.limits.history_ceiling
        metrics = analyze(snapshot.turns, ceiling)

        if metrics.last6_tokens > ceiling:
            raise HistoryFloorUnmet(
                "最近 "
                f"{min(metrics.active_turns, COMPACTION_FLOOR_TURNS)} 轮 "
                f"{metrics.last6_tokens} tokens 超过 history_ceiling="
                f"{ceiling}，压缩无法释放空间：session={session_id}"
            )

        target_turns = self._fit_target_turns(snapshot)
        if not target_turns:
            raise HistoryHardCapExceeded(
                "硬上限强制压缩取不到可压缩 turn："
                f"session={session_id}, 主表 {metrics.active_turns} 轮"
            )

        target_seq = target_turns[-1].seq

        try:
            summary = await self.summarizer.update(snapshot.summary, target_turns)
        except SummaryError as exc:
            raise HistoryHardCapExceeded(
                f"硬上限强制压缩失败：摘要生成失败：{exc}；"
                f"session={session_id}"
            ) from exc

        try:
            self.store.save_summary_cas(
                session_id,
                summary,
                covered_through_seq=target_seq,
                expected_covered_through_seq=snapshot.covered_through_seq,
            )
        except HistoryCoverageConflict:
            # 其他进程已推进水位，继续归档即可，不重试摘要。
            logger.info(
                "硬上限强制压缩 CAS 冲突，继续归档：session=%s expected=%d",
                session_id,
                snapshot.covered_through_seq,
            )
        except HistoryStoreError as exc:
            raise HistoryHardCapExceeded(
                f"硬上限强制压缩失败：摘要保存失败：{exc}；"
                f"session={session_id}"
            ) from exc

    def _archive_quietly(self, session_id: str) -> None:
        try:
            self.store.archive_covered(session_id)
        except HistoryStoreError as exc:
            logger.warning("硬上限归档失败：session=%s：%s", session_id, exc)

    # ------------------------------------------------------------------ 内部

    def _fit_target_turns(
        self, snapshot: HistorySnapshot
    ) -> tuple[TurnRecord, ...]:
        """最后 6 轮之前的连续前缀，满足摘要输入上限。"""
        candidates = snapshot.turns[:-COMPACTION_FLOOR_TURNS]
        return self.summarizer.fit_turns(
            snapshot.summary,
            candidates,
            self._input_ceiling(),
        )

    def _input_ceiling(self) -> int:
        """摘要输入上限：``min(SUMMARY_INPUT_CEILING, available - 最大档位)``。

        公式由 summarizer 在构造时算好（protocol §8.3），这里直接复用，
        避免两处各算一遍产生漂移。
        """
        return self.summarizer.input_ceiling

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock


def _session_key(value: str) -> str:
    """锁的 key 必须和 store 的归一化口径一致。"""
    if not isinstance(value, str):
        raise TypeError("session_id 必须是字符串")

    key = value.strip()
    if not key:
        raise ValueError("session_id 不能为空")
    return key
