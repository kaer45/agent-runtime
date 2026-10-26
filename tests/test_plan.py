import pytest
from pydantic import ValidationError
from tasks.plan import TaskPlan


def test_valid_plan():
    """测试合法的完整任务计划"""
    data = {
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
    }
    plan = TaskPlan.model_validate(data)
    assert len(plan.tasks) == 3
    assert plan.tasks[2].dependencies == ["T1", "T2"]


def test_duplicate_task_id():
    """测试任务 ID 重复"""
    data = {
        "tasks": [
            {"id": "T1", "tool": "get_weather", "arguments": {}, "dependencies": []},
            {"id": "T1", "tool": "get_current_time", "arguments": {}, "dependencies": []},
        ]
    }
    with pytest.raises(ValidationError, match="重复任务 ID"):
        TaskPlan.model_validate(data)


def test_missing_dependency():
    """测试依赖了不存在的任务 ID"""
    data = {
        "tasks": [
            {"id": "T1", "tool": "get_weather", "arguments": {}, "dependencies": ["T99"]},
        ]
    }
    with pytest.raises(ValidationError, match="依赖不存在任务 T99"):
        TaskPlan.model_validate(data)


def test_missing_dependency_declaration():
    """测试参数里引用了 from_task，但 dependencies 列表里没写"""
    data = {
        "tasks": [
            {"id": "T1", "tool": "get_weather", "arguments": {}, "dependencies": []},
            {
                "id": "T2", "tool": "brewing",
                "arguments": {"weather": {"from_task": "T1"}},
                "dependencies": [],  # 故意不写 T1
            },
        ]
    }
    with pytest.raises(ValidationError, match="但没有声明对应的 dependencies"):
        TaskPlan.model_validate(data)


def test_self_dependency():
    """测试任务依赖了自己"""
    data = {
        "tasks": [
            {"id": "T1", "tool": "get_weather", "arguments": {}, "dependencies": ["T1"]},
        ]
    }
    with pytest.raises(ValidationError, match="不能依赖自己"):
        TaskPlan.model_validate(data)