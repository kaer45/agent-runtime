import pytest
from pydantic import ValidationError
from tasks.dag import TaskDAG
from tasks.plan import TaskPlan, PlannedTask


def test_valid_dag_topo_order():
    """测试正常 DAG 的拓扑排序"""
    plan = TaskPlan.model_validate({
        "tasks": [
            {"id": "T1", "tool": "get_weather", "arguments": {"city": "天津"}, "dependencies": []},
            {"id": "T2", "tool": "get_current_time", "arguments": {}, "dependencies": []},
            {
                "id": "T3", "tool": "brewing",
                "arguments": {
                    "weather": {"from_task": "T1"},
                    "current_time": {"from_task": "T2"},
                },
                "dependencies": ["T1", "T2"],
            },
        ]
    })

    dag = TaskDAG(plan.tasks)
    order = dag.topo_order

    assert len(order) == 3
    # 无论 T1 和 T2 谁在前，它们都必须在 T3 之前
    assert order.index("T1") < order.index("T3")
    assert order.index("T2") < order.index("T3")


def test_detect_cycle_two_nodes():
    """测试 T1 <-> T2 的简单环"""
    # 环检测在 TaskPlan 初始化时就会触发
    with pytest.raises(ValidationError, match="循环依赖"):
        TaskPlan.model_validate({
            "tasks": [
                {"id": "T1", "tool": "get_weather", "arguments": {}, "dependencies": ["T2"]},
                {"id": "T2", "tool": "get_current_time", "arguments": {}, "dependencies": ["T1"]},
            ]
        })


def test_detect_longer_cycle():
    """测试 T1 -> T2 -> T3 -> T1 的长环"""
    with pytest.raises(ValidationError, match="循环依赖"):
        TaskPlan.model_validate({
            "tasks": [
                {"id": "T1", "tool": "get_weather", "arguments": {}, "dependencies": ["T3"]},
                {"id": "T2", "tool": "get_current_time", "arguments": {}, "dependencies": ["T1"]},
                {"id": "T3", "tool": "brewing", "arguments": {}, "dependencies": ["T2"]},
            ]
        })


def test_dag_indegree_and_dependents():
    """测试 TaskDAG 内部构建的入度表和反向边是否正确"""
    tasks = [
        PlannedTask(id="T1", tool="a", arguments={}, dependencies=[]),
        PlannedTask(id="T2", tool="b", arguments={}, dependencies=[]),
        PlannedTask(id="T3", tool="c", arguments={}, dependencies=["T1", "T2"]),
    ]
    
    dag = TaskDAG(tasks)
    
    # 验证入度
    assert dag.indegree["T1"] == 0
    assert dag.indegree["T2"] == 0
    assert dag.indegree["T3"] == 2
    
    # 验证反向边（依赖关系）
    assert "T3" in dag.dependents["T1"]
    assert "T3" in dag.dependents["T2"]
    assert len(dag.dependents["T3"]) == 0