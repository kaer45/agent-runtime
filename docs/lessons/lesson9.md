# 第 9 课：用 JSON Schema 严格校验工具参数

> 模块二 · 模型规划接入（第 5/5 课）
> 前置：第 8 课（`ToolCatalog` 已能让模型读懂工具能力）
> 下一课：第 10 课进入上下文预算管理
> 预计用时：2-3 小时
> 本课分支：`dev`

---

## 0. 开工前检查

先确认第 8 课是干净的：

```powershell
git switch dev
git status --short
.venv\Scripts\python.exe -m pytest -q
```

期望：工作区干净，`78 passed`。

第 9 课新增一个依赖 `jsonschema`，先安装：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

确认可用：

```powershell
.venv\Scripts\python.exe -c "import jsonschema; print(jsonschema.__version__)"
```

---

## 1. 本课目标

第 8 课解决的是「模型知不知道工具怎么用」，本课解决「模型填的参数合不合法」。

```text
模型生成计划
    │
    ▼
TaskPlan 结构校验（Pydantic）      已有
    │
    ▼
工具名校验（TaskConverter）         已有
    │
    ▼
参数级 JSON Schema 校验             本课
    ├─ Planner：失败回填给模型修复
    ├─ Executor：执行前再挡一次
    └─ Scheduler：from_task 解析后再挡一次
```

学完你应该能回答：

1. 为什么 `TaskPlan` 和 `validate_tools()` 挡不住 `{"city": 123}`？
2. 为什么校验要放在 Planner、Executor、Scheduler 三个位置，而不是只放一个？
3. 为什么含 `from_task` 的任务不能在预校验阶段直接校验？
4. 为什么 `additionalProperties` 不写，额外参数就永远查不出来？

**本课产出**

| 交付物 | 说明 |
|---|---|
| `tasks/validator.py` | `ToolArgumentValidator`、`ArgumentValidationError` |
| `agent/planner.py` | 生成计划后做参数校验，失败回填 |
| `tasks/plan_executor.py` | 执行前防御性校验 |
| `tasks/scheduler.py` | `from_task` 解析后严格校验 |
| `tools/registry.py` | Schema 增加 `additionalProperties` |
| `requirements.txt` | 新增 `jsonschema` |
| `tests/unit/test_lesson09_argument_validation.py` | 12 个测试 |
| `scripts/lesson09_smoke.py` | 不调用模型的校验冒烟脚本 |

**本课明确不做**

- 不校验工具的**返回值**（后续可做）
- 不做业务语义校验（例如「成都是不是城市」）
- 不自动重试工具调用（校验错误不是瞬时错误）
- 不做 ToolRunner 的通用重试策略改造（第 23 课）
- 不接 MCP 的完整 Schema（第 28 课）

---

## 2. 问题：结构合法，不代表参数合法

当前 `TaskPlan` 只检查：

- 任务 ID 格式、重复 ID；
- 依赖是否存在、是否成环；
- `from_task` 引用是否存在、是否声明了依赖。

`TaskConverter.validate_tools()` 只检查工具名是否注册。

但模型完全可以生成：

```json
{
  "id": "T1",
  "tool": "get_weather",
  "arguments": {"city": 123},
  "dependencies": []
}
```

这个计划能通过 Pydantic，也能通过工具名校验，直到执行时才炸：

```text
AttributeError: 'int' object has no attribute 'strip'
```

或者更隐蔽：

```json
{"city": "成都", "unit": "celsius"}
```

`get_weather` 根本不接受 `unit`，但 Python 只有在调用时才会报 `unexpected keyword argument`。这时已经进入调度器，错误只能作为任务失败返回，模型失去了修复机会。

本课要把这类错误提前到**计划返回之前**，并且把错误信息回填给模型，让它重新提交。

---

## 3. 设计决策

### 3.1 用 `jsonschema`，不手写校验器

| 方案 | 优点 | 缺点 |
|---|---|---|
| 手写 if/else | 无依赖 | 只能覆盖当前几种类型，MCP / 未来 Schema 很快不够用 |
| `pydantic` 动态建模型 | 已有依赖 | JSON Schema → Pydantic 的转换本身又是一层坑 |
| `jsonschema` | 标准实现，支持 `$ref`、`enum`、组合 Schema | 多一个依赖 |

**选 `jsonschema`。** 工具 Schema 本来就是 JSON Schema，直接用标准校验器最稳；第 28 课接 MCP 时也能复用。

### 3.2 三个校验点，各管一段

| 位置 | 校验什么 | 失败后 |
|---|---|---|
| `TaskPlanner` | 模型刚生成的计划 | 错误回填，模型重试 |
| `TaskPlanExecutor` | 所有进入执行器的计划 | 直接拒绝，防止绕过 Planner |
| `TaskScheduler` | `from_task` 解析后的最终参数 | 标记任务失败，不调用工具 |

只放 Planner 不够：`TaskPlanExecutor` 可能被其他调用方直接使用，不能假设计划一定来自 Planner。

只放 Executor 也不够：Executor 看到的是带 `{"from_task": "T1"}` 的计划，无法校验最终类型。

只放 Scheduler 更不够：错误发生时已经进入执行阶段，Planner 没有机会修复。

### 3.3 `from_task` 的处理策略

计划里的参数可能是：

```json
{"weather": {"from_task": "T1"}}
```

预校验阶段拿不到 T1 的结果，所以：

1. **Planner / Executor 预校验**：只要任务参数里出现 `from_task`，就跳过这个任务的参数校验。
2. **Scheduler 执行前**：`resolve_arguments()` 已经把占位符换成真实结果，再做严格校验。

跳过整个任务，而不是只跳过占位符字段，是为了避免误报：

- 如果只删掉占位符字段，`required` 会误报缺失；
- 如果给占位符塞一个假值，又可能误报类型错误。

代价是：含占位符的任务如果有其他静态参数错误，会晚一点在 Scheduler 才被发现。这是可接受的折中——最终仍然会在调用工具前挡住。

### 3.4 `additionalProperties` 必须显式写

JSON Schema 默认允许额外字段。也就是说，即使 Schema 写了 `city`，`{"city": "成都", "unit": "c"}` 仍然合法。

所以 `ToolRegistry.get_tool_schema()` 要生成：

```json
"additionalProperties": false
```

除非函数签名里有 `**kwargs`，否则工具不应该接受多余参数。

### 3.5 错误信息要能直接回填

错误信息统一成：

```text
任务 T1 工具 get_weather 参数不合法：city: 123 is not of type 'string'
```

包含：

- 任务 ID；
- 工具名；
- 参数路径（`city` / `years.1` / `(arguments)`）；
- `jsonschema` 的原始说明。

Planner 把它放进 `tool` 消息：

```json
{"success": false, "error": "任务 T1 工具 get_weather 参数不合法：..."}
```

模型下一次就能针对 `city` 修复，而不是只看到一句「执行失败」。

---

## 4. 接口与数据结构

### 4.1 `ArgumentValidationError`

```python
class ArgumentValidationError(ValueError):
    """工具参数不符合 JSON Schema。"""
```

继承 `ValueError`，这样 Planner 现有的 `except (... ValueError ...)` 不需要大改。

### 4.2 `ToolArgumentValidator`

```python
class ToolArgumentValidator:
    def __init__(self, registry: ToolRegistry): ...

    def validate_arguments(
        self,
        tool_name: str,
        arguments: dict,
        *,
        task_id: str | None = None,
        allow_placeholders: bool = False,
    ) -> None: ...

    def validate_plan(
        self,
        plan: TaskPlan,
        *,
        allow_placeholders: bool = True,
    ) -> None: ...
```

`allow_placeholders=True` 时，遇到 `from_task` 就跳过该任务；默认 `False` 是严格模式。

### 4.3 三个接入点

```python
# TaskPlanner
self.argument_validator = ToolArgumentValidator(registry)
self.argument_validator.validate_plan(plan, allow_placeholders=True)

# TaskPlanExecutor
self.argument_validator.validate_plan(plan, allow_placeholders=True)

# TaskScheduler（resolve_arguments 之后）
self.argument_validator.validate_arguments(
    task.tool_name,
    task.arguments,
    task_id=task_id,
)
```

---

## 5. 实现步骤

### 第 1 步：加依赖

`requirements.txt`：

```text
jsonschema>=4.23
```

✅ 完成标志：`python -c "import jsonschema"` 成功。

### 第 2 步：让 Schema 拒绝多余参数

在 `ToolRegistry.get_tool_schema()` 里：

```python
allows_extra = any(
    param.kind is inspect.Parameter.VAR_KEYWORD
    for param in signature.parameters.values()
)

# ...
"additionalProperties": allows_extra,
```

✅ 完成标志：`get_weather` 的 Schema 里能看到 `"additionalProperties": false`。

### 第 3 步：建 `tasks/validator.py`

- `ArgumentValidationError`；
- `ToolArgumentValidator`；
- `_has_placeholder()` 递归判断；
- `_format_error()` 把错误压成一行。

✅ 完成标志：`validate_arguments("get_weather", {"city": 123})` 抛出带 `city` 和 `string` 的错误。

### 第 4 步：接入 Planner

- 构造时创建 `self.argument_validator`；
- 把两处 `TaskConverter.validate_tools(plan, self.registry)` 换成 `self._validate_plan(plan)`；
- `_validate_plan()` 先校验工具名，再校验参数；
- 复用现有 `except (..., ValueError, ...)` 捕获 `ArgumentValidationError`。

✅ 完成标志：模型第一次给 `{"city": 123}`，第二次被修复消息引导，最终返回合法计划。

### 第 5 步：接入 Executor

- 构造时创建 validator；
- `TaskConverter.validate_tools()` 之后调用 `validate_plan(plan, allow_placeholders=True)`。

✅ 完成标志：即使调用方绕过 Planner，非法静态参数也会在调度前被拒绝。

### 第 6 步：接入 Scheduler

在 `launch()` 里，`task.resolve_arguments(tasks)` 成功后：

```python
try:
    self.argument_validator.validate_arguments(
        task.tool_name,
        task.arguments,
        task_id=task_id,
    )
except ArgumentValidationError as e:
    task.mark_failed(str(e))
    pending_events.append(RuntimeEvent(
        type=EventType.TASK_FAILED,
        run_id=run_id,
        task_id=task_id,
        data={"tool_name": task.tool_name, "error": str(task.error)},
    ))
    return
```

✅ 完成标志：上游返回字符串、下游需要整数时，T2 在调用工具前变成 `TASK_FAILED`。

### 第 7 步：测试

按第 6 节的 12 个用例实现，先跑本课单测，再跑全量。

✅ 完成标志：`12 passed`，全量 `90 passed`。

### 第 8 步：冒烟

```powershell
.venv\Scripts\python.exe -m scripts.lesson09_smoke
```

不需要 API key。

---

## 6. 测试代码

### 6.1 `tests/unit/test_lesson09_argument_validation.py`

```python
import asyncio
from typing import Literal

import pytest

from agent.planner import PlanGenerationError, TaskPlanner
from events.models import EventType
from tasks.plan import TaskPlan
from tasks.plan_executor import TaskPlanExecutor
from tasks.scheduler import TaskScheduler
from tasks.validator import ArgumentValidationError, ToolArgumentValidator
from tests.fixtures.fake_llm import FakeLLM, FakeMessage, plan_call
from tools.builtin import get_current_time, get_weather
from tools.registry import ToolRegistry
from tools.runner import ToolRunner


def pour_wine(style: Literal["dry", "sweet"], bottles: int = 1) -> str:
    """按指定风格倒酒。

    Args:
        style: 酒款风格，只能是 dry 或 sweet。
        bottles: 倒几瓶，默认 1。
    """
    return f"{style} x {bottles}"


def select_wines(years: list[int]) -> list[str]:
    """按年份筛选酒款。

    Args:
        years: 年份列表，每个元素必须是整数。
    """
    return [str(year) for year in years]


async def current_label() -> str:
    """返回一个字符串标签，用于测试上游结果类型不匹配。"""
    return "not-an-int"


async def add_sugar(grams: int) -> str:
    """加糖。

    Args:
        grams: 糖的克数，必须是整数。
    """
    return f"{grams}g"


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.register(get_weather)
    r.register(get_current_time)
    r.register(pour_wine)
    r.register(select_wines)
    r.register(current_label)
    r.register(add_sugar)
    return r


def make_plan(tool: str, arguments: dict) -> TaskPlan:
    return TaskPlan(
        tasks=[
            {
                "id": "T1",
                "tool": tool,
                "arguments": arguments,
                "dependencies": [],
            }
        ]
    )


def make_chain_plan() -> TaskPlan:
    return TaskPlan(
        tasks=[
            {
                "id": "T1",
                "tool": "current_label",
                "arguments": {},
                "dependencies": [],
            },
            {
                "id": "T2",
                "tool": "add_sugar",
                "arguments": {"grams": {"from_task": "T1"}},
                "dependencies": ["T1"],
            },
        ]
    )


async def collect(events):
    return [event async for event in events]


def test_valid_arguments_pass(registry):
    validator = ToolArgumentValidator(registry)

    validator.validate_arguments("get_weather", {"city": "成都"})


def test_missing_required_argument_rejected(registry):
    validator = ToolArgumentValidator(registry)

    with pytest.raises(ArgumentValidationError) as exc:
        validator.validate_plan(make_plan("get_weather", {}))

    message = str(exc.value)
    assert "city" in message
    assert "required" in message


def test_wrong_type_rejected(registry):
    validator = ToolArgumentValidator(registry)

    with pytest.raises(ArgumentValidationError) as exc:
        validator.validate_plan(make_plan("get_weather", {"city": 123}))

    message = str(exc.value)
    assert "city" in message
    assert "string" in message


def test_extra_argument_rejected(registry):
    validator = ToolArgumentValidator(registry)

    with pytest.raises(ArgumentValidationError) as exc:
        validator.validate_plan(
            make_plan("get_weather", {"city": "成都", "unit": "celsius"})
        )

    message = str(exc.value)
    assert "unit" in message or "Additional" in message


def test_enum_value_rejected(registry):
    validator = ToolArgumentValidator(registry)

    validator.validate_arguments("pour_wine", {"style": "dry"})

    with pytest.raises(ArgumentValidationError) as exc:
        validator.validate_plan(make_plan("pour_wine", {"style": "sour"}))

    assert "sweet" in str(exc.value)


def test_array_item_type_rejected(registry):
    validator = ToolArgumentValidator(registry)

    with pytest.raises(ArgumentValidationError) as exc:
        validator.validate_plan(
            make_plan("select_wines", {"years": [2019, "2020"]})
        )

    message = str(exc.value)
    assert "2020" in message or "integer" in message


def test_plan_error_contains_task_and_tool(registry):
    validator = ToolArgumentValidator(registry)

    with pytest.raises(ArgumentValidationError) as exc:
        validator.validate_plan(make_plan("get_weather", {}))

    message = str(exc.value)
    assert "T1" in message
    assert "get_weather" in message


def test_placeholders_are_skipped_only_in_prevalidation(registry):
    validator = ToolArgumentValidator(registry)
    plan = make_chain_plan()

    # 预校验阶段看不到 T1 的结果，只能跳过含占位符的任务。
    validator.validate_plan(plan, allow_placeholders=True)

    # 关闭跳过后，占位符对象会被当成整数参数，校验失败。
    with pytest.raises(ArgumentValidationError):
        validator.validate_plan(plan, allow_placeholders=False)


def test_planner_repairs_invalid_arguments(registry):
    bad = [
        {
            "id": "T1",
            "tool": "get_weather",
            "arguments": {"city": 123},
            "dependencies": [],
        }
    ]
    good = [
        {
            "id": "T1",
            "tool": "get_weather",
            "arguments": {"city": "成都"},
            "dependencies": [],
        }
    ]
    llm = FakeLLM([
        FakeMessage(tool_calls=[plan_call(bad)]),
        FakeMessage(tool_calls=[plan_call(good, call_id="call_2")]),
    ])
    planner = TaskPlanner(llm, registry)

    plan = asyncio.run(planner.plan("今天成都天气怎么样"))

    assert plan.tasks[0].arguments["city"] == "成都"
    assert len(llm.calls) == 2

    tool_messages = [
        message for message in llm.calls[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert "city" in tool_messages[-1]["content"]


def test_planner_gives_up_after_invalid_arguments(registry):
    bad = [
        {
            "id": "T1",
            "tool": "get_weather",
            "arguments": {"city": 123},
            "dependencies": [],
        }
    ]
    llm = FakeLLM([
        FakeMessage(tool_calls=[plan_call(bad, call_id=f"c{i}")])
        for i in range(3)
    ])
    planner = TaskPlanner(llm, registry, max_repairs=2)

    with pytest.raises(PlanGenerationError, match="3 次尝试"):
        asyncio.run(planner.plan("今天成都天气怎么样"))

    assert len(llm.calls) == 3


def test_executor_rejects_invalid_arguments_before_scheduler(registry):
    executor = TaskPlanExecutor(scheduler=object(), registry=registry)
    plan = make_plan("get_weather", {"city": 123})

    with pytest.raises(ArgumentValidationError):
        asyncio.run(collect(executor.execute(plan, "run-1")))


def test_scheduler_validates_resolved_arguments(registry):
    executor = TaskPlanExecutor(
        scheduler=TaskScheduler(tool_runner=ToolRunner(registry=registry)),
        registry=registry,
    )

    events = asyncio.run(collect(executor.execute(make_chain_plan(), "run-1")))

    failed = [event for event in events if event.type == EventType.TASK_FAILED]
    assert [event.task_id for event in failed] == ["T2"]
    assert "integer" in failed[0].data["error"] or "not of type" in failed[0].data["error"]
```

### 6.2 这 12 个测试分别防什么

| 测试 | 证明 |
|---|---|
| `test_valid_arguments_pass` | 正常参数不会误报 |
| `test_missing_required_argument_rejected` | 必填参数缺失被挡住 |
| `test_wrong_type_rejected` | 类型错误被挡住 |
| `test_extra_argument_rejected` | `additionalProperties=false` 生效 |
| `test_enum_value_rejected` | `Literal` 生成的 `enum` 被校验 |
| `test_array_item_type_rejected` | 数组元素类型也会递归校验 |
| `test_plan_error_contains_task_and_tool` | 错误能定位到任务和工具 |
| `test_placeholders_are_skipped_only_in_prevalidation` | 预校验跳过占位符，严格模式不跳过 |
| `test_planner_repairs_invalid_arguments` | Planner 把错误回填并重试 |
| `test_planner_gives_up_after_invalid_arguments` | 修复次数用尽后抛 `PlanGenerationError` |
| `test_executor_rejects_invalid_arguments_before_scheduler` | Executor 不依赖 Planner 也做校验 |
| `test_scheduler_validates_resolved_arguments` | 上游结果回填后，类型不匹配在调用前失败 |

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson09_argument_validation.py -q
```

期望 `12 passed`；全量期望 `90 passed`。

---

## 7. 冒烟脚本

`scripts/lesson09_smoke.py`：

```python
from tasks.validator import ArgumentValidationError, ToolArgumentValidator
from tools.builtin import get_weather
from tools.registry import ToolRegistry

setup_registry = ToolRegistry()
setup_registry.register(get_weather)


def check(label: str, arguments: dict) -> None:
    validator = ToolArgumentValidator(setup_registry)
    try:
        validator.validate_arguments("get_weather", arguments)
    except ArgumentValidationError as exc:
        print(f"[FAIL] {label}: {exc}")
    else:
        print(f"[OK] {label}")


def main() -> None:
    check("正常参数", {"city": "成都"})
    check("缺少必填", {})
    check("类型错误", {"city": 123})
    check("多余参数", {"city": "成都", "unit": "celsius"})


if __name__ == "__main__":
    main()
```

运行：

```powershell
.venv\Scripts\python.exe -m scripts.lesson09_smoke
```

期望：

```text
[OK] 正常参数
[FAIL] 缺少必填: 工具 get_weather 参数不合法：(arguments): 'city' is a required property
[FAIL] 类型错误: 工具 get_weather 参数不合法：city: 123 is not of type 'string'
[FAIL] 多余参数: 工具 get_weather 参数不合法：(arguments): Additional properties are not allowed ('unit' was unexpected)
```

---

## 8. 参考实现

### 8.1 `tasks/validator.py`

```python
"""工具参数级 JSON Schema 校验。

Planner 在返回计划前调用，校验失败可以把错误回填给模型修复；
Executor / Scheduler 在执行前再调用一次，防止其他调用方绕过 Planner。
"""

from __future__ import annotations

import logging
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from tasks.plan import TaskPlan
from tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ArgumentValidationError(ValueError):
    """工具参数不符合 JSON Schema。"""


class ToolArgumentValidator:
    """按工具 Schema 校验参数字典。"""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def validate_arguments(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        task_id: str | None = None,
        allow_placeholders: bool = False,
    ) -> None:
        """校验单个工具调用参数。

        Args:
            tool_name: 已注册的工具名。
            arguments: 模型生成的参数字典。
            task_id: 可选的任务 ID，用于错误信息。
            allow_placeholders: 预校验阶段是否允许 ``{"from_task": "T1"}``。
                允许时，只要参数里出现占位符就跳过整个任务；等上游结果
                回填后，再由调度器做一次严格校验。

        Raises:
            ArgumentValidationError: 工具未注册，或参数不符合 Schema。
        """
        if not self.registry.has(tool_name):
            raise ArgumentValidationError(f"工具 {tool_name} 未注册")

        if allow_placeholders and self._has_placeholder(arguments):
            logger.debug("任务 %s 含 from_task 占位符，跳过预校验", task_id)
            return

        schema = self.registry.get_tool_schema(tool_name)["function"]["parameters"]
        validator = Draft202012Validator(schema)
        errors = sorted(
            validator.iter_errors(arguments),
            key=lambda error: [str(part) for part in error.absolute_path],
        )
        if not errors:
            return

        details = "；".join(self._format_error(error) for error in errors)
        prefix = f"任务 {task_id} " if task_id else ""
        raise ArgumentValidationError(
            f"{prefix}工具 {tool_name} 参数不合法：{details}"
        )

    def validate_plan(
        self,
        plan: TaskPlan,
        *,
        allow_placeholders: bool = True,
    ) -> None:
        """校验计划里的所有任务参数。

        Args:
            plan: 待校验的任务计划。
            allow_placeholders: 是否跳过含 ``from_task`` 占位符的任务。
        """
        for task in plan.tasks:
            self.validate_arguments(
                task.tool,
                task.arguments,
                task_id=task.id,
                allow_placeholders=allow_placeholders,
            )

    @staticmethod
    def _has_placeholder(value: Any) -> bool:
        """递归判断参数中是否存在 from_task 占位符。"""
        if isinstance(value, dict):
            if "from_task" in value:
                return True
            return any(
                ToolArgumentValidator._has_placeholder(item)
                for item in value.values()
            )
        if isinstance(value, list):
            return any(
                ToolArgumentValidator._has_placeholder(item)
                for item in value
            )
        return False

    @staticmethod
    def _format_error(error: ValidationError) -> str:
        """把 jsonschema 的错误压缩成一行，方便回填给模型。"""
        path = ".".join(str(part) for part in error.absolute_path) or "(arguments)"
        return f"{path}: {error.message}"
```

### 8.2 `tools/registry.py`：拒绝多余参数

```python
allows_extra = any(
    param.kind is inspect.Parameter.VAR_KEYWORD
    for param in signature.parameters.values()
)

# ...
"parameters": {
    "type": "object",
    "properties": properties,
    "required": required,
    # 工具没有 **kwargs 时，多余的参数应当被校验器拒绝。
    "additionalProperties": allows_extra,
},
```

### 8.3 `agent/planner.py`：预校验 + 错误回填

```python
from tasks.validator import ToolArgumentValidator

# __init__ 里
self.argument_validator = ToolArgumentValidator(registry)

def _validate_plan(self, plan: TaskPlan) -> None:
    """校验工具名和参数；预校验阶段跳过 from_task 占位符。"""
    TaskConverter.validate_tools(plan, self.registry)
    self.argument_validator.validate_plan(plan, allow_placeholders=True)
```

两个成功返回点都改成：

```python
plan = TaskPlan.model_validate(arguments)
self._validate_plan(plan)
logger.info("计划生成成功，任务数=%d", len(plan.tasks))
return plan
```

`ArgumentValidationError` 继承 `ValueError`，所以现有的修复循环不需要重写；错误会进入 `_format_error()`，再作为 `tool` 消息回填。

### 8.4 `tasks/plan_executor.py`：执行前防御

```python
from tasks.validator import ToolArgumentValidator

# __init__ 里
self.argument_validator = ToolArgumentValidator(registry)

# execute() 里
TaskConverter.validate_tools(plan, self.registry)
# 预校验跳过 from_task；占位符解析后由调度器再做一次严格校验。
self.argument_validator.validate_plan(plan, allow_placeholders=True)
tasks = TaskConverter.convert(plan)
```

### 8.5 `tasks/scheduler.py`：解析后严格校验

```python
from tasks.validator import ArgumentValidationError, ToolArgumentValidator

# __init__ 里
self.argument_validator = ToolArgumentValidator(tool_runner.registry)

# launch() 里，resolve_arguments 成功后
try:
    self.argument_validator.validate_arguments(
        task.tool_name,
        task.arguments,
        task_id=task_id,
    )
except ArgumentValidationError as e:
    task.mark_failed(str(e))
    pending_events.append(RuntimeEvent(
        type=EventType.TASK_FAILED,
        run_id=run_id,
        task_id=task_id,
        data={"tool_name": task.tool_name, "error": str(task.error)},
    ))
    return
```

### 8.6 `requirements.txt`

```text
jsonschema>=4.23
```

---

## 9. 验收清单（DoD）

- [x] `requirements.txt` 包含 `jsonschema`
- [ ] `get_weather` 的 Schema 包含 `"additionalProperties": false`
- [ ] `ToolArgumentValidator` 能校验必填、类型、额外参数、`enum`、数组元素
- [ ] 含 `from_task` 的任务在预校验阶段被跳过
- [ ] `TaskPlanner` 参数不合法时会回填错误并重试
- [ ] 修复次数用尽时抛 `PlanGenerationError`
- [ ] `TaskPlanExecutor` 不依赖 Planner 也能挡住非法参数
- [ ] `TaskScheduler` 在解析占位符后能挡住类型不匹配
- [x] `pytest tests/unit/test_lesson09_argument_validation.py -q` → 12 passed
- [x] 全量 `pytest -q` → 90 passed
- [ ] 冒烟脚本 4 条输出符合预期
- [ ] 第 5-8 课测试没有被改坏

---

## 10. 常见坑

| 坑 | 现象 | 处理 |
|---|---|---|
| 只校验工具名 | `{"city": 123}` 进入执行阶段才报错 | 加参数级校验 |
| 忘记 `additionalProperties` | 多余参数永远合法 | 在 Registry 生成 `false` |
| 预校验不跳过 `from_task` | 正常计划被误报类型错误 | `allow_placeholders=True` |
| 只在预校验做 | 上游结果类型不对时漏掉 | Scheduler 解析后再校验 |
| 只在 Scheduler 做 | 模型没有修复机会 | Planner 也要校验并回填 |
| `TaskPlanExecutor.execute()` 是异步生成器 | `with pytest.raises` 包住调用却不触发 | 用 `async for` / `collect()` 消费 |
| 把校验错误当瞬时错误重试 | 重复调用模型或工具 | 校验失败直接回填 / 拒绝，不重试 |
| 错误路径为空 | required 错误只显示 `(arguments)` | 保留 `(arguments)`，消息里仍有字段名 |
| `jsonschema` 版本过旧 | `Draft202012Validator` 不可用 | `jsonschema>=4.23` |
| 工具实现有 `**kwargs` | 额外参数被误杀 | `additionalProperties` 按 `VAR_KEYWORD` 动态设置 |

---

## 11. 扩展与思考题

1. **返回值校验**：工具返回 `str`，下游却要 `int`，能否也给工具定义 `returns` Schema？
2. **错误聚合策略**：一次返回全部错误，还是只返回第一个？错误太多会不会让模型迷失？
3. **语义校验**：`city="火星"` 类型合法，业务不合法。这层应该放在工具内，还是单独的业务校验器？
4. **Schema 缓存**：每次 `get_tool_schema()` 都重新 inspect；工具多时要不要缓存编译后的 validator？
5. **`$ref` / 嵌套对象**：MCP 的 Schema 可能包含 `$ref`，`Draft202012Validator` 能处理，但你的错误路径渲染是否还清楚？
6. **安全边界**：校验能挡住格式错误，但挡不住恶意参数；权限和审批是第 29 课的事。

---

## 12. 与第 10 课的衔接

第 8 课让模型「看得懂工具」，第 9 课让模型「填得合法」，但还有一个问题没解决：

> 对话一长，历史消息、工具目录、任务结果会一起把上下文撑爆。

第 10 课开始做上下文预算：

- 统计 token；
- 给 system / 历史 / 工具结果分配预算；
- 超预算时按优先级裁剪或摘要；
- 保证最关键的指令和事实不被裁掉。

可以记成一句话：

> **第 8 课管「说清楚」，第 9 课管「填合法」，第 10 课管「装得下」。**
