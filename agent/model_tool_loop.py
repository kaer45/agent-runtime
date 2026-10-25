import json
import uuid
import logging

from agent.llm import LLM
from tools.registry import ToolRegistry
from tools.runner import ToolRunner
from tasks.scheduler import TaskScheduler
from tasks.models import Task, TaskState
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
            
            # 调试模型输出
            logger.debug("reasoning: %r", response.reasoning_content)
            logger.debug("content: %r", response.content)

            if not response.tool_calls:
                yield RuntimeEvent(
                    type=EventType.FINAL_ANSWER,
                    run_id=run_id,
                    data={"content": response.content}
                )
                return

            # 把带 tool_calls 的 assistant 消息放回上下文
            messages.append(response.model_dump())

            # 解析 tool_calls 构建任务图
            tasks: dict[str, Task] = {}
            for index, tool_call in enumerate(response.tool_calls):
                task_id = f"T{index+1}"
                try:
                    arguments = json.loads(tool_call.function.arguments)
                except Exception as e:
                    logger.warning("参数解析失败，默认置为空: %s", e)
                    arguments = {}

                tasks[task_id] = Task(
                    task_id=task_id,
                    tool_name=tool_call.function.name,
                    arguments=arguments
                )

            # 交给 DAG 调度器并转发事件
            try:
                async for event in self.scheduler.run(tasks, run_id):
                    yield event
            except Exception as e:
                logger.exception("调度异常")
                yield RuntimeEvent(
                    type=EventType.RUN_FINISHED, 
                    run_id=run_id,
                    data={"error": str(e), "error_type": type(e).__name__},
                )

            # 收集任务结果并作为 tool 消息回填
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