from enum import Enum
import logging

logger = logging.getLogger(__name__)

class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"


class Task:
    def __init__(self, task_id: str, tool_name: str, arguments: dict, dependencies: list[str] | None = None):
        self.task_id = task_id
        self.tool_name = tool_name
        self.arguments = arguments
        self.dependencies = dependencies or []
        
        self.state = TaskState.PENDING
        self.result = None
        self.error = None

    def mark_running(self):
        self.state = TaskState.RUNNING

    def mark_success(self, result):
        self.state = TaskState.SUCCESS
        self.result = result

    def mark_failed(self, error):
        self.state = TaskState.FAILED
        self.error = error

    def mark_blocked(self, error=None):
        self.state = TaskState.BLOCKED
        self.error = error

    def can_run(self, tasks: dict[str, "Task"]) -> bool:
        """判断当前任务的所有依赖是否均已成功。
        
        注意：在事件驱动的 DAG 调度器中，主要是靠 indegree 判断，
        此方法主要用于简单的同步调度或单测场景。
        """
        return all(
            tasks[dep].state == TaskState.SUCCESS 
            for dep in self.dependencies
        )

    def resolve_arguments(self, tasks: dict[str, "Task"]) -> dict:
        """解析参数中的 {"from_task": "T1"} 引用。"""
        resolved = {}
        for key, value in self.arguments.items():
            if isinstance(value, dict) and "from_task" in value:
                ref_id = value["from_task"]
                if ref_id not in tasks:
                    raise KeyError(f"Task '{self.task_id}' references unknown task '{ref_id}'")
                ref_task = tasks[ref_id]
                if ref_task.state != TaskState.SUCCESS:
                    raise RuntimeError(
                        f"Task '{self.task_id}' cannot resolve '{ref_id}': state is {ref_task.state.value}"
                    )
                resolved[key] = ref_task.result
            else:
                resolved[key] = value
        self.arguments = resolved
        return resolved

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "tool_name": self.tool_name,
            "state": self.state.value,
            "result": self.result,
            "error": self.error,
            "dependencies": self.dependencies,
        }