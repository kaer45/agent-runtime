"""任务规划器：让模型通过 function call 生成合法的 TaskPlan。

只负责生成与校验计划，不执行任务；校验失败时把错误回填给模型重试。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from tasks.converter import TaskConverter
from tasks.plan import TaskPlan
from tools.registry import ToolRegistry

if TYPE_CHECKING:
    from agent.llm import LLM

logger = logging.getLogger(__name__)

PLAN_TOOL_NAME = "submit_task_plan"

# 手写 JSON Schema：Pydantic 生成的 $defs/$ref 部分兼容接口不支持。
PLAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PLAN_TOOL_NAME,
        "description": (
            "提交一份任务计划。只有调用本工具才算完成规划；"
            "一次调用提交完整计划，不要在文本里输出计划。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "任务列表，至少一个任务。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "pattern": "^T[0-9]+$",
                                "description": "任务 ID，从 T1 开始连续编号",
                            },
                            "tool": {
                                "type": "string",
                                "description": "已注册的工具名",
                            },
                            "arguments": {
                                "type": "object",
                                "description": '工具参数；引用前序任务结果用 {"from_task": "T1"}',
                            },
                            "dependencies": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "前置任务 ID 列表，无依赖写 []",
                            },
                        },
                        "required": ["id", "tool", "arguments", "dependencies"],
                    },
                }
            },
            "required": ["tasks"],
        },
    },
}

PROTOCOL_RULES = """\
你是任务规划器。你的唯一职责是把用户请求转换成一个合法的任务计划，通过 submit_task_plan 工具提交。

规则：
1. 只输出计划，不执行工具，不编造工具结果。
2. 任务 ID 从 T1 开始连续编号（T1、T2、T3...）。
3. 只能使用下面列出的工具名，禁止编造工具。
4. 参数名必须与工具目录完全一致，不要做同义改写（例如 current_time 不能写成 time）。
5. 如果某个参数需要前序任务的结果，参数值写成 {"from_task": "T1"}，并且必须把 "T1" 放进当前任务的 dependencies。
6. dependencies 只写真正的前置任务；没有依赖写 []。
7. 无参数工具把 arguments 写成 {}。
8. 能并行的任务不要互相依赖；有数据依赖的任务必须声明依赖。
9. 不要输出解释性文字，所有内容都在工具调用里。
"""


class PlanGenerationError(RuntimeError):
    """模型在限定次数内没有生成合法计划。"""


class TaskPlanner:
    """把用户请求转换成 TaskPlan。

    Example:
        planner = TaskPlanner(llm, registry)
        plan = await planner.plan("今天波尔多适合酿酒吗？")
    """

    def __init__(self, llm: "LLM", registry: ToolRegistry, max_repairs: int = 2):
        """初始化规划器。

        Args:
            llm: 提供 async chat(...) 的对象；测试时可注入假模型。
            registry: 工具注册表，提供工具名与参数 Schema。
            max_repairs: 校验失败后的最大重试次数（不含首次）。

        Raises:
            ValueError: max_repairs 为负数时。
        """
        if max_repairs < 0:
            raise ValueError("max_repairs 不能为负数")
        self.llm = llm
        self.registry = registry
        self.max_repairs = max_repairs

    def build_messages(self, user_input: str) -> list[dict]:
        """构造初始消息：system 提示 + user 请求。"""
        return [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": user_input},
        ]

    def _format_tool(self, name: str) -> str:
        """把工具渲染成一行「名字(参数: 类型): 描述」。"""
        fn = self.registry.get_tool_schema(name)["function"]
        parameters = fn.get("parameters") or {}
        properties = parameters.get("properties") or {}
        required = set(parameters.get("required") or [])

        args = []
        for pname, spec in properties.items():
            ptype = spec.get("type", "any")
            if isinstance(ptype, list):
                ptype = "|".join(str(t) for t in ptype)
            optional = "?" if pname not in required else ""
            args.append(f"{pname}: {ptype}{optional}")

        signature = ", ".join(args)
        description = fn.get("description", "")
        return f"- {name}({signature}): {description}"

    def _system_prompt(self) -> str:
        """拼接 system prompt：固定规则 + 动态工具目录。"""
        catalog = "\n".join(
            self._format_tool(name) for name in self.registry.list_tools()
        )
        return (
            PROTOCOL_RULES
            + "\n可用工具（参数格式：名字: 类型，? 表示可选）：\n"
            + catalog
        )

    async def plan(self, user_input: str) -> TaskPlan:
        """生成计划，失败时自动修复。

        强制模型调用 ``submit_task_plan``，再用 TaskPlan 和工具名校验；
        失败时把错误作为 tool 消息回填，最多重试 ``max_repairs`` 次。

        Args:
            user_input: 用户请求。

        Returns:
            通过校验的 TaskPlan。

        Raises:
            PlanGenerationError: 重试次数用尽仍无合法计划。
        """
        messages = self.build_messages(user_input)
        last_error = "未知错误"

        for attempt in range(self.max_repairs + 1):
            response = await self.llm.chat(
                messages=messages,
                tools=[PLAN_TOOL],
                # 强制模型走结构化 tool_call。
                tool_choice={"type": "function", "function": {"name": PLAN_TOOL_NAME}},
                # DeepSeek 思考模式不支持 named tool_choice。
                thinking=False,
            )

            call = self._find_plan_call(response)
            if call is None:
                # 模型可能没调用工具，尝试把 content 当 JSON 计划解析。
                try:
                    plan = TaskPlan.model_validate(json.loads(response.content or ""))
                    TaskConverter.validate_tools(plan, self.registry)
                    logger.warning("模型未调用工具，已从 content 解析计划")
                    return plan
                except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
                    last_error = (
                        f"模型没有调用 {PLAN_TOOL_NAME}；"
                        f"content 也不是合法计划：{self._format_error(exc)}"
                    )
                    messages.append({"role": "assistant", "content": response.content or ""})
                    messages.append(
                        {
                            "role": "user",
                            "content": f"{last_error}。请调用 {PLAN_TOOL_NAME} 重新提交。",
                        }
                    )
                    continue

            try:
                # 接口返回的 arguments 是 JSON 字符串。
                arguments = json.loads(call.function.arguments)
                plan = TaskPlan.model_validate(arguments)
                # 参数级校验留到第 9 课。
                TaskConverter.validate_tools(plan, self.registry)
                logger.info("计划生成成功，任务数=%d", len(plan.tasks))
                return plan
            except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
                last_error = self._format_error(exc)
                logger.warning("计划校验失败（第 %d 次）: %s", attempt + 1, last_error)

                # 修复消息必须成对：先 assistant(tool_calls)，再 tool 结果。
                messages.append(self._assistant_tool_call_message(call, response))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(
                            {"success": False, "error": last_error},
                            ensure_ascii=False,
                        ),
                    }
                )

        raise PlanGenerationError(
            f"模型在 {self.max_repairs + 1} 次尝试后仍未生成合法计划，最后错误：{last_error}"
        )

    @staticmethod
    def _find_plan_call(response):
        """返回名为 submit_task_plan 的 tool call；没有则返回 None。"""
        for call in response.tool_calls or []:
            if call.function.name == PLAN_TOOL_NAME:
                return call
        return None

    @staticmethod
    def _assistant_tool_call_message(call, response) -> dict:
        """把 tool call 还原成 assistant 消息（不回填 reasoning_content）。"""
        return {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
            ],
        }

    @staticmethod
    def _format_error(exc: Exception) -> str:
        """把校验异常转成易读的一行文本。"""
        if isinstance(exc, ValidationError):
            details = []
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"]) or "(plan)"
                details.append(f"{loc}: {err['msg']}")
            return "计划不合法：" + "；".join(details)
        return f"计划不合法：{exc}"
