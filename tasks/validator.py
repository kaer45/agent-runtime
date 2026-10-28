"""工具参数级 JSON Schema 校验。

Planner 在返回计划前调用，校验失败可以把错误回填给模型修复；
Executor / Scheduler 在执行前再调用一次，防止其他调用方绕过 Planner。
"""

from __future__ import annotations

import logging
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from tasks.plan import TaskPlan
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ArgumentValidationError(ValueError):
    """工具参数不符合 JSON Schema。"""


class ToolArgumentValidator:
    """按工具 Schema 校验参数字典。"""

    def __init__(self,registry:ToolRegistry):
        self.registry=registry

    def validate_arguments(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        task_id: str | None = None,
        allow_placeholders: bool = False,
    ) -> None:
        """校验单个工具调用参数。

        Args:
            tool_name: 已注册的工具名。
            arguments: 模型生成的参数字典。
            task_id: 可选的任务 ID，用于错误信息。
            allow_placeholders: 预校验阶段是否允许 ``{"from_task": "T1"}``。
                允许时，只要参数里出现占位符就跳过整个任务；等上游结果
                回填后，再由调度器做一次严格校验。

        Raises:
            ArgumentValidationError: 工具未注册，或参数不符合 Schema。
        """
        if not self.registry.has(tool_name):
            raise ArgumentValidationError(f"工具 {tool_name} 未注册")

        if allow_placeholders and self._has_placeholder(arguments):
            logger.debug("任务 %s 含 from_task 占位符，跳过预校验", task_id)
            return

        schema = self.registry.get_tool_schema(tool_name)["function"]["parameters"]
        validator = Draft202012Validator(schema)
        errors = sorted(
            validator.iter_errors(arguments),
            key=lambda error: [str(part) for part in error.absolute_path],
        )
        if not errors:
            return

        details = "；".join(self._format_error(error) for error in errors)
        prefix = f"任务 {task_id} " if task_id else ""
        raise ArgumentValidationError(
            f"{prefix}工具 {tool_name} 参数不合法：{details}"
        )

    def validate_plan(
        self,
        plan: TaskPlan,
        *,
        allow_placeholders: bool = True,
    ) -> None:
        """校验计划里的所有任务参数。

        Args:
            plan: 待校验的任务计划。
            allow_placeholders: 是否跳过含 ``from_task`` 占位符的任务。
        """
        for task in plan.tasks:
            self.validate_arguments(
                task.tool,
                task.arguments,
                task_id=task.id,
                allow_placeholders=allow_placeholders,
            )

    @staticmethod
    def _has_placeholder(value: Any) -> bool:
        """递归判断参数中是否存在 from_task 占位符。"""
        if isinstance(value, dict):
            if "from_task" in value:
                return True
            return any(
                ToolArgumentValidator._has_placeholder(item)
                for item in value.values()
            )
        if isinstance(value, list):
            return any(
                ToolArgumentValidator._has_placeholder(item)
                for item in value
            )
        return False

    @staticmethod
    def _format_error(error: ValidationError) -> str:
        """把 jsonschema 的错误压缩成一行，方便回填给模型。"""
        path = ".".join(str(part) for part in error.absolute_path) or "(arguments)"
        return f"{path}: {error.message}"