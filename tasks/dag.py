from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tasks.plan import PlannedTask


class TaskDAG:
    """任务图（DAG）计算引擎，初始化时完成环检测与拓扑排序。"""

    def __init__(self, tasks: list["PlannedTask"]):
        self.tasks = tasks
        self.dependents: dict[str, list[str]] = {}
        self.indegree: dict[str, int] = {}
        self.topo_order: list[str] = []
        
        self._build_graph()
        self._check_cycle_and_sort()

    def _build_graph(self):
        """根据 tasks 构建反向边和入度表。"""
        task_ids = {task.id for task in self.tasks}
        self.dependents = {tid: [] for tid in task_ids}
        self.indegree = {tid: 0 for tid in task_ids}

        for task in self.tasks:
            self.indegree[task.id] = len(task.dependencies)
            for dep in task.dependencies:
                if dep in self.dependents:
                    self.dependents[dep].append(task.id)

    def _check_cycle_and_sort(self):
        """利用 Kahn 算法检测环，并生成拓扑序。"""
        tmp_indegree = dict(self.indegree)
        q = deque([tid for tid, d in tmp_indegree.items() if d == 0])
        
        while q:
            tid = q.popleft()
            self.topo_order.append(tid)
            for succ in self.dependents[tid]:
                tmp_indegree[succ] -= 1
                if tmp_indegree[succ] == 0:
                    q.append(succ)

        if len(self.topo_order) < len(self.tasks):
            cyclic_nodes = [tid for tid, d in tmp_indegree.items() if d > 0]
            raise ValueError(f"任务图存在循环依赖，涉及任务: {cyclic_nodes}")