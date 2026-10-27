"""把「规划 → 执行 → 回答」串成一次完整的 Agent 运行。"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator

from agent.llm import LLM
from agent.planner import PlanGenerationError, TaskPlanner
from events.models import EventType, RuntimeEvent
from tasks.plan_executor import TaskPlanExecutor

logger = logging.getLogger(__name__)


class PlanRunner:
    """执行一次「生成计划 → 执行 DAG → 生成回答」流程。

    Args:
        planner: 生成 TaskPlan。
        executor: 执行 TaskPlan。
        llm: 生成最终回答。
        system_prompt: 最终回答使用的人设与约束。
    """

    def __init__(
        self,
        planner: TaskPlanner,
        executor: TaskPlanExecutor,
        llm: LLM,
        system_prompt: str,
    ):
        self.planner = planner
        self.executor = executor
        self.llm = llm
        self.system_prompt = system_prompt

    async def run(self, user_input: str, run_id: str | None = None) -> AsyncIterator[RuntimeEvent]:
        """运行一次完整流程。

        Args:
            user_input: 用户请求。
            run_id: 可选的外部 run_id；不传则自动生成。

        Yields:
            RuntimeEvent：运行、规划、执行、回答四个阶段的事件。
        """
        run_id = run_id or str(uuid.uuid4())

        # 运行边界由 PlanRunner 负责，调度器的 run_* 事件在下面被过滤掉。
        yield RuntimeEvent(type=EventType.RUN_STARTED, run_id=run_id)
        yield RuntimeEvent(
            type=EventType.PLAN_STARTED,
            run_id=run_id,
            data={"message": "正在规划任务..."},
        )

        try:
            plan = await self.planner.plan(user_input)
        except PlanGenerationError as exc:
            yield RuntimeEvent(
                type=EventType.PLAN_FAILED,
                run_id=run_id,
                data={"error": str(exc)},
            )
            yield RuntimeEvent(
                type=EventType.RUN_FINISHED,
                run_id=run_id,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        yield RuntimeEvent(
            type=EventType.PLAN_READY,
            run_id=run_id,
            data={
                "task_count": len(plan.tasks),
                "tasks": [
                    {"id": t.id, "tool": t.tool, "dependencies": t.dependencies}
                    for t in plan.tasks
                ],
            },
        )

        results: dict[str, dict] = {}
        try:
            async for event in self.executor.execute(plan, run_id):
                # 调度器也会发 run_started/run_finished；一次运行只保留一个边界。
                if event.type in (EventType.RUN_STARTED, EventType.RUN_FINISHED):
                    continue
                if event.task_id is not None and event.type in (
                    EventType.TASK_SUCCESS,
                    EventType.TASK_FAILED,
                    EventType.TASK_BLOCKED,
                ):
                    results[event.task_id] = self._task_result(event)
                yield event
        except Exception as exc:
            logger.exception("执行计划失败")
            yield RuntimeEvent(
                type=EventType.RUN_FINISHED,
                run_id=run_id,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        try:
            content = await self._compose_answer(user_input, results)
        except Exception as exc:
            logger.exception("生成最终回答失败")
            yield RuntimeEvent(
                type=EventType.RUN_FINISHED,
                run_id=run_id,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        yield RuntimeEvent(
            type=EventType.FINAL_ANSWER,
            run_id=run_id,
            data={"content": content},
        )
        yield RuntimeEvent(type=EventType.RUN_FINISHED, run_id=run_id)

    @staticmethod
    def _task_result(event: RuntimeEvent) -> dict:
        """把任务事件整理成回答阶段使用的结构。"""
        if event.type == EventType.TASK_SUCCESS:
            return {
                "tool_name": event.data.get("tool_name"),
                "success": True,
                "result": event.data.get("result"),
            }
        error_key = "error" if event.type == EventType.TASK_FAILED else "reason"
        return {
            "tool_name": event.data.get("tool_name"),
            "success": False,
            "error": event.data.get(error_key),
        }

    async def _compose_answer(self, user_input: str, results: dict[str, dict]) -> str:
        """基于任务结果生成最终回答。"""
        result_text = json.dumps(results, ensure_ascii=False, indent=2, default=str)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
            {
                "role": "user",
                "content": (
                    "以下是任务执行结果（JSON）：\n"
                    f"{result_text}\n\n"
                    "请基于以上结果回答用户，不要编造未出现的信息。"
                ),
            },
        ]
        response = await self.llm.chat(messages=messages)
        return response.content or "模型未能给出回答"