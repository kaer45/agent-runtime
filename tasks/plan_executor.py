from collections.abc import AsyncIterator

from events.models import RuntimeEvent
from tasks.converter import TaskConverter
from tasks.plan import TaskPlan
from tasks.scheduler import TaskScheduler
from tools.registry import ToolRegistry


class TaskPlanExecutor:
    """把校验过的 TaskPlan 交给调度器执行。"""

    def __init__(self, scheduler: TaskScheduler, registry: ToolRegistry):
        """初始化执行器。

        Args:
            scheduler: DAG 调度器。
            registry: 用于再次校验计划里的工具名。
        """
        self.scheduler = scheduler
        self.registry = registry

    async def execute(self, plan: TaskPlan, run_id: str) -> AsyncIterator[RuntimeEvent]:
        """执行计划并逐个产出事件。

        Args:
            plan: 已通过校验的任务计划。
            run_id: 本次运行的 ID，调度器事件会沿用它。

        Yields:
            RuntimeEvent：调度器产生的任务事件。
        """
        dag = plan.get_dag()
        # 这里有必要重新校验，因为TaskPlan不一定只来自TaskPlanner，所以可以得到经验，来调用方有多个时，底层都要做校验
        TaskConverter.validate_tools(plan, self.registry) 
        tasks = TaskConverter.convert(plan)

        async for event in self.scheduler.run(tasks, dag, run_id):
            yield event