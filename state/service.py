"""把 StateDelta 应用到 SessionState，并处理版本与冲突。

``StateStore`` 负责持久化，``SessionRegistry`` 负责同一会话串行化；
``StateService`` 负责 delta 的语义校验、状态转换和乐观锁版本检查。
"""

from __future__ import annotations

from copy import deepcopy

from state.extractor import StateDelta
from state.models import Goal, GoalStatus, SessionState
from state.session import SessionRegistry
from state.store import StateConflictError, StateSnapshot


class StateApplyError(ValueError):
    """Delta 无法应用到当前状态（例如目标不存在、状态转换非法）。"""


def apply_delta(state: SessionState, delta: StateDelta) -> SessionState:
    """把 delta 应用到一个新状态，不修改传入的 state。

    Raises:
        StateApplyError: 目标不存在，或目标状态转换非法。
    """
    if not isinstance(state, SessionState):
        raise TypeError("state 必须是 SessionState")
    if not isinstance(delta, StateDelta):
        raise TypeError("delta 必须是 StateDelta")

    preferences = deepcopy(state.preferences)
    facts = deepcopy(state.facts)
    progress = dict(state.progress)
    constraints = list(state.constraints)
    goals = [goal.model_copy(deep=True) for goal in state.goals]

    for key in delta.preferences_remove:
        preferences.pop(key, None)
    for key, value in delta.preferences_set.items():
        preferences[key] = deepcopy(value)

    for key in delta.facts_remove:
        facts.pop(key, None)
    for key, value in delta.facts_set.items():
        facts[key] = deepcopy(value)

    for key in delta.progress_remove:
        progress.pop(key, None)
    for key, value in delta.progress_set.items():
        progress[key] = value

    for text in delta.constraints_remove:
        if text in constraints:
            constraints.remove(text)
    for text in delta.constraints_add:
        if text not in constraints:
            constraints.append(text)

    for text in delta.goals_add:
        if not any(goal.text == text for goal in goals):
            goals.append(Goal(text=text))

    for text in delta.goals_complete:
        index = _find_goal(goals, text)
        goal = goals[index]
        if goal.status == GoalStatus.DONE:
            continue
        if goal.status == GoalStatus.ABANDONED:
            raise StateApplyError(f"已放弃的目标不能标记完成：{text}")
        goals[index] = goal.model_copy(update={"status": GoalStatus.DONE})

    for text in delta.goals_abandon:
        index = _find_goal(goals, text)
        goal = goals[index]
        if goal.status == GoalStatus.ABANDONED:
            continue
        if goal.status == GoalStatus.DONE:
            raise StateApplyError(f"已完成的目标不能标记放弃：{text}")
        goals[index] = goal.model_copy(update={"status": GoalStatus.ABANDONED})

    return SessionState(
        preferences=preferences,
        goals=goals,
        constraints=constraints,
        progress=progress,
        facts=facts,
    )


def _find_goal(goals: list[Goal], text: str) -> int:
    for index, goal in enumerate(goals):
        if goal.text == text:
            return index
    raise StateApplyError(f"目标不存在：{text}")


class StateService:
    """带版本和冲突检查的状态服务。

    Example:
        service = StateService(registry)
        snapshot = await service.apply("session-1", delta)
    """

    def __init__(self, registry: SessionRegistry):
        self.registry = registry

    async def get(self, session_id: str) -> StateSnapshot:
        """读取状态快照；不存在的会话返回空状态和版本 0。"""
        async with self.registry.acquire(session_id):
            snapshot = self.registry.load_snapshot(session_id)
            return snapshot if snapshot is not None else StateSnapshot(
                state=SessionState(),
                version=0,
            )

    async def apply(
        self,
        session_id: str,
        delta: StateDelta,
        *,
        expected_version: int | None = None,
    ) -> StateSnapshot:
        """应用 delta，返回新的状态快照。

        Args:
            session_id: 会话 ID。
            delta: 状态增量。
            expected_version: 可选的期望版本；不传则应用到最新版本。

        Raises:
            StateConflictError: 期望版本与当前版本不一致。
            StateApplyError: delta 语义上无法应用。
        """
        if not isinstance(delta, StateDelta):
            raise TypeError("delta 必须是 StateDelta")
        if expected_version is not None:
            if (
                not isinstance(expected_version, int)
                or isinstance(expected_version, bool)
                or expected_version < 0
            ):
                raise ValueError("expected_version 必须是非负整数或 None")

        async with self.registry.acquire(session_id):
            snapshot = self.registry.load_snapshot(session_id)
            current = snapshot if snapshot is not None else StateSnapshot(
                state=SessionState(),
                version=0,
            )

            if (
                expected_version is not None
                and expected_version != current.version
            ):
                raise StateConflictError(
                    f"状态版本冲突：期望 {expected_version}，"
                    f"当前 {current.version}"
                )

            if delta.is_empty():
                return current

            updated = apply_delta(current.state, delta)

            if updated == current.state:
                return current

            new_version = self.registry.save_snapshot(
                session_id,
                updated,
                expected_version=current.version,
            )
            return StateSnapshot(state=updated, version=new_version)
