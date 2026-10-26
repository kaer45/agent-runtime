import asyncio
import logging
from collections import deque

from events.models import EventType, RuntimeEvent
from tools.runner import ToolRunner
from tasks.models import Task, TaskState
from tasks.dag import TaskDAG

logger = logging.getLogger(__name__)


class TaskScheduler:
    def __init__(self, tool_runner: ToolRunner):
        self.tool_runner = tool_runner

    async def _execute_task(self, task: Task) -> Task:
        """执行任务，并更新其状态。"""
        task.mark_running()
        
        # 调用纯粹的底层执行器
        result = await self.tool_runner.run(task.tool_name, task.arguments)
        
        if result["success"]:
            task.mark_success(result["result"])
        else:
            task.mark_failed(result["error"])
            
        return task

    async def run(self, tasks: dict[str, Task],dag:TaskDAG, run_id: str):
        """事件驱动调度器，异步生成 RuntimeEvent。
        
        注意：传入的 tasks 必须已经在 TaskPlan 中被校验为合法的 DAG。
        """
        
        # 1. 建反向边和入度表 (因为已经校验过，这里直接建图即可)
        dependents: dict[str, list[str]] = dag.dependents
        indegree: dict[str, int] = dag.indegree.copy()
        

        yield RuntimeEvent(type=EventType.RUN_STARTED, run_id=run_id)

        pending_events: list[RuntimeEvent] = []
        ready: deque[str] = deque(tid for tid, d in indegree.items() if d == 0)
        running: dict[asyncio.Task, str] = {}

        def launch(task_id: str):
            task = tasks[task_id]
            try:
                task.resolve_arguments(tasks)
            except Exception as e:
                task.mark_failed(f"Argument resolution failed: {e}")
                pending_events.append(RuntimeEvent(
                    type=EventType.TASK_BLOCKED, run_id=run_id, task_id=task_id,
                    data={"tool_name": task.tool_name, "reason": str(task.error)}
                ))
                return

            logger.info("启动任务 %s (工具: %s)", task_id, task.tool_name)
            pending_events.append(RuntimeEvent(
                type=EventType.TASK_STARTED, run_id=run_id, task_id=task_id,
                data={"tool_name": task.tool_name, "arguments": task.arguments, "dependencies": task.dependencies}
            ))
            
            fut = asyncio.create_task(self._execute_task(task))
            running[fut] = task_id

        # 启动初始没有依赖的任务
        while ready:
            launch(ready.popleft())
        for ev in pending_events:
            yield ev
        pending_events.clear()

        # 2. 事件驱动执行
        while running:
            done, _ = await asyncio.wait(running.keys(), return_when=asyncio.FIRST_COMPLETED)

            for fut in done:
                task_id = running.pop(fut)
                task = tasks[task_id]
                await fut

                if task.state == TaskState.SUCCESS:
                    pending_events.append(RuntimeEvent(
                        type=EventType.TASK_SUCCESS, run_id=run_id, task_id=task_id,
                        data={"tool_name": task.tool_name, "result": task.result}
                    ))
                    # 更新后续任务的入度
                    for succ in dependents[task_id]:
                        indegree[succ] -= 1
                        if indegree[succ] == 0:
                            ready.append(succ)
                else:
                    pending_events.append(RuntimeEvent(
                        type=EventType.TASK_FAILED, run_id=run_id, task_id=task_id,
                        data={"tool_name": task.tool_name, "error": str(task.error)}
                    ))

            while ready:
                launch(ready.popleft())
                
            for ev in pending_events:
                yield ev
            pending_events.clear()

        # 3. 失败原因传播
        self._propagate_blocked(tasks, dependents)
        for task in tasks.values():
            if task.state == TaskState.BLOCKED:
                yield RuntimeEvent(
                    type=EventType.TASK_BLOCKED, run_id=run_id, task_id=task.task_id,
                    data={"tool_name": task.tool_name, "reason": task.error}
                )

        yield RuntimeEvent(type=EventType.RUN_FINISHED, run_id=run_id)

    @staticmethod
    def _propagate_blocked(tasks, dependents) -> None:
        """多源 BFS 传播失败/阻塞状态。"""
        q = deque(tid for tid, t in tasks.items() if t.state in (TaskState.FAILED, TaskState.BLOCKED))
        seen = set(q)

        while q:
            tid = q.popleft()
            for succ in dependents[tid]:
                t = tasks[succ]
                if t.state == TaskState.PENDING and succ not in seen:
                    t.mark_blocked(f"Dependency '{tid}' blocked")
                    seen.add(succ)
                    q.append(succ)