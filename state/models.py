"""结构化会话状态：偏好、目标、约束、进度和已确认事实。"""

from __future__ import annotations

import re

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_GOAL_ID_RE = re.compile(r"^g_[0-9a-f]{12}$")


def is_valid_goal_id(value: str) -> bool:
    """Goal ID 是否合法：g_ + 12 位小写十六进制。"""
    return bool(_GOAL_ID_RE.fullmatch(value))


class GoalStatus(str, Enum):
    ACTIVE = "active"
    DONE = "done"
    ABANDONED = "abandoned"


class Goal(BaseModel):
    # 配置模型字典，extra 默认是ignore，遇到模型没有的字段会静默丢失。现在使用forbid，遇到没有字段报错。
    # str_strip_whitespace 开启可以把字符串自动去掉首尾空格，这样比较时不会出错。
    model_config = ConfigDict(extra="forbid",str_strip_whitespace=True)

    id: str = ""
    text : str
    status: GoalStatus = GoalStatus.ACTIVE

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if value and not is_valid_goal_id(value):
            raise ValueError("goal.id 格式必须是 g_ + 12 位十六进制")
        return value

    @field_validator("text")
    @classmethod
    def text_must_be_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("goal.text 不能为空")
        return value


class SessionState(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    preferences: dict[str, Any] = Field(default_factory=dict)
    goals: list[Goal] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    progress: dict[str, str] = Field(default_factory=dict)
    facts: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_state(self):
        # 这里检查 text 和 id 既兼容旧数据没有 id，同时因为目标文本是大模型生成，所以还是需要检查。
        goal_texts = [goal.text for goal in self.goals]
        if len(goal_texts) != len(set(goal_texts)):
            raise ValueError("goals 中存在重复目标")

        goal_ids = [goal.id for goal in self.goals if goal.id]
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