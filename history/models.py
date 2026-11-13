from dataclasses import dataclass

@dataclass(frozen=True)
class RollingSummary:
    """当前摘要及其已覆盖的消息数量。"""

    text: str = ""
    covered_messages: int = 0


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