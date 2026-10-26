import uuid
from collections.abc import AsyncIterator

from tasks.converter import TaskConverter
from tasks.dag import TaskDAG
from tasks.plan import TaskPlan
from tasks.scheduler import TaskScheduler
from tools.registry import ToolRegistry
from events.models import RuntimeEvent


class TaskPlanExecutor:
    '''负责将经过校验的计划交给调度器执行'''
    def __init__(self,scheduler:TaskScheduler,registry:ToolRegistry):
        self.scheduler=scheduler
        self.registry=registry

    async def execute(self,plan:TaskPlan)->AsyncIterator[RuntimeEvent]:
        # 1. 直接从 plan 中获取已经校验好的 DAG（不重复计算）
        dag = plan.get_dag()

        # 2. 检查工具是否已经注册
        TaskConverter.validate_tools(plan, self.registry)

        # 3. 转换成现有运行时 Task
        tasks = TaskConverter.convert(plan)

        # 4. 交给现有调度器执行，传入 tasks, dag, run_id
        run_id = str(uuid.uuid4())
        async for event in self.scheduler.run(tasks, dag, run_id):
            yield event