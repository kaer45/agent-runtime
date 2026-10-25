from typing import Any
from pydantic import Field, BaseModel, model_validator
from tasks.dag import TaskDAG


class PlannedTask(BaseModel):
    """任务计划中的单个任务"""
    id: str = Field(...,pattern=r"^T\d+$", description="任务唯一标识，如 T1")
    tool: str = Field(..., description="工具名称")
    arguments: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dependencies(self):
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError(f"任务 {self.id} 存在重复依赖")

        if self.id in self.dependencies:
            raise ValueError(f"任务 {self.id} 不能依赖自己")
        
        return self


class TaskPlan(BaseModel):
    """统一任务计划，包含全局校验逻辑"""
    tasks: list[PlannedTask]

    _dag: TaskDAG | None = None 

    def get_dag(self) -> TaskDAG:
        """获取绑定好的 DAG 实例"""
        if not self._dag:
            raise ValueError("DAG 尚未初始化")
        return self._dag

    @model_validator(mode="after")
    def validate_plan(self):
        # 1. 检查任务 id 是否重复
        task_ids = [task.id for task in self.tasks]
        task_id_set = set(task_ids)

        if len(task_ids) != len(task_id_set):
            raise ValueError("任务计划中存在重复任务 ID")

        # 2. 检查依赖存在性 & 参数引用合法性
        for task in self.tasks:
            for dependency in task.dependencies:
                if dependency not in task_id_set:
                    raise ValueError(f"任务 {task.id} 依赖不存在任务 {dependency}")

            references = self._find_references(task.arguments)
            for reference in references:
                if reference not in task_id_set:
                    raise ValueError(f"任务 {task.id} 引用了不存在的任务 {reference}")
                if reference not in task.dependencies:
                    raise ValueError(f"任务 {task.id} 引用了任务 {reference}, 但没有声明对应的 dependencies")

        # 3. 交给 DAG 引擎进行环检测与拓扑排序
        # 如果存在环，这里会抛出 ValueError
        self._dag=TaskDAG(self.tasks)

        return self


    @classmethod
    def _find_references(cls, value: Any) -> list[str]:
        """递归查找参数中的 from_task 引用"""
        references = []

        if isinstance(value, dict):
            if "from_task" in value:
                reference = value["from_task"]
                if not isinstance(reference, str):
                    raise ValueError("from_task 必须是字符串")
                references.append(reference)
            
            for item in value.values():
                references.extend(cls._find_references(item))

        elif isinstance(value, list):
            for item in value:
                references.extend(cls._find_references(item))

        return references