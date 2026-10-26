from tasks.models import Task
from tasks.plan import TaskPlan
from tools.registry import ToolRegistry

class TaskConverter:
    '''将任务计划转换成运行时 Task'''

    @staticmethod
    def convert(plan:TaskPlan)->dict[str,Task]:
        tasks={}
        for planned_task in plan.tasks:
            tasks[planned_task.id]=Task(
                task_id=planned_task.id,
                tool_name=planned_task.tool,
                arguments=planned_task.arguments.copy(), # 浅拷贝
                dependencies=list(planned_task.dependencies)
            )
        return tasks
        

    @staticmethod
    def validate_tools(plan:TaskPlan,registry:ToolRegistry)->None:
        for planned_task in plan.tasks:
            if not registry.has(planned_task.tool):
                raise ValueError(f"任务 {planned_task.id} 使用了未注册的工具: "
                                 f"{planned_task.tool}")
    