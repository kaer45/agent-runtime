import json
import uuid
import logging

from agent.llm import LLM
from tools.registry import ToolRegistry
from tools.runner import ToolRunner
from tasks.scheduler import TaskScheduler
from tasks.models import Task, TaskState
from tasks.plan import TaskPlan, PlannedTask
from tasks.converter import TaskConverter
from events.models import EventType, RuntimeEvent

logger = logging.getLogger(__name__)

class ModelToolLoop:
    def __init__(
        self,
        llm: LLM,
        tool_registry: ToolRegistry,
        tool_runner: ToolRunner,
        scheduler: TaskScheduler,
        max_iterations: int = 10
    ):
        self.llm = llm
        self.tool_registry = tool_registry
        self.tool_runner = tool_runner
        self.scheduler = scheduler
        self.max_iterations = max_iterations

    async def run(self, messages: list[dict]):
        """执行模型与工具交互的主循环，以异步生成器形式产出事件流。"""
        tools = self.tool_registry.get_all_schemas()
        run_id = str(uuid.uuid4())
        
        logger.info("循环开始，可用工具: %s", [t['function']['name'] for t in tools])

        for _ in range(self.max_iterations):
            yield RuntimeEvent(
                type=EventType.MODEL_STARTED,
                run_id=run_id,
                data={"message": "正在分析用户请求..."}
            )
            
            response = await self.llm.chat(messages=messages, tools=tools)
            
            yield RuntimeEvent(
                type=EventType.MODEL_FINISHED,
                run_id=run_id,
                data={"tool_call_count": len(response.tool_calls or [])}
            )
            
            logger.debug("reasoning: %r", response.reasoning_content)
            logger.debug("content: %r", response.content)

            if not response.tool_calls:
                yield RuntimeEvent(
                    type=EventType.FINAL_ANSWER,
                    run_id=run_id,
                    data={"content": response.content}
                )
                return

            messages.append(response.model_dump())

            # 1. 构建 TaskPlan 数据
            tasks_data = []
            for index, tool_call in enumerate(response.tool_calls):
                task_id = f"T{index+1}"
                try:
                    arguments = json.loads(tool_call.function.arguments)
                except Exception as e:
                    logger.warning("参数解析失败，默认置为空: %s", e)
                    arguments = {}

                tasks_data.append({
                    "id": task_id,
                    "tool": tool_call.function.name,
                    "arguments": arguments,
                    "dependencies": []
                })

            # 2. 进行强校验并生成 DAG
            try:
                # 这里生成任务计划时，会进行许多校验，同时生成TaskDAG
                plan = TaskPlan(tasks=tasks_data)
                dag = plan.get_dag() 

                # 校验工具是否都已注册
                TaskConverter.validate_tools(plan, self.tool_registry)

                # 转换为运行时 Task 实例
                tasks = TaskConverter.convert(plan)

            except Exception as e:
                logger.error("任务图生成失败: %s", e)
                yield RuntimeEvent(
                    type=EventType.RUN_FINISHED,
                    run_id=run_id,
                    data={"error": f"任务图不合法: {e}", "error_type": type(e).__name__},
                )
                return
            

            # 3. 交给 DAG 调度器执行并转发事件
            try:
                async for event in self.scheduler.run(tasks, dag, run_id):
                    yield event
            except Exception as e:
                logger.exception("调度异常")
                yield RuntimeEvent(
                    type=EventType.RUN_FINISHED, 
                    run_id=run_id,
                    data={"error": str(e), "error_type": type(e).__name__},
                )

            # 5. 收集任务结果并作为 tool 消息回填
            for index, tool_call in enumerate(response.tool_calls):
                task_id = f"T{index+1}"
                task = tasks[task_id]
                result = {
                    "success": task.state == TaskState.SUCCESS,
                    "result": task.result,
                    "error": task.error
                }
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str)
                })

        raise RuntimeError(f"Tool loop exceeded maximum iterations: '{self.max_iterations}'.")