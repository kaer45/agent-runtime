"""历史存储层的异常。

全部继承 ``HistoryStoreError``，方便 ``TurnOrchestrator`` 统一兜底：

- ``HistoryTurnInvalid``：一轮消息不满足 turn 结构；
- ``HistoryCoverageConflict``：``save_summary_cas`` 的期望水位不匹配；
- ``HistoryFloorUnmet``：最近 6 轮超过 ``history_ceiling``；
- ``HistoryHardCapExceeded``：硬上限维护后主表仍然超限。
"""

from __future__ import annotations


class HistoryStoreError(RuntimeError):
    """历史库读写失败、schema 不兼容或数据损坏。"""


class HistoryTurnInvalid(HistoryStoreError):
    """一轮消息不满足 turn 结构约束（strategy §4.5）。"""


class HistoryCoverageConflict(HistoryStoreError):
    """``save_summary_cas`` 的 expected coverage 不匹配。"""


class HistoryFloorUnmet(HistoryStoreError):
    """最近 6 轮超过 ``history_ceiling``，压缩无法修复。"""


class HistoryHardCapExceeded(HistoryStoreError):
    """硬上限维护后主表仍 >= ``hard_limit``。"""


class SummaryError(RuntimeError):
    """摘要生成失败：调用异常、空摘要、超窗或档位用尽。"""
