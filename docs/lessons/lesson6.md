# 第 6 课：把模型生成的任务计划接入 Agent Runtime

> 模块二 · 模型规划接入（第 2/5 课）
> 前置：第 5 课（`TaskPlanner` 已能生成并通过校验的 `TaskPlan`）
> 下一课：第 7 课让 Agent 区分普通对话与任务规划
> 预计用时：3-4 小时
> 本课分支：`dev`

---

## 0. 开工前检查

```powershell
git switch dev
git status --short
.venv\Scripts\python.exe -m pytest -q
```

期望：

- 工作区干净；
- `21 passed`（第 1-5 课）；
- 第 5 课冒烟能产出 `T1/T2/T3`，且 T3 参数是 `current_time`：

```powershell
.venv\Scripts\python.exe -m scripts.lesson05_smoke "今天波尔多适合酿酒吗？"
```

如果这里不通过，先回第 5 课修好。

---

## 1. 本课目标

把「规划」和「执行」接起来：

```text
用户请求
  → TaskPlanner.plan()       生成 TaskPlan（第 5 课）
  → TaskPlanExecutor         执行 DAG（第 4 课的调度器）
  → 汇总任务结果
  → LLM 生成最终回答
  → SSE 事件流
```

学完你应该能回答：

1. 计划任务的 ID 是 `T1/T2`，没有 `tool_call_id`，结果为什么不能伪造成 `role="tool"` 消息？
2. 为什么 `run_started/run_finished` 只能有一个来源？
3. 为什么最终回答要继续用同一个 LLM，但不能把工具再传给它？

**本课产出**

| 交付物 | 说明 |
|---|---|
| `events/models.py` | 新增 `plan_started / plan_ready / plan_failed` |
| `tasks/plan_executor.py` | `execute(plan, run_id)`，不再自己生成 run_id |
| `agent/plan_runner.py` | 规划 → 执行 → 回答的编排器 |
| `agent/runtime.py` | 用 `PlanRunner` 替换 `ModelToolLoop` |
| `agent/__init__.py` | 导出更新 |
| `tests/unit/test_lesson06_plan_runner.py` | 7 个测试 |
| `scripts/lesson06_smoke.py` | 真实调用冒烟 |
| `static/index.html`（可选） | 展示规划阶段事件 |

**本课明确不做**

- 不判断「聊天还是任务」（第 7 课）
- 不做工具参数级校验（第 9 课）
- 不做重试/检查点/暂停（第 21-25 课）
- 不做统一事件总线（第 30 课）
- 失败后不自动重新规划（第 35 课）

---

## 2. 为什么需要这一课

看现在的 [model_tool_loop.py:66-130](../../agent/model_tool_loop.py#L66-L130)，它做了两件事：

```python
# 1. 把 tool_calls 硬转成平铺任务
tasks_data.append({
    "id": task_id,
    "tool": tool_call.function.name,
    "arguments": arguments,
    "dependencies": [],
})

# 2. 用 tool_call_id 把结果回填
messages.append({
    "role": "tool",
    "tool_call_id": tool_call.id,
    "content": json.dumps(result),
})
```

第 5 课已经能生成带依赖的 `TaskPlan`，但一旦接进来，第 2 步就不成立了：

- 计划里的任务是 `T1/T2/T3`，**没有 `tool_call_id`**；
- 这些任务不是主模型这一轮产生的 tool call，强行伪造 `role="tool"` 会被接口拒绝（消息序列里找不到对应的 `tool_calls`）。

所以本课的核心不是「把 planner 塞进循环」，而是**重新设计结果回填方式**，并把运行边界收拢到一个地方。

---

## 3. 设计决策

### 3.1 执行结果怎么回填给模型

| 方案 | 做法 | 结果 |
|---|---|---|
| A. 伪造成 tool 消息 | 给每个任务编一个 `tool_call_id` | ❌ OpenAI 兼容接口要求 tool 消息对应真实的 assistant tool_calls，容易 400 |
| B. 汇总成一条 user 消息 | 把结果 JSON / 文本作为新的一轮 user 输入 | ✅ 简单、稳定，不依赖 tool 协议 |
| C. 塞进 system prompt | 把结果拼到系统提示里 | ❌ 污染人设，且系统提示会被反复发送 |

**选 B**。执行结束后再调用一次 LLM，消息结构是：

```python
[
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": 用户原始请求},
    {"role": "user", "content": "以下是任务执行结果（JSON）：...\n请基于结果回答用户。"},
]
```

注意：最终回答这次调用**不传 tools**。传了它就可能继续发起工具调用，又回到「谁来执行」的问题。

### 3.2 运行边界谁负责

现在 `TaskScheduler.run()` 每次都会发 `run_started` 和 `run_finished`（[scheduler.py:42](../../tasks/scheduler.py#L42)、[scheduler.py:117](../../tasks/scheduler.py#L117)）。

如果 `PlanRunner` 再发一遍，一次请求会出现两个 run 边界，前端状态会乱。本课约定：

> **`PlanRunner` 负责一次用户请求的 run 边界；调度器的 `run_started/run_finished` 被过滤掉。**

这样事件流从「运行开始」到「运行结束」只包一层：

```text
run_started
  plan_started
  plan_ready
  task_started / task_success / task_failed / task_blocked
  final_answer
run_finished
```

### 3.3 文件怎么放

| 方案 | 做法 | 评价 |
|---|---|---|
| A. 原地改 `ModelToolLoop` | 保留类名，替换内部实现 | 改动小，但名字已经名不副实 |
| B. 新建 `PlanRunner`，删除旧文件 | 职责清晰，第 7 课只在外面加路由 | ✅ 推荐 |

本课选 B。组件关系：

```text
AgentRuntime
  └─ PlanRunner
       ├─ TaskPlanner      生成 TaskPlan
       ├─ TaskPlanExecutor 执行 DAG
       │    └─ TaskScheduler
       │         └─ ToolRunner
       └─ LLM              生成最终回答
```

### 3.4 异常路径

| 阶段 | 事件 | 是否继续 |
|---|---|---|
| 规划失败（`PlanGenerationError`） | `plan_failed` + `run_finished(error)` | 停止，不执行 |
| 执行抛异常 | `run_finished(error)` | 停止，不回答 |
| 最终回答失败 | `run_finished(error)` | 停止 |
| 单个任务失败 | `task_failed` / `task_blocked` | 继续，把失败信息交给回答阶段 |

任务失败不终止整个 run：模型可以在最终回答里解释「哪一步失败、为什么」。

---

## 4. 接口与数据结构

### 4.1 新增事件类型

```python
class EventType(str, Enum):
    ...
    PLAN_STARTED = "plan_started"
    PLAN_READY = "plan_ready"
    PLAN_FAILED = "plan_failed"
```

`PLAN_READY.data`：

```json
{
  "task_count": 3,
  "tasks": [
    {"id": "T1", "tool": "get_weather", "dependencies": []},
    {"id": "T2", "tool": "get_current_time", "dependencies": []},
    {"id": "T3", "tool": "brewing", "dependencies": ["T1", "T2"]}
  ]
}
```

### 4.2 `TaskPlanExecutor.execute(plan, run_id)`

签名从：

```python
async def execute(self, plan: TaskPlan) -> AsyncIterator[RuntimeEvent]:
    ...
```

改成：

```python
async def execute(self, plan: TaskPlan, run_id: str) -> AsyncIterator[RuntimeEvent]:
    ...
```

`run_id` 由 `PlanRunner` 统一生成，保证规划、任务、回答三阶段的事件属于同一次运行。

### 4.3 `PlanRunner`

```python
class PlanRunner:
    def __init__(self, planner, executor, llm, system_prompt): ...

    async def run(self, user_input: str, run_id: str | None = None):
        """生成计划 → 执行 → 生成回答，逐条 yield 事件。"""
```

### 4.4 结果结构

`PlanRunner` 从任务事件里收集：

```json
{
  "T1": {"tool_name": "get_weather", "success": true, "result": "波尔多-晴"},
  "T2": {"tool_name": "get_current_time", "success": true, "result": "2026-10-26 22:00:00"},
  "T3": {"tool_name": "brewing", "success": true, "result": "适合酿酒"}
}
```

失败/阻塞任务：

```json
{
  "T1": {"tool_name": "get_weather", "success": false, "error": "boom"},
  "T3": {"tool_name": "brewing", "success": false, "error": "Dependency 'T1' blocked"}
}
```

注意 `task_blocked` 的字段是 `reason`，收集时要映射成 `error`。

---

## 5. 实现步骤

### 第 1 步：加事件类型

修改 [events/models.py](../../events/models.py)，新增三个 `EventType`。不要改 `RuntimeEvent` 结构。

✅ 完成标志：`EventType.PLAN_READY.value == "plan_ready"`。

### 第 2 步：让 `TaskPlanExecutor` 接收 `run_id`

修改 [tasks/plan_executor.py](../../tasks/plan_executor.py)：

- 删除内部的 `uuid.uuid4()`；
- 签名改为 `execute(plan, run_id)`；
- 其余逻辑（取 DAG、校验工具、convert、调度）不变。

✅ 完成标志：调度器事件里的 `run_id` 和传入值一致。

### 第 3 步：建 `PlanRunner` 骨架

新建 `agent/plan_runner.py`：

- 构造参数：`planner / executor / llm / system_prompt`；
- `run()` 先发 `run_started`、`plan_started`；
- 用 `try/except PlanGenerationError` 处理规划失败。

✅ 完成标志：规划失败时能发出 `plan_failed` + `run_finished(error)`。

### 第 4 步：执行并收集结果

- 调 `executor.execute(plan, run_id)`；
- **跳过**调度器的 `run_started/run_finished`；
- 遇到 `task_success / task_failed / task_blocked` 时写入 `results`；
- 其它任务事件原样转发。

✅ 完成标志：成功计划执行完后 `results` 有三个任务。

### 第 5 步：生成最终回答

- 把 `results` 序列化成 JSON；
- 拼 `system + 用户请求 + 结果说明` 三条消息；
- 调 `llm.chat(messages)`，**不传 tools**；
- 发 `final_answer`，再发 `run_finished`。

✅ 完成标志：事件流最后两条是 `final_answer`、`run_finished`。

### 第 6 步：接入 `AgentRuntime`

修改 [agent/runtime.py](../../agent/runtime.py)：

- 组装 `TaskPlanner`、`TaskPlanExecutor`、`PlanRunner`；
- `AgentRuntime.run()` 直接转发 `PlanRunner.run()`；
- 保留 `SYSTEM_PROMPT` 作为最终回答的人设。

同时更新 [agent/__init__.py](../../agent/__init__.py)，不再导出 `ModelToolLoop`。

✅ 完成标志：`from agent.runtime import AgentRuntime` 正常，`api/server.py` 不用改。

### 第 7 步：移除旧主循环

确认没有引用后删除：

```powershell
git rm agent/model_tool_loop.py
```

如果暂时想保留做对照，至少不要 import 它——两套执行路径并存时，改错文件很难发现。

✅ 完成标志：`grep -r "model_tool_loop" --include=*.py .` 没有结果。

### 第 8 步：测试、冒烟、前端

- 写 6.1 的 7 个测试；
- 写 7 的冒烟脚本；
- 可选：按第 8 节给前端补 `plan_*` 事件。

✅ 完成标志：`28 passed`，真实冒烟跑通。

---

## 6. 测试代码

### 6.1 `tests/unit/test_lesson06_plan_runner.py`

```python
import asyncio

from agent.plan_runner import PlanRunner
from agent.planner import PlanGenerationError
from events.models import EventType, RuntimeEvent
from tasks.plan import TaskPlan
from tasks.plan_executor import TaskPlanExecutor
from tasks.scheduler import TaskScheduler
from tests.fixtures.fake_llm import FakeLLM, FakeMessage
from tools.registry import ToolRegistry
from tools.runner import ToolRunner


TASKS = [
    {"id": "T1", "tool": "get_weather", "arguments": {"city": "波尔多"}, "dependencies": []},
    {"id": "T2", "tool": "get_current_time", "arguments": {}, "dependencies": []},
    {
        "id": "T3",
        "tool": "brewing",
        "arguments": {
            "weather": {"from_task": "T1"},
            "current_time": {"from_task": "T2"},
        },
        "dependencies": ["T1", "T2"],
    },
]


def make_plan() -> TaskPlan:
    return TaskPlan.model_validate({"tasks": TASKS})


class FakePlanner:
    def __init__(self, plan=None, error=None):
        self.plan_obj = plan
        self.error = error
        self.calls = []

    async def plan(self, user_input: str) -> TaskPlan:
        self.calls.append(user_input)
        if self.error:
            raise self.error
        return self.plan_obj


class FakeExecutor:
    def __init__(self, events):
        self.events = events
        self.calls = []

    async def execute(self, plan, run_id):
        self.calls.append((plan, run_id))
        for event in self.events:
            yield RuntimeEvent(
                type=event.type,
                run_id=run_id,
                task_id=event.task_id,
                data=event.data,
            )


class BrokenExecutor:
    async def execute(self, plan, run_id):
        raise RuntimeError("executor boom")
        yield  # pragma: no cover


async def collect(events):
    return [event async for event in events]


def success_events():
    return [
        RuntimeEvent(EventType.RUN_STARTED, "ignored"),
        RuntimeEvent(EventType.TASK_STARTED, "ignored", "T1", {"tool_name": "get_weather"}),
        RuntimeEvent(
            EventType.TASK_SUCCESS, "ignored", "T1",
            {"tool_name": "get_weather", "result": "波尔多-晴"},
        ),
        RuntimeEvent(
            EventType.TASK_SUCCESS, "ignored", "T2",
            {"tool_name": "get_current_time", "result": "2026-10-26 22:00:00"},
        ),
        RuntimeEvent(
            EventType.TASK_SUCCESS, "ignored", "T3",
            {"tool_name": "brewing", "result": "适合酿酒"},
        ),
        RuntimeEvent(EventType.RUN_FINISHED, "ignored"),
    ]


def test_run_emits_plan_execute_and_answer():
    plan = make_plan()
    executor = FakeExecutor(success_events())
    llm = FakeLLM([FakeMessage(content="结论：适合酿酒")])
    runner = PlanRunner(FakePlanner(plan), executor, llm, "系统提示")

    events = asyncio.run(collect(runner.run("今天波尔多适合酿酒吗？", run_id="run-1")))

    assert [e.type for e in events] == [
        EventType.RUN_STARTED,
        EventType.PLAN_STARTED,
        EventType.PLAN_READY,
        EventType.TASK_STARTED,
        EventType.TASK_SUCCESS,
        EventType.TASK_SUCCESS,
        EventType.TASK_SUCCESS,
        EventType.FINAL_ANSWER,
        EventType.RUN_FINISHED,
    ]
    assert {e.run_id for e in events} == {"run-1"}
    assert executor.calls == [(plan, "run-1")]
    assert events[2].data["task_count"] == 3
    assert events[-2].data["content"] == "结论：适合酿酒"
    assert llm.calls[0]["tools"] is None


def test_results_are_passed_to_answer_llm():
    llm = FakeLLM([FakeMessage(content="结论")])
    runner = PlanRunner(FakePlanner(make_plan()), FakeExecutor(success_events()), llm, "系统提示")

    asyncio.run(collect(runner.run("今天适合酿酒吗？", run_id="run-1")))

    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "波尔多-晴" in prompt
    assert "适合酿酒" in prompt
    assert "T3" in prompt


def test_plan_failure_stops_before_execution():
    executor = FakeExecutor([])
    llm = FakeLLM([])
    runner = PlanRunner(
        FakePlanner(error=PlanGenerationError("模型没给出合法计划")),
        executor,
        llm,
        "系统提示",
    )

    events = asyncio.run(collect(runner.run("今天适合酿酒吗？", run_id="run-bad")))

    assert [e.type for e in events] == [
        EventType.RUN_STARTED,
        EventType.PLAN_STARTED,
        EventType.PLAN_FAILED,
        EventType.RUN_FINISHED,
    ]
    assert "模型没给出合法计划" in events[2].data["error"]
    assert executor.calls == []
    assert llm.calls == []


def test_failed_and_blocked_results_are_passed_to_llm():
    events_in = [
        RuntimeEvent(EventType.RUN_STARTED, "ignored"),
        RuntimeEvent(EventType.TASK_FAILED, "ignored", "T1",
                     {"tool_name": "get_weather", "error": "boom"}),
        RuntimeEvent(EventType.TASK_BLOCKED, "ignored", "T3",
                     {"tool_name": "brewing", "reason": "Dependency 'T1' blocked"}),
        RuntimeEvent(EventType.RUN_FINISHED, "ignored"),
    ]
    llm = FakeLLM([FakeMessage(content="失败原因")])
    runner = PlanRunner(FakePlanner(make_plan()), FakeExecutor(events_in), llm, "系统提示")

    asyncio.run(collect(runner.run("今天适合酿酒吗？", run_id="run-2")))

    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "boom" in prompt
    assert "blocked" in prompt


def test_run_id_is_generated_when_absent():
    llm = FakeLLM([FakeMessage(content="结论")])
    runner = PlanRunner(FakePlanner(make_plan()), FakeExecutor(success_events()), llm, "系统提示")

    events = asyncio.run(collect(runner.run("今天适合酿酒吗？")))

    run_ids = {e.run_id for e in events}
    assert len(run_ids) == 1
    assert next(iter(run_ids))


def test_executor_error_ends_run():
    llm = FakeLLM([])
    runner = PlanRunner(FakePlanner(make_plan()), BrokenExecutor(), llm, "系统提示")

    events = asyncio.run(collect(runner.run("今天适合酿酒吗？", run_id="run-3")))

    assert [e.type for e in events] == [
        EventType.RUN_STARTED,
        EventType.PLAN_STARTED,
        EventType.PLAN_READY,
        EventType.RUN_FINISHED,
    ]
    assert "executor boom" in events[-1].data["error"]
    assert llm.calls == []


async def get_weather(city: str) -> str:
    return f"{city}-晴"


async def get_current_time() -> str:
    return "2026-10-26 22:00:00"


async def brewing(weather: str, current_time: str) -> str:
    return f"{weather} / {current_time} / 适合酿酒"


def test_integration_with_real_scheduler():
    registry = ToolRegistry()
    registry.register(get_weather)
    registry.register(get_current_time)
    registry.register(brewing)

    executor = TaskPlanExecutor(
        scheduler=TaskScheduler(tool_runner=ToolRunner(registry=registry)),
        registry=registry,
    )
    llm = FakeLLM([FakeMessage(content="综合：适合酿酒")])
    runner = PlanRunner(FakePlanner(make_plan()), executor, llm, "系统提示")

    events = asyncio.run(collect(runner.run("今天波尔多适合酿酒吗？", run_id="run-it")))

    successes = [e.task_id for e in events if e.type == EventType.TASK_SUCCESS]
    assert set(successes) == {"T1", "T2", "T3"}
    assert successes[-1] == "T3"
    assert {e.run_id for e in events} == {"run-it"}

    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "波尔多-晴" in prompt
    assert "适合酿酒" in prompt

    assert events[-2].type == EventType.FINAL_ANSWER
    assert events[-1].type == EventType.RUN_FINISHED
```

### 6.2 这 7 个测试分别防什么

| 测试 | 证明 |
|---|---|
| `test_run_emits_plan_execute_and_answer` | 事件顺序、run_id、run 边界、结果交给 LLM |
| `test_results_are_passed_to_answer_llm` | 任务结果确实进了回答提示词 |
| `test_plan_failure_stops_before_execution` | 规划失败不执行、不回答 |
| `test_failed_and_blocked_results_are_passed_to_llm` | 失败/阻塞信息也能进回答 |
| `test_run_id_is_generated_when_absent` | 不传 run_id 时自动生成且全程一致 |
| `test_executor_error_ends_run` | 执行异常能收尾，不会挂住 |
| `test_integration_with_real_scheduler` | 真实 DAG + 真实调度器能跑通 |

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson06_plan_runner.py -q
```

期望 `7 passed`；全量：

```powershell
.venv\Scripts\python.exe -m pytest -q
```

期望 `28 passed`（21 + 7）。

---

## 7. 冒烟脚本

`scripts/lesson06_smoke.py`：

```python
import asyncio
import sys

from agent.runtime import AgentRuntime


async def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "今天波尔多适合酿酒吗？"
    runtime = AgentRuntime()

    async for event in runtime.run(question):
        print(event.type.value, event.task_id or "", event.data)


if __name__ == "__main__":
    asyncio.run(main())
```

运行：

```powershell
.venv\Scripts\python.exe -m scripts.lesson06_smoke "今天波尔多适合酿酒吗？"
```

期望看到（T1/T2 顺序可能互换）：

```text
run_started
plan_started
plan_ready {'task_count': 3, 'tasks': [...]}
task_started T1 {...}
task_started T2 {...}
task_success T2 {...}
task_success T1 {...}
task_started T3 {...}
task_success T3 {...}
final_answer 综合结果：...
run_finished
```

这次真实调用大约消耗 **2 次 LLM 请求**（1 次规划 + 1 次回答），比第 5 课多一次。

---

## 8. 前端适配（可选）

不做也能跑：未知事件会被忽略，任务和最终回答仍能显示。但规划阶段是空白的，失败时也不好看。

在 `static/index.html` 的 `handleEvent` 里，`task_started` 之前加：

```javascript
if (type === "plan_started") {
    addEvent("🧭", "正在规划任务", data.message || "");
    return;
}

if (type === "plan_ready") {
    const count = data.task_count || 0;
    const lines = (data.tasks || []).map(
        t => `${t.id} ${t.tool}` +
             (t.dependencies && t.dependencies.length
                ? " ← " + t.dependencies.join(", ")
                : "")
    );
    addEvent("🗺️", `计划已生成（${count} 个任务）`, lines.join("\n"));
    return;
}

if (type === "plan_failed") {
    addEvent("⚠️", "规划失败", data.error);
    answer.innerText = "规划失败：\n" + (data.error || "");
    answer.className = "";
    statusText.innerText = "规划失败";
    statusDot.classList.remove("running");
    sendBtn.disabled = false;
    input.disabled = false;
    return;
}
```

再把 `run_finished` 的处理改成区分成功/失败：

```javascript
if (type === "run_finished") {
    if (data.error) {
        statusText.innerText = "执行失败";
        sendBtn.disabled = false;
        input.disabled = false;
    } else {
        statusText.innerText = "执行完成";
    }
    statusDot.classList.remove("running");
    return;
}
```

---

## 9. 参考实现

### 9.1 `events/models.py`（只列出新增部分）

```python
class EventType(str, Enum):
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"

    PLAN_STARTED = "plan_started"
    PLAN_READY = "plan_ready"
    PLAN_FAILED = "plan_failed"

    MODEL_STARTED = "model_started"
    ...
```

### 9.2 `tasks/plan_executor.py`

```python
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
        TaskConverter.validate_tools(plan, self.registry)
        tasks = TaskConverter.convert(plan)

        async for event in self.scheduler.run(tasks, dag, run_id):
            yield event
```

### 9.3 `agent/plan_runner.py`

```python
"""把「规划 → 执行 → 回答」串成一次完整的 Agent 运行。"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator

from agent.llm import LLM
from agent.planner import PlanGenerationError, TaskPlanner
from events.models import EventType, RuntimeEvent
from tasks.plan_executor import TaskPlanExecutor

logger = logging.getLogger(__name__)


class PlanRunner:
    """执行一次「生成计划 → 执行 DAG → 生成回答」流程。

    Args:
        planner: 生成 TaskPlan。
        executor: 执行 TaskPlan。
        llm: 生成最终回答。
        system_prompt: 最终回答使用的人设与约束。
    """

    def __init__(
        self,
        planner: TaskPlanner,
        executor: TaskPlanExecutor,
        llm: LLM,
        system_prompt: str,
    ):
        self.planner = planner
        self.executor = executor
        self.llm = llm
        self.system_prompt = system_prompt

    async def run(self, user_input: str, run_id: str | None = None) -> AsyncIterator[RuntimeEvent]:
        """运行一次完整流程。

        Args:
            user_input: 用户请求。
            run_id: 可选的外部 run_id；不传则自动生成。

        Yields:
            RuntimeEvent：运行、规划、执行、回答四个阶段的事件。
        """
        run_id = run_id or str(uuid.uuid4())

        # 运行边界由 PlanRunner 负责，调度器的 run_* 事件在下面被过滤掉。
        yield RuntimeEvent(type=EventType.RUN_STARTED, run_id=run_id)
        yield RuntimeEvent(
            type=EventType.PLAN_STARTED,
            run_id=run_id,
            data={"message": "正在规划任务..."},
        )

        try:
            plan = await self.planner.plan(user_input)
        except PlanGenerationError as exc:
            yield RuntimeEvent(
                type=EventType.PLAN_FAILED,
                run_id=run_id,
                data={"error": str(exc)},
            )
            yield RuntimeEvent(
                type=EventType.RUN_FINISHED,
                run_id=run_id,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        yield RuntimeEvent(
            type=EventType.PLAN_READY,
            run_id=run_id,
            data={
                "task_count": len(plan.tasks),
                "tasks": [
                    {"id": t.id, "tool": t.tool, "dependencies": t.dependencies}
                    for t in plan.tasks
                ],
            },
        )

        results: dict[str, dict] = {}
        try:
            async for event in self.executor.execute(plan, run_id):
                # 调度器也会发 run_started/run_finished；一次运行只保留一个边界。
                if event.type in (EventType.RUN_STARTED, EventType.RUN_FINISHED):
                    continue
                if event.task_id is not None and event.type in (
                    EventType.TASK_SUCCESS,
                    EventType.TASK_FAILED,
                    EventType.TASK_BLOCKED,
                ):
                    results[event.task_id] = self._task_result(event)
                yield event
        except Exception as exc:
            logger.exception("执行计划失败")
            yield RuntimeEvent(
                type=EventType.RUN_FINISHED,
                run_id=run_id,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        try:
            content = await self._compose_answer(user_input, results)
        except Exception as exc:
            logger.exception("生成最终回答失败")
            yield RuntimeEvent(
                type=EventType.RUN_FINISHED,
                run_id=run_id,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        yield RuntimeEvent(
            type=EventType.FINAL_ANSWER,
            run_id=run_id,
            data={"content": content},
        )
        yield RuntimeEvent(type=EventType.RUN_FINISHED, run_id=run_id)

    @staticmethod
    def _task_result(event: RuntimeEvent) -> dict:
        """把任务事件整理成回答阶段使用的结构。"""
        if event.type == EventType.TASK_SUCCESS:
            return {
                "tool_name": event.data.get("tool_name"),
                "success": True,
                "result": event.data.get("result"),
            }
        error_key = "error" if event.type == EventType.TASK_FAILED else "reason"
        return {
            "tool_name": event.data.get("tool_name"),
            "success": False,
            "error": event.data.get(error_key),
        }

    async def _compose_answer(self, user_input: str, results: dict[str, dict]) -> str:
        """基于任务结果生成最终回答。"""
        result_text = json.dumps(results, ensure_ascii=False, indent=2, default=str)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
            {
                "role": "user",
                "content": (
                    "以下是任务执行结果（JSON）：\n"
                    f"{result_text}\n\n"
                    "请基于以上结果回答用户，不要编造未出现的信息。"
                ),
            },
        ]
        response = await self.llm.chat(messages=messages)
        return response.content or ""
```

### 9.4 `agent/runtime.py`

```python
import logging

from agent.llm import LLM
from agent.plan_runner import PlanRunner
from agent.planner import TaskPlanner
from tasks.plan_executor import TaskPlanExecutor
from tasks.scheduler import TaskScheduler
from tools.builtin import brewing, get_current_time, get_weather
from tools.registry import ToolRegistry
from tools.runner import ToolRunner

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是私人酒庄的葡萄酒酿酒大师。\n\n"
    "你可以使用提供的工具，不要自己编造工具结果。"
)


class AgentRuntime:
    """组装 LLM、工具、规划器与执行器，对外提供事件流。"""

    def __init__(self):
        # 1. 全局单例 LLM
        self.llm = LLM()

        # 2. 工具
        registry = ToolRegistry()
        registry.register(get_weather)
        registry.register(get_current_time)
        registry.register(brewing)

        # 3. 执行链：Runner → Scheduler → Executor
        tool_runner = ToolRunner(registry=registry)
        scheduler = TaskScheduler(tool_runner=tool_runner)
        executor = TaskPlanExecutor(scheduler=scheduler, registry=registry)

        # 4. 规划器 + 运行器
        planner = TaskPlanner(llm=self.llm, registry=registry)
        self.runner = PlanRunner(
            planner=planner,
            executor=executor,
            llm=self.llm,
            system_prompt=SYSTEM_PROMPT,
        )

        logger.info("AgentRuntime 初始化完成，模型: %s", self.llm.model)
        logger.info("已注册工具: %s", registry.list_tools())

    async def run(self, user_input: str):
        """接收用户输入，返回事件生成器。"""
        async for event in self.runner.run(user_input):
            yield event
```

### 9.5 `agent/__init__.py`

```python
from .llm import LLM
from .plan_runner import PlanRunner
from .planner import PlanGenerationError, TaskPlanner
from .runtime import AgentRuntime

__all__ = [
    "LLM",
    "PlanRunner",
    "PlanGenerationError",
    "TaskPlanner",
    "AgentRuntime",
]
```

### 9.6 删除旧文件

```powershell
git rm agent/model_tool_loop.py
```

---

## 10. 验收清单（DoD）

- [x] `EventType` 有 `plan_started / plan_ready / plan_failed`
- [x] `TaskPlanExecutor.execute(plan, run_id)` 不再自己生成 run_id
- [x] `PlanRunner` 只发一次 `run_started` 和一次 `run_finished`
- [ ] 事件顺序符合 3.4：成功路径 `final_answer` 在 `run_finished` 之前
- [ ] 规划失败：`plan_failed` + `run_finished(error)`，不执行、不回答
- [ ] 执行异常：能收尾，不挂住
- [ ] 失败/阻塞任务的结果进入回答提示词
- [x] `pytest tests/unit/test_lesson06_plan_runner.py -q` → 7 passed
- [x] 全量 `pytest -q` → 28 passed
- [x] 真实冒烟：`plan_ready` → 三个任务成功 → `final_answer`
- [x] 没有 import `model_tool_loop`
- [ ] `git diff --stat` 只包含本课预期文件

---

## 11. 常见坑

| 坑 | 现象 | 处理 |
|---|---|---|
| `FakePlanner` 属性名和 `plan` 方法重名 | `TypeError: 'TaskPlan' object is not callable` | 属性改叫 `plan_obj`（参考实现已避开） |
| 没过滤调度器的 run 事件 | 一次请求出现两个 `run_started` | 在 `PlanRunner` 里 `continue` 掉 `run_started/run_finished` |
| 伪造成 tool 消息回填 | 400：找不到对应 tool_calls | 用汇总 user 消息（方案 B） |
| `FakeExecutor.execute` 写成普通协程 | `TypeError: 'coroutine' object is not async iterable` | 里面要有 `yield`，才是异步生成器 |
| 忘记传 `run_id` 给 executor | 事件 run_id 不一致，前端无法归组 | `PlanRunner` 统一生成并透传 |
| 只收集 `task_success` | 失败/阻塞任务在提示词里缺失 | 同时收集 `task_failed` / `task_blocked` |
| 回答阶段又传 tools | 模型继续调工具，流程又回到「谁执行」 | `llm.chat(messages)` 不传 tools |
| 规划失败前端卡住 | 按钮一直禁用 | 补 `plan_failed` / `run_finished(error)` 处理 |
| 删了旧文件但 `__init__` 还在 import | `ImportError` | 同步更新 `agent/__init__.py` |
| 任务结果 JSON 太大 | 回答阶段 token 超预算 | 第 10 课做上下文预算；先限制结果摘要 |

---

## 12. 扩展与思考题

1. **多轮规划**：如果执行结果不足以回答，是否让模型基于结果再规划一轮？这会引入「循环终止条件」，是第 35 课反思重规划的前置。
2. **结果压缩**：任务结果里的长文本怎么截断/摘要？第 10 课会系统解决。
3. **并行回答**：执行完所有任务后，是否可以让多个回答候选并行生成再选一个？
4. **run 边界归属**：如果第 30 课引入事件总线，`run_started/run_finished` 应该由谁发？为什么现在的方案是「运行器负责」？
5. **零任务计划**：如果 planner 返回 `tasks: []`，当前流程会怎样？需要拒绝还是直接回答？第 7 课会给出「不需要规划」的路径。

---

## 13. 与第 7 课的衔接

第 7 课会加一个 `IntentRouter`：

```python
async def run(self, user_input: str):
    if await self.router.is_task(user_input):
        async for event in self.runner.run(user_input):
            yield event
    else:
        async for event in self.chat.run(user_input):
            yield event
```

`PlanRunner` 只负责「任务路径」，不要在里面塞闲聊逻辑；`ChatResponder` 负责「普通对话路径」。这样两条路径都能单独测试，第 7 课也只需要改 `AgentRuntime.run()`。
