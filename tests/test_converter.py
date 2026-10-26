import pytest
from tasks.converter import TaskConverter
from tasks.plan import TaskPlan
from tools.registry import ToolRegistry


def create_plan():
    """辅助函数：构建标准的测试 Plan"""
    return TaskPlan.model_validate({
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


def test_convert_plan_to_tasks():
    plan = create_plan()
    tasks = TaskConverter.convert(plan)

    assert len(tasks) == 3

    task1 = tasks["T1"]
    task3 = tasks["T3"]

    assert task1.tool_name == "get_weather"
    assert task1.arguments == {"city": "天津"}

    assert task3.tool_name == "brewing"
    assert task3.dependencies == ["T1", "T2"]
    assert task3.arguments["weather"] == {"from_task": "T1"}


def test_convert_does_not_share_top_level_arguments():
    plan = create_plan()
    tasks = TaskConverter.convert(plan)

    planned_task = plan.tasks[0]
    runtime_task = tasks["T1"]

    runtime_task.arguments["city"] = "北京"

    # 验证浅拷贝生效，原 Plan 参数不被篡改
    assert planned_task.arguments["city"] == "天津"


def test_validate_tools_success():
    plan = create_plan()
    registry = ToolRegistry()
    
    def get_weather(city: str): pass
    def get_current_time(): pass
    def brewing(weather: str, current_time: str): pass
    
    registry.register(get_weather)
    registry.register(get_current_time)
    registry.register(brewing)

    # 校验通过，不抛异常
    TaskConverter.validate_tools(plan, registry)


def test_validate_tools_missing():
    plan = create_plan()
    registry = ToolRegistry()  # 空注册表

    with pytest.raises(ValueError, match="未注册的工具"):
        TaskConverter.validate_tools(plan, registry)