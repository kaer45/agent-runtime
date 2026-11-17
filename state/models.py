"""结构化会话状态：偏好、目标、约束、进度和已确认事实。"""

from __future__ import annotations

import re
import uuid

from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_GOAL_ID_RE = re.compile(r"^g_[0-9a-f]{12}$")


def new_goal_id() -> str:
    """生成新的目标 ID：g_ + 12 位十六进制。"""
    return "g_" + uuid.uuid4().hex[:12]

def is_valid_goal_id(value: str) -> bool:
    """Goal ID 是否合法：g_ + 12 位小写十六进制。"""
    return bool(_GOAL_ID_RE.fullmatch(value))


class GoalStatus(str, Enum):
    ACTIVE = "active"
    DONE = "done"
    ABANDONED = "abandoned"


class Goal(BaseModel):
    """目标模型，有转换状态机"""
    # 配置模型字典，extra 默认是ignore，遇到模型没有的字段会静默丢失。现在使用forbid，遇到没有字段报错。
    # str_strip_whitespace 开启可以把字符串自动去掉首尾空格，这样比较时不会出错。
    model_config = ConfigDict(extra="forbid",str_strip_whitespace=True)

    id: str = Field(default_factory=new_goal_id)
    text : str
    status: GoalStatus = GoalStatus.ACTIVE

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not is_valid_goal_id(value):
            raise ValueError("goal.id 格式必须是 g_ + 12 位十六进制")
        return value

    @field_validator("text")
    @classmethod
    def text_must_be_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("goal.text 不能为空")
        return value

    def mark_done(self) -> Goal:
        """标记目标完成，合法转换为：

        1. done -> done，幂等。
        2. active -> done。
        """
        if self.status == GoalStatus.DONE:
            return self

        if self.status == GoalStatus.ABANDONED:
            raise ValueError(f"已放弃的目标不能标记完成：{self.id}")
        return self.model_copy(update={"status":GoalStatus.DONE})

    def mark_abandoned(self) -> Goal:
        """标记目标放弃，合法转换为：

        1. active -> abandoned。
        2. abandoned -> abandoned，幂等。
        """
        if self.status == GoalStatus.ABANDONED:
            return self

        if self.status == GoalStatus.DONE:
            raise ValueError(f"已完成的目标不能标记放弃：{self.id}")
        return self.model_copy(update={"status":GoalStatus.ABANDONED})


class SessionState(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    preferences: dict[str, Any] = Field(default_factory=dict)
    goals: list[Goal] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    progress: dict[str, str] = Field(default_factory=dict)
    facts: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_state(self):
        # Goal.id 在创建时生成并校验；这里只检查会话内唯一性。
        goal_texts = [goal.text for goal in self.goals]
        if len(goal_texts) != len(set(goal_texts)):
            raise ValueError("goals 中存在重复目标")

        goal_ids = [goal.id for goal in self.goals]
        if len(goal_ids) != len(set(goal_ids)):
            raise ValueError("goals 中存在重复目标 ID")

        if len(self.constraints) != len(set(self.constraints)):
            raise ValueError("constraints 中存在重复约束")
        if any(not constraint.strip() for constraint in self.constraints):
            raise ValueError("constraints 不能包含空字符串")

        if any(not key.strip() for key in self.preferences):
            raise ValueError("preferences 的 key 不能为空")
        if any(not key.strip() for key in self.facts):
            raise ValueError("facts 的 key 不能为空")
        if any(not key.strip() for key in self.progress):
            raise ValueError("progress 的 key 不能为空")

        return self

class StateDelta(BaseModel):
    """一次对话轮次产生的状态增量。

    字段名中的 ``_set`` 表示新增或覆盖，``_remove`` 表示删除；
    ``goals_add`` 使用目标文本，``goals_complete`` / ``goals_abandon``
    使用已有目标的 ID。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    preferences_set: dict[str, Any] = Field(default_factory=dict)
    preferences_remove: list[str] = Field(default_factory=list)
    facts_set: dict[str, Any] = Field(default_factory=dict)
    facts_remove: list[str] = Field(default_factory=list)
    goals_add: list[str] = Field(default_factory=list)
    goals_complete: list[str] = Field(default_factory=list)
    goals_abandon: list[str] = Field(default_factory=list)
    constraints_add: list[str] = Field(default_factory=list)
    constraints_remove: list[str] = Field(default_factory=list)
    progress_set: dict[str, str] = Field(default_factory=dict)
    progress_remove: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_delta(self):
        self._reject_overlap(
            self.preferences_set, self.preferences_remove, "preferences"
        )
        self._reject_overlap(self.facts_set, self.facts_remove, "facts")
        self._reject_overlap(self.progress_set, self.progress_remove, "progress")
        self._reject_overlap(
            set(self.constraints_add), set(self.constraints_remove), "constraints"
        )

        self._reject_overlap(
            set(self.goals_complete),
            set(self.goals_abandon),
            "goals complete/abandon",
        )

        for name, values in (
            ("preferences_remove", self.preferences_remove),
            ("facts_remove", self.facts_remove),
            ("goals_add", self.goals_add),
            ("goals_complete", self.goals_complete),
            ("goals_abandon", self.goals_abandon),
            ("constraints_add", self.constraints_add),
            ("constraints_remove", self.constraints_remove),
            ("progress_remove", self.progress_remove),
        ):
            self._reject_blank_and_duplicate(name, values)

        for name, values in (
            ("goals_complete", self.goals_complete),
            ("goals_abandon", self.goals_abandon),
        ):
            self._reject_invalid_goal_ids(name, values)

        for name, values in (
            ("preferences_set", self.preferences_set),
            ("facts_set", self.facts_set),
            ("progress_set", self.progress_set),
        ):
            for key in values:
                if not key.strip():
                    raise ValueError(f"{name} 的 key 不能为空")

        return self

    @staticmethod
    def _reject_overlap(set_values, remove_values, name: str) -> None:
        overlap = set(set_values) & set(remove_values)
        if overlap:
            if "goals" in name:
                raise ValueError(f"{name} 不能同时完成和放弃目标：{sorted(overlap)}")
            raise ValueError(f"{name} 不能同时设置和删除：{sorted(overlap)}")

    @staticmethod
    def _reject_blank_and_duplicate(name: str, values: list[str]) -> None:
        if any(not value.strip() for value in values):
            raise ValueError(f"{name} 不能包含空字符串")
        if len(values) != len(set(values)):
            raise ValueError(f"{name} 不能包含重复项")

    @staticmethod
    def _reject_invalid_goal_ids(name: str, values: list[str]) -> None:
        invalid = [value for value in values if not is_valid_goal_id(value)]
        if invalid:
            raise ValueError(
                f"{name} 必须是目标 ID（g_ + 12 位十六进制）：{invalid}"
            )

    def is_empty(self) -> bool:
        """是否没有任何变更。"""
        return not any(
            (
                self.preferences_set,
                self.preferences_remove,
                self.facts_set,
                self.facts_remove,
                self.goals_add,
                self.goals_complete,
                self.goals_abandon,
                self.constraints_add,
                self.constraints_remove,
                self.progress_set,
                self.progress_remove,
            )
        )


@dataclass(frozen=True)
class StateSnapshot:
    """从存储层读出的一份状态快照。

    放在 models 层，避免 context / history 为了一个值对象反向依赖 store。
    """

    state: SessionState
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.state, SessionState):
            raise TypeError("StateSnapshot.state 必须是 SessionState")

        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("StateSnapshot.version 必须是整数")
        if self.version < 0:
            raise ValueError("StateSnapshot.version 不能为负数")