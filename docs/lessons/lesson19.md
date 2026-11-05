# 第 19 课：状态更新接入对话流程

> 模块三 · 上下文与状态（第 10/11 课）
> 前置：第 18 课（`ContextBuilder` 已能组装上下文）
> 下一课：第 20 课统一历史、摘要与状态快照的持久化
> 预计用时：2-3 小时
> 本课分支：`dev`

---

## 0. 开工前检查

先确认第 18 课是干净的：

```powershell
git switch dev
git status --short
.venv\Scripts\python.exe -m pytest --ignore=tests/unit/test_lesson19_turn_state_updater.py -q
```

期望：`204 passed`。

> 本课测试是“先红后绿”：实现 `state/updater.py` 之前，
> `pytest tests/unit/test_lesson19_turn_state_updater.py -q` 会因
> `ModuleNotFoundError: No module named 'state.updater'` 失败。

---

## 1. 本课目标

第 18 课已经能把“当前状态 + 摘要 + 历史 + 当前问题”组装成 messages，但状态还是只读的：

```text
用户说了一句话
      ↓
ContextBuilder 把旧状态放进 prompt
      ↓
模型回答
      ↓
??? 这一轮产生的新偏好、新目标，谁来写回？
```

本课做 `TurnStateUpdater`：

```text
本轮 messages
      ↓
StateExtractor.extract(current_state, messages)
      ↓
StateDelta
      ↓
StateService.apply(..., expected_version)
      ↓
新的 StateSnapshot
```

同时解决四件事：

1. **回合边界**：只提取本轮新增消息，不重复提取完整历史；
2. **非阻塞**：回答结束后可以后台更新，不阻塞用户；
3. **顺序**：同一 session 的提取 + 应用必须串行；
4. **幂等**：同一个 `turn_id` 重复提交，只应用一次。

学完你应该能回答：

1. 为什么不能把完整历史传给 `StateExtractor`？
2. 为什么 `StateExtractor` 失败不能静默返回空 delta？
3. “异步更新”为什么不等于 `asyncio.create_task()` 之后不管？
4. 为什么同一 session 的更新必须串行？
5. 幂等键为什么是 `(session_id, turn_id)`，而不是只靠 delta 内容？
6. 什么是“读己之写”，下一轮读状态前为什么要 `wait()`？
7. 为什么 `TurnStateUpdater` 不能直接复用 `SessionRegistry.acquire()` 再加锁？

**本课产出**

| 交付物 | 说明 |
|---|---|
| `state/updater.py` | `TurnStateUpdater` / `StateUpdateResult` |
| `state/__init__.py` | 导出更新器类型 |
| `tests/unit/test_lesson19_turn_state_updater.py` | 13 个测试用例 |
| `scripts/lesson19_smoke.py` | 不调用模型的状态更新冒烟脚本 |

**本课明确不做**

- 不修改 `AgentRuntime` / API（还没有 session 和历史存储，见第 20 课）
- 不做历史消息持久化（第 20 课）
- 不做跨进程的持久化幂等表（第 20 课及之后）
- 不做状态更新事件推送（第 30 课）
- 不做失败重试队列/退避（第 23 课）
- 不做任务执行过程中的中间状态更新

---

## 2. 问题：状态提取已经有了，但没人调用

当前 `StateExtractor` 的契约是：

```python
delta = await extractor.extract(state, latest_messages)
```

它只负责“提议”，不负责写：

- 空 delta 表示“本轮没有变化”；
- 提取失败抛 `StateExtractionError`；
- 不修改 `SessionState`；
- 不知道版本，也不知道幂等。

`StateService.apply()` 负责写：

```python
snapshot = await service.apply(
    session_id,
    delta,
    expected_version=snapshot.version,
)
```

但直接在每个 responder 里写这段代码，会遇到：

| 问题 | 后果 |
|---|---|
| 把完整历史传给提取器 | 旧目标/偏好被反复提取 |
| 回答后 `await` 提取 | 用户多等一次 LLM 调用 |
| `create_task()` 后不管 | 异常丢失、下一轮读到旧状态 |
| 同一 session 并发更新 | 后写覆盖先写，或版本冲突 |
| 重试同一轮 | 目标/约束重复，版本无意义地增加 |
| 不同 session 共用一个锁 | 所有会话被串行化，吞吐下降 |

所以需要一个专门的协调层。

---

## 3. 设计决策

### 3.1 回合边界：只传本轮新增消息

“一轮”指从用户发出当前问题，到助手给出最终回答之间的消息：

```python
latest_messages = [
    {"role": "user", "content": user_input},
    {"role": "assistant", "content": answer},
]
```

如果本轮有工具调用，可以包含：

```text
user
assistant(tool_calls)
tool
...
assistant(final answer)
```

但不要传完整历史。

原因：

- 当前状态已经承载了历史结论；
- 旧消息里的目标会被重复 `goals_add`；
- 用户改过预算时，旧值可能和新值一起被提取；
- 提取成本随历史长度增长；
- 旧消息里的指令可能诱导提取器。

`ContextBuilder` 负责“把历史给回答模型看”，`TurnStateUpdater` 负责“只把本轮变化写回状态”。两者输入不同，不要混用。

### 3.2 提取失败不能静默变成空 delta

`StateExtractor` 的设计是 fail fast：

```python
try:
    delta = await extractor.extract(state, messages)
except StateExtractionError as exc:
    ...
```

如果捕获后返回 `StateDelta()`：

- 调用方无法区分“这一轮确实没变化”和“提取失败了”；
- 状态会悄悄变旧；
- 下一轮模型会基于旧状态回答，而且没有任何告警。

`TurnStateUpdater` 的处理是：

```text
提取失败 → StateUpdateResult(error=...)
```

不写状态、不缓存失败、允许同一 `turn_id` 重试。

### 3.3 非阻塞：`schedule()` + `wait()`

`update()` 是 async，但调用方 `await` 它时仍然会等提取完成：

```python
result = await updater.update(...)   # 阻塞到提取 + 应用完成
```

如果回答已经发给用户，状态更新可以后台做：

```python
task = updater.schedule(...)         # 立即返回 Task
```

但“后台”不等于“fire-and-forget”。必须配套：

1. **保存 Task 引用**：否则可能被 GC，或者异常无人接收；
2. **捕获异常**：避免 `Task exception was never retrieved`；
3. **同一 session 串行**：不能两个后台任务同时读改写；
4. **下一轮读之前等待**：读己之写；
5. **关闭时 drain**：避免进程退出丢任务。

本课的 `TurnStateUpdater` 同时提供：

```python
await updater.wait(session_id)   # 等这个会话当前挂起的任务
await updater.drain()            # 等所有会话
```

下一轮开始时的顺序应该是：

```python
await updater.wait(session_id)
snapshot = await state_service.get(session_id)
built = context_builder.build(..., state=snapshot.state)
```

否则可能读到上一轮更新前的状态。

### 3.4 同一 session 串行，不同 session 并行

更新流程有两个阶段：

```text
提取（调用 LLM，慢）
应用（写 SQLite，快）
```

如果两个更新同时跑：

```text
turn-1 读取 state version=1
turn-2 读取 state version=1
turn-1 提取并 apply → version=2
turn-2 提取并 apply → version 冲突，或基于旧状态覆盖
```

所以 `TurnStateUpdater` 为每个 `session_id` 维护一把锁，把“读取 → 提取 → 应用”整体串行化。

不同 session 用不同锁，可以并行。

#### 锁保护什么，不保护什么

这里很容易误解：

```text
TurnStateUpdater 的 session 锁
    保护：update() 内部的 读取 → 提取 → 应用
    不保护：下一轮在 updater 外部调用 state_service.get()
```

`state_service.get()` 并不知道 `TurnStateUpdater` 里有没有后台任务，它只读数据库。所以下一轮如果直接 `get()`，即使上一轮的后台更新正在跑，也可能读到旧版本。

时间线：

```text
T1 回答完成
  schedule(turn-1)
    └─ [updater 锁] 读 v1 → 提取（LLM，慢）→ 应用 v2 → 释放锁

T2 用户很快发来下一句
  情况 A：不 wait
    get() → 读到 v1（旧状态）
    build() → 用旧状态回答
    schedule(turn-2)
      └─ 等 updater 锁 → 读 v2 → 提取 → 应用 v3

  情况 B：先 wait
    await updater.wait(session_id)
      └─ 等 turn-1 完成
    get() → 读到 v2
    build() → 用新状态回答
```

所以答案是：

> **如果使用 `schedule()` 做后台更新，下一轮读取状态前必须 `await updater.wait(session_id)`。**
> 这个等待不是自动发生的，是调用方的契约。

推荐的回合入口：

```python
async def begin_turn(session_id: str):
    await updater.wait(session_id)          # 先等上一轮后台更新
    snapshot = await state_service.get(session_id)
    return snapshot
```

如果这一轮是直接 `await updater.update(...)`，更新在返回前已经完成，就不需要再 `wait()`。

`wait()` 只等待“调用它时已经存在的任务”。如果调用之后又有新的 `schedule()`，它不会预知。正常编排中，回合入口是串行的：先等旧任务，再处理新回合，所以这个边界是够用的；更严格的做法是再加一把“回合锁”，把 `wait → get → build` 整体串行化。

**注意**：不要这样写：

```python
async with self.service.registry.acquire(session_id):
    snapshot = await self.service.get(session_id)   # get 内部又 acquire 同一把锁
```

`asyncio.Lock` 不可重入，会死锁。`TurnStateUpdater` 用自己的锁，`StateService` 内部再用它自己的锁。

### 3.5 幂等键：`(session_id, turn_id)`

只用 delta 内容做幂等不够：

- 两个不同的轮次可能产生相同 delta；
- 模型重试可能产生略有不同的 delta；
- 空 delta 也需要知道“这一轮已经处理过”。

所以用调用方提供的 `turn_id`：

```python
await updater.update(
    session_id="session-1",
    turn_id="turn-2026-11-04-001",
    messages=latest_messages,
)
```

规则：

| 情况 | 行为 |
|---|---|
| 第一次成功 | 正常提取 + 应用，缓存结果 |
| 同一 `(session_id, turn_id)` 再次调用 | 返回缓存结果，`duplicate=True` |
| 第一次是空 delta | 缓存空结果，重复调用不再提取 |
| 第一次失败 | 不缓存，允许重试 |
| 同一 `turn_id` 但不同 `session_id` | 视为不同键，互不影响 |

本课的缓存是**进程内**的：

```python
self._results: dict[tuple[str, str], StateUpdateResult]
```

进程重启后缓存丢失，持久化幂等需要第 20 课的表或独立幂等存储。

### 3.6 `expected_version`：防止基于旧状态写回

调用方通常从上下文构建阶段拿到一个快照：

```python
snapshot = await state_service.get(session_id)
built = context_builder.build(..., state=snapshot.state)
...
await updater.update(
    session_id,
    turn_id,
    messages,
    expected_version=snapshot.version,
)
```

`TurnStateUpdater` 会：

1. 读取当前最新快照；
2. 如果 `expected_version` 不匹配，直接返回失败，**不调用提取器**；
3. 匹配时，用当前快照的版本作为 `StateService.apply()` 的 `expected_version`。

这样避免“用旧状态提取的 delta，悄悄应用到新状态”。

失败结果不缓存，调用方可以重新读取状态后再试。

### 3.7 失败隔离：返回结果，不炸主回答

`TurnStateUpdater.update()` 对已知业务错误返回 `StateUpdateResult`：

- `StateExtractionError`
- `StateApplyError`
- `StateStoreError`（包括版本冲突）

不抛出，是为了让调用方可以：

```python
result = await updater.update(...)
if result.failed:
    logger.warning("状态更新失败: %s", result.error)
# 用户回答仍然正常返回
```

未知异常（编程错误）仍然向上抛；后台任务的回调会记录它。

这符合第 13 课的结论：

> fail fast 是“不吞错”，非阻塞是“调用方的策略”。

### 3.8 空 delta 不增加版本

如果提取器返回空 delta：

```python
result = StateUpdateResult(
    turn_id=turn_id,
    delta=delta,
    snapshot=snapshot,
    applied=False,
)
```

不调用 `StateService.apply()`，所以：

- 不创建数据库行；
- 不增加 version；
- 但结果会被缓存，重复同一轮不会再次调用 LLM。

---

## 4. API 契约

### 4.1 `TurnStateUpdater`

```python
class TurnStateUpdater:
    def __init__(
        self,
        extractor: StateExtractor,
        service: StateService,
    ): ...

    async def update(
        self,
        session_id: str,
        turn_id: str,
        messages: Sequence[dict],
        *,
        expected_version: int | None = None,
    ) -> StateUpdateResult: ...

    def schedule(
        self,
        session_id: str,
        turn_id: str,
        messages: Sequence[dict],
        *,
        expected_version: int | None = None,
    ) -> asyncio.Task[StateUpdateResult]: ...

    async def wait(self, session_id: str) -> None: ...

    async def drain(self) -> None: ...
```

### 4.2 `StateUpdateResult`

```python
@dataclass(frozen=True)
class StateUpdateResult:
    turn_id: str
    delta: StateDelta
    snapshot: StateSnapshot | None
    applied: bool
    duplicate: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool: ...
    @property
    def failed(self) -> bool: ...
```

| 字段/属性 | 含义 |
|---|---|
| `turn_id` | 本轮唯一 ID |
| `delta` | 提取出的增量；失败时为空 delta |
| `snapshot` | 应用后的快照；失败/无变化时是当前快照 |
| `applied` | 是否真的写入了新状态 |
| `duplicate` | 是否命中了幂等缓存 |
| `error` | 失败原因；成功为 `None` |
| `ok` | `error is None` |
| `failed` | `error is not None` |

注意 `duplicate=True` 时，`applied` 保留第一次的结果。它表示“这一轮曾经成功应用过”，不是“这次又应用了一次”。

---

## 5. 实现步骤

### 第 1 步：建 `state/updater.py`

```text
state/
├── models.py
├── extractor.py
├── store.py
├── session.py
├── service.py
└── updater.py    # 本课
```

✅ 完成标志：`from state.updater import TurnStateUpdater` 可用。

### 第 2 步：实现 `StateUpdateResult`

- frozen dataclass；
- 保存 turn、delta、snapshot、applied、duplicate、error；
- 用 property 提供 `ok` / `failed`。

### 第 3 步：实现每会话锁

- `dict[str, asyncio.Lock]`；
- 用 `_locks_guard` 保护字典创建；
- 不要用 `SessionRegistry.acquire()` 包住 `StateService.get/apply`，会重入死锁。

### 第 4 步：实现 `update()`

顺序：

1. 规范化 `session_id` / `turn_id`；
2. 复制并校验 `messages`；
3. 获取 session 锁；
4. 查幂等缓存；
5. 读取最新快照；
6. 检查 `expected_version`；
7. 提取 delta；
8. 空 delta → 返回 no-op；
9. 应用 delta；
10. 缓存并返回结果。

✅ 完成标志：同一轮重复调用不重复提取，不重复加版本。

### 第 5 步：实现 `schedule()` 和 `wait()`

- `schedule()` 创建 Task，保存引用，注册 done callback；
- callback 移除 Task、记录失败；
- `wait(session_id)` gather 当前挂起任务；
- `drain()` 循环等待所有会话任务。

✅ 完成标志：`schedule()` 立即返回，`wait()` 后能读到新状态。

### 第 6 步：导出并测试

在 `state/__init__.py` 增加：

```python
from .updater import StateUpdateResult, TurnStateUpdater
```

并加入 `__all__`。

---

## 6. 测试代码

### 6.1 `tests/unit/test_lesson19_turn_state_updater.py`

```python
import asyncio
import tempfile
from pathlib import Path

import pytest

from state.extractor import StateDelta, StateExtractionError
from state.models import SessionState
from state.service import StateService
from state.session import SessionRegistry
from state.store import StateStore
from state.updater import StateUpdateResult, TurnStateUpdater


@pytest.fixture
def service() -> StateService:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson19-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    store = StateStore(db_path)
    store.initialize()

    try:
        yield StateService(SessionRegistry(store))
    finally:
        db_path.unlink(missing_ok=True)


class FakeExtractor:
    def __init__(self, deltas=()):
        self.deltas = list(deltas)
        self.calls = 0
        self.states = []

    async def extract(self, state: SessionState, messages):
        self.calls += 1
        self.states.append((state, list(messages)))
        if not self.deltas:
            raise AssertionError("FakeExtractor 没有更多 delta")
        item = self.deltas.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class BlockingExtractor(FakeExtractor):
    def __init__(self, deltas=()):
        super().__init__(deltas)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def extract(self, state: SessionState, messages):
        self.started.set()
        await self.release.wait()
        return await super().extract(state, messages)


class ConcurrencyExtractor:
    def __init__(self, deltas):
        self.deltas = list(deltas)
        self.active = 0
        self.max_active = 0

    async def extract(self, state: SessionState, messages):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return self.deltas.pop(0)


def make_updater(service, extractor) -> TurnStateUpdater:
    return TurnStateUpdater(extractor, service)


def test_applies_delta_and_bumps_version(service):
    extractor = FakeExtractor([StateDelta(preferences_set={"style": "dry"})])
    updater = make_updater(service, extractor)

    result = asyncio.run(
        updater.update("session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}])
    )

    assert isinstance(result, StateUpdateResult)
    assert result.ok is True
    assert result.applied is True
    assert result.duplicate is False
    assert result.error is None
    assert result.snapshot is not None
    assert result.snapshot.version == 1
    assert result.snapshot.state.preferences == {"style": "dry"}
    assert result.delta.preferences_set == {"style": "dry"}
    assert extractor.calls == 1


def test_empty_delta_is_a_noop(service):
    extractor = FakeExtractor([StateDelta()])
    updater = make_updater(service, extractor)

    result = asyncio.run(
        updater.update("session-1", "turn-1", [{"role": "user", "content": "随便聊聊"}])
    )

    assert result.ok is True
    assert result.applied is False
    assert result.duplicate is False
    assert result.snapshot is not None
    assert result.snapshot.version == 0
    assert service.registry.exists("session-1") is False


def test_same_turn_id_is_idempotent(service):
    extractor = FakeExtractor([StateDelta(preferences_set={"style": "dry"})])
    updater = make_updater(service, extractor)

    async def main():
        first = await updater.update(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        second = await updater.update(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        return first, second

    first, second = asyncio.run(main())

    assert first.applied is True
    assert first.duplicate is False
    assert second.duplicate is True
    assert second.snapshot == first.snapshot
    assert extractor.calls == 1

    snapshot = asyncio.run(service.get("session-1"))
    assert snapshot.version == 1


def test_concurrent_same_turn_id_applies_once(service):
    extractor = FakeExtractor([StateDelta(progress_set={"选酒": "done"})])
    updater = make_updater(service, extractor)

    async def main():
        return await asyncio.gather(
            updater.update("session-1", "turn-1", [{"role": "user", "content": "完成"}]),
            updater.update("session-1", "turn-1", [{"role": "user", "content": "完成"}]),
        )

    results = asyncio.run(main())

    assert extractor.calls == 1
    assert sum(1 for result in results if result.duplicate) == 1
    assert sum(
        1 for result in results if result.applied and not result.duplicate
    ) == 1

    snapshot = asyncio.run(service.get("session-1"))
    assert snapshot.version == 1


def test_same_session_updates_are_serialized(service):
    extractor = ConcurrencyExtractor(
        [
            StateDelta(progress_set={"a": "1"}),
            StateDelta(progress_set={"b": "1"}),
        ]
    )
    updater = make_updater(service, extractor)

    async def main():
        return await asyncio.gather(
            updater.update("session-1", "turn-1", [{"role": "user", "content": "a"}]),
            updater.update("session-1", "turn-2", [{"role": "user", "content": "b"}]),
        )

    results = asyncio.run(main())

    assert all(result.applied for result in results)
    assert extractor.max_active == 1

    snapshot = asyncio.run(service.get("session-1"))
    assert snapshot.version == 2
    assert snapshot.state.progress == {"a": "1", "b": "1"}


def test_extraction_failure_is_returned_and_can_retry(service):
    extractor = FakeExtractor(
        [
            StateExtractionError("模型超时"),
            StateDelta(preferences_set={"style": "dry"}),
        ]
    )
    updater = make_updater(service, extractor)

    async def main():
        first = await updater.update(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        second = await updater.update(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        return first, second

    first, second = asyncio.run(main())

    assert first.failed is True
    assert "模型超时" in first.error
    assert first.snapshot is not None
    assert first.snapshot.version == 0
    assert second.applied is True
    assert extractor.calls == 2

    snapshot = asyncio.run(service.get("session-1"))
    assert snapshot.version == 1


def test_stale_expected_version_fails_without_extracting(service):
    extractor = FakeExtractor([StateDelta(preferences_set={"style": "dry"})])
    updater = make_updater(service, extractor)

    result = asyncio.run(
        updater.update(
            "session-1",
            "turn-1",
            [{"role": "user", "content": "我喜欢干型"}],
            expected_version=5,
        )
    )

    assert result.failed is True
    assert "状态版本冲突" in result.error
    assert result.snapshot is not None
    assert result.snapshot.version == 0
    assert extractor.calls == 0
    assert service.registry.exists("session-1") is False


def test_schedule_does_not_block(service):
    extractor = BlockingExtractor([StateDelta(preferences_set={"style": "dry"})])
    updater = make_updater(service, extractor)

    async def main():
        task = updater.schedule(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        await asyncio.wait_for(extractor.started.wait(), timeout=1)
        assert task.done() is False

        extractor.release.set()
        return await task

    result = asyncio.run(main())

    assert result.applied is True
    assert result.snapshot.version == 1


def test_wait_ensures_read_your_writes(service):
    extractor = BlockingExtractor([StateDelta(preferences_set={"style": "dry"})])
    updater = make_updater(service, extractor)

    async def main():
        task = updater.schedule(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        await asyncio.wait_for(extractor.started.wait(), timeout=1)

        async def release():
            extractor.release.set()

        asyncio.create_task(release())
        await updater.wait("session-1")
        assert task.done() is True

        return await service.get("session-1")

    snapshot = asyncio.run(main())

    assert snapshot.version == 1
    assert snapshot.state.preferences == {"style": "dry"}


def test_background_failure_is_captured(service):
    extractor = FakeExtractor([StateExtractionError("提取失败")])
    updater = make_updater(service, extractor)

    async def main():
        task = updater.schedule(
            "session-1", "turn-1", [{"role": "user", "content": "你好"}]
        )
        return await task

    result = asyncio.run(main())

    assert result.failed is True
    assert "提取失败" in result.error
    assert result.snapshot is not None
    assert result.snapshot.version == 0


def test_schedule_same_turn_id_still_applies_once(service):
    extractor = BlockingExtractor([StateDelta(preferences_set={"style": "dry"})])
    updater = make_updater(service, extractor)

    async def main():
        first = updater.schedule(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        second = updater.schedule(
            "session-1", "turn-1", [{"role": "user", "content": "我喜欢干型"}]
        )
        await asyncio.wait_for(extractor.started.wait(), timeout=1)
        extractor.release.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(main())

    assert extractor.calls == 1
    assert sum(1 for result in results if result.duplicate) == 1

    snapshot = asyncio.run(service.get("session-1"))
    assert snapshot.version == 1


@pytest.mark.parametrize(
    ("session_id", "turn_id"),
    [("", "turn-1"), ("session-1", "")],
)
def test_invalid_ids_are_rejected(service, session_id, turn_id):
    updater = make_updater(service, FakeExtractor())

    with pytest.raises(ValueError):
        asyncio.run(updater.update(session_id, turn_id, []))
```

---

### 6.2 每个测试防什么

| 测试 | 主要防止的回归 |
|---|---|
| `test_applies_delta_and_bumps_version` | updater 只提取不写回，delta 丢失或版本没增加 |
| `test_empty_delta_is_a_noop` | 空 delta 也创建数据库行、增加版本，制造无意义写入 |
| `test_same_turn_id_is_idempotent` | 重试同一轮时重复加目标/约束，版本被重复递增 |
| `test_concurrent_same_turn_id_applies_once` | 两个并发任务都穿过缓存检查，重复提取、重复应用 |
| `test_same_session_updates_are_serialized` | 同一 session 并发“读 → 提取 → 写”，后写覆盖先写 |
| `test_extraction_failure_is_returned_and_can_retry` | 提取失败被吞成空 delta，或失败结果被永久缓存，无法重试 |
| `test_stale_expected_version_fails_without_extracting` | 忽略 `expected_version`，把基于旧状态的 delta 写到新状态；或冲突时仍白调一次 LLM |
| `test_schedule_does_not_block` | `schedule()` 实际变成同步 await，用户回答被状态提取拖住 |
| `test_wait_ensures_read_your_writes` | 下一轮不等待后台更新，读到旧状态 |
| `test_background_failure_is_captured` | 后台 Task 异常无人接收，出现 “Task exception was never retrieved” |
| `test_schedule_same_turn_id_still_applies_once` | 两个 `schedule()` 并发注册同一轮，重复应用 |
| `test_invalid_ids_are_rejected` | 空 `session_id`/`turn_id` 进入锁和缓存，造成跨会话碰撞或脏缓存 |

注意：第 19 课这些测试不是纯单元测试。它们使用了真实的
`StateService`、`SessionRegistry` 和临时 SQLite 文件，更接近
“组件集成测试”。其中 `test_same_session_updates_are_serialized`
依赖 `asyncio.sleep(0.01)` 制造重叠窗口，在极慢或极快的 CI 上
有一定时序敏感性；生产级测试可以改用 `Event` 做确定性同步。

## 7. 冒烟脚本

### 7.1 `scripts/lesson19_smoke.py`

```python
import asyncio
import tempfile
from pathlib import Path

from state.extractor import StateDelta
from state.models import SessionState
from state.service import StateService
from state.session import SessionRegistry
from state.store import StateStore
from state.updater import TurnStateUpdater


class FakeExtractor:
    """按顺序返回预设 delta，不调用模型。"""

    def __init__(self, deltas):
        self.deltas = list(deltas)

    async def extract(self, state: SessionState, messages):
        await asyncio.sleep(0.01)
        return self.deltas.pop(0)


async def main() -> None:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson19-smoke-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    store = StateStore(db_path)
    store.initialize()
    service = StateService(SessionRegistry(store))
    extractor = FakeExtractor(
        [
            StateDelta(
                preferences_set={"style": "dry"},
                goals_add=["买两瓶干型酒"],
            ),
            StateDelta(preferences_set={"budget": 800}),
        ]
    )
    updater = TurnStateUpdater(extractor, service)

    try:
        messages = [
            {"role": "user", "content": "我喜欢干型酒，想买两瓶。"},
            {"role": "assistant", "content": "可以先看波尔多产区。"},
        ]

        first = await updater.update("demo", "turn-1", messages)
        duplicate = await updater.update("demo", "turn-1", messages)

        print("first applied:", first.applied)
        print("first version:", first.snapshot.version)
        print("duplicate:", duplicate.duplicate)
        print("version after duplicate:", duplicate.snapshot.version)

        task = updater.schedule("demo", "turn-2", messages)
        print("scheduled done:", task.done())
        await updater.wait("demo")

        snapshot = await service.get("demo")
        print("final version:", snapshot.version)
        print("preferences:", snapshot.state.preferences)
        print("goals:", [goal.text for goal in snapshot.state.goals])
    finally:
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
```

### 7.2 期望输出

```text
first applied: True
first version: 1
duplicate: True
version after duplicate: 1
scheduled done: False
final version: 2
preferences: {'style': 'dry', 'budget': 800}
goals: ['买两瓶干型酒']
```

### 7.3 真实 LLM 端到端冒烟

前面的测试都用 FakeExtractor，不访问网络。如果要用真实模型跑一遍
“组装上下文 → 回答 → 提取状态 → 应用状态”的完整链路，可以运行：

```powershell
# 先只跑一轮，控制调用成本
.venv\Scripts\python.exe -m scripts.real_llm_state_e2e --turns 1

# 跑完整三轮
.venv\Scripts\python.exe -m scripts.real_llm_state_e2e

# 等 TurnStateUpdater 实现后，改走 updater 路径
.venv\Scripts\python.exe -m scripts.real_llm_state_e2e --updater
```

脚本使用真实组件：

- `agent.llm.LLM`：真实模型调用；
- `ContextBuilder` + `TokenBudget`：真实上下文组装；
- `StateExtractor`：真实状态提取；
- `StateService` + `StateStore`：真实 SQLite 状态读写；
- `--updater`：第 19 课的 `TurnStateUpdater`。

它会打印每轮的：

```text
context: state_tokens=... summary_tokens=... original=... final=... dropped=...
assistant: ...
delta: {...}
state version: 0 -> 1
{...最终状态...}
```

注意：

- 这会真实调用模型，产生费用和延迟；
- 需要 `.env` 中配置 `LLM_API_KEY` / `LLM_BASE_URL`；
- 默认使用临时 SQLite，运行完删除；加 `--keep-db` 可保留检查；
- 使用 `--updater` 时，观察 delta 要看 `StateUpdateResult.delta`，不要再单独调用 `extractor.extract()`：updater 内部已经提取过一次，重复调用会多花一次 LLM，而且两次结果可能不同；
- 这是“组件级端到端”，还不是 `AgentRuntime` 级端到端：session API、历史持久化还没接。

---

## 8. 参考实现

<details>
<summary>展开 `state/updater.py`</summary>

```python
"""回合结束后提取并应用状态增量，处理幂等与后台执行。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Sequence

from state.extractor import StateDelta, StateExtractionError, StateExtractor
from state.models import SessionState
from state.service import StateApplyError, StateService
from state.store import StateSnapshot, StateStoreError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StateUpdateResult:
    turn_id: str
    delta: StateDelta
    snapshot: StateSnapshot | None
    applied: bool
    duplicate: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def failed(self) -> bool:
        return self.error is not None


class TurnStateUpdater:
    """把一轮对话的 StateDelta 提取并安全地应用到状态。

    - 同一 session 的提取 + 应用串行；
    - 同一 (session_id, turn_id) 只应用一次；
    - schedule() 提供非阻塞入口，wait() 用于读己之写。
    """

    def __init__(self, extractor: StateExtractor, service: StateService):
        self.extractor = extractor
        self.service = service
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._results: dict[tuple[str, str], StateUpdateResult] = {}
        self._tasks: dict[str, set[asyncio.Task]] = {}

    async def update(
        self,
        session_id: str,
        turn_id: str,
        messages: Sequence[dict],
        *,
        expected_version: int | None = None,
    ) -> StateUpdateResult:
        session_id = self._normalize_id(session_id, "session_id")
        turn_id = self._normalize_id(turn_id, "turn_id")
        messages = self._normalize_messages(messages)

        async with await self._lock_for(session_id):
            key = (session_id, turn_id)
            cached = self._results.get(key)
            if cached is not None:
                return replace(cached, duplicate=True)

            try:
                snapshot = await self.service.get(session_id)
            except StateStoreError as exc:
                return self._failure(turn_id, None, exc)

            if expected_version is not None and snapshot.version != expected_version:
                return StateUpdateResult(
                    turn_id=turn_id,
                    delta=StateDelta(),
                    snapshot=snapshot,
                    applied=False,
                    error=(
                        f"状态版本冲突：期望 {expected_version}，"
                        f"当前 {snapshot.version}"
                    ),
                )

            try:
                delta = await self.extractor.extract(snapshot.state, messages)
            except (StateExtractionError, StateStoreError) as exc:
                logger.warning("状态提取失败 turn=%s: %s", turn_id, exc)
                return self._failure(turn_id, snapshot, exc)

            if delta.is_empty():
                result = StateUpdateResult(
                    turn_id=turn_id,
                    delta=delta,
                    snapshot=snapshot,
                    applied=False,
                )
                self._results[key] = result
                return result

            try:
                updated = await self.service.apply(
                    session_id,
                    delta,
                    expected_version=snapshot.version,
                )
            except (StateApplyError, StateStoreError) as exc:
                logger.warning("状态应用失败 turn=%s: %s", turn_id, exc)
                return self._failure(turn_id, snapshot, exc)

            result = StateUpdateResult(
                turn_id=turn_id,
                delta=delta,
                snapshot=updated,
                applied=True,
            )
            self._results[key] = result
            return result

    def schedule(
        self,
        session_id: str,
        turn_id: str,
        messages: Sequence[dict],
        *,
        expected_version: int | None = None,
    ) -> asyncio.Task[StateUpdateResult]:
        session_id = self._normalize_id(session_id, "session_id")
        turn_id = self._normalize_id(turn_id, "turn_id")
        messages = self._normalize_messages(messages)
s
        task = asyncio.create_task(
            self.update(
                session_id,
                turn_id,
                messages,
                expected_version=expected_version,
            )
        )
        self._tasks.setdefault(session_id, set()).add(task)
        task.add_done_callback(
            lambda finished: self._on_task_done(session_id, finished)
        )
        return task

    async def wait(self, session_id: str) -> None:
        """等待某个会话当前挂起的后台更新完成。"""
        session_id = self._normalize_id(session_id, "session_id")
        tasks = list(self._tasks.get(session_id, ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def drain(self) -> None:
        """等待所有会话的后台更新完成，用于关闭/测试。"""
        while True:
            tasks = [
                task
                for pending in self._tasks.values()
                for task in pending
            ]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _lock_for(self, session_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    def _on_task_done(self, session_id: str, task: asyncio.Task) -> None:
        pending = self._tasks.get(session_id)
        if pending is not None:
            pending.discard(task)

        if task.cancelled():
            return

        try:
            result = task.result()
        except Exception:
            logger.exception("后台状态更新任务异常 session=%s", session_id)
            return

        if result.failed:
            logger.warning(
                "后台状态更新失败 session=%s turn=%s: %s",
                session_id,
                result.turn_id,
                result.error,
            )

    @staticmethod
    def _failure(
        turn_id: str,
        snapshot: StateSnapshot | None,
        exc: Exception,
    ) -> StateUpdateResult:
        return StateUpdateResult(
            turn_id=turn_id,
            delta=StateDelta(),
            snapshot=snapshot,
            applied=False,
            error=str(exc),
        )

    @staticmethod
    def _normalize_id(value: str, name: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{name} 必须是字符串")
        value = value.strip()
        if not value:
            raise ValueError(f"{name} 不能为空")
        return value

    @staticmethod
    def _normalize_messages(messages: Sequence[dict]) -> list[dict]:
        if isinstance(messages, (str, bytes)):
            raise TypeError("messages 必须是消息序列")
        try:
            return list(messages)
        except TypeError as exc:
            raise TypeError("messages 必须是消息序列") from exc
```

</details>

### 8.1 `state/__init__.py` 导出

```python
from .updater import StateUpdateResult, TurnStateUpdater
```

并加入 `__all__`。

---

## 9. 常见坑

### 9.1 把完整历史传给提取器

错误：

```python
delta = await extractor.extract(snapshot.state, all_history)
```

后果：旧目标被重复添加，旧预算和新预算冲突。只传本轮新增消息。

### 9.2 `create_task()` 后不保存引用

```python
asyncio.create_task(updater.update(...))
```

Task 可能被 GC，异常也无人接收。`schedule()` 内部保存引用，并在 done callback 中移除。

### 9.3 后台失败静默消失

如果 `schedule()` 不注册 callback，失败只会停留在 Task 里；没人 `await` 时，甚至可能没有日志。本课在 callback 中记录 `result.failed` 和未知异常。

### 9.4 同一 session 并发提取

不要并发执行：

```python
await asyncio.gather(
    update(session, "turn-1", ...),
    update(session, "turn-2", ...),
)
```

除非有会话锁。否则两个任务都基于旧版本提取，后写可能覆盖先写。

### 9.5 用 `SessionRegistry.acquire()` 包住 `StateService`

`StateService.get/apply` 内部会再次 `acquire` 同一把锁，`asyncio.Lock` 不可重入，会死锁。协调层用自己的锁。

### 9.6 认为内存幂等是永久的

`_results` 只在当前进程内。重启后同一个 `turn_id` 会再次提取、再次应用。持久化幂等需要数据库表或独立幂等存储，第 20 课处理。

### 9.7 复用 `turn_id` 但内容不同

同一 `(session_id, turn_id)` 命中缓存后，会直接返回旧结果，不会检查 `messages` 是否变化。调用方必须保证 `turn_id` 唯一标识一轮。

### 9.8 `expected_version` 用错

`expected_version` 应该来自“组装上下文时读到的快照版本”，不是随便传当前版本。传错会导致更新被拒绝，或者失去防止旧状态覆盖的意义。

---

## 10. 验收清单

- [ ] `state/updater.py` 存在，`TurnStateUpdater` / `StateUpdateResult` 可导入
- [ ] 同一 session 的更新串行
- [ ] 不同 session 不共用锁
- [ ] 同一 `(session_id, turn_id)` 只应用一次
- [ ] 空 delta 不增加版本
- [ ] 提取失败返回失败结果，不写状态，可重试
- [ ] `expected_version` 不匹配时不调用提取器
- [ ] `schedule()` 立即返回，`wait()` 后能读到新状态
- [ ] 后台任务异常被捕获/记录
- [ ] `state/__init__.py` 导出新类型

测试命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson19_turn_state_updater.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m scripts.lesson19_smoke
```

期望：

```text
13 passed
217 passed
```

---

## 11. 扩展与思考题

1. **持久化幂等**：如果把 `(session_id, turn_id)` 存进 SQLite，如何保证“状态写入”和“幂等记录”在同一个事务里？
2. **冲突重试**：`expected_version` 不匹配时，是直接失败，还是重新读取状态并重新提取？重新提取会不会产生和第一次不同的 delta？
3. **任务路径**：任务执行可能持续很久，回合边界怎么定义？工具结果算不算“本轮新增消息”？
4. **多进程**：进程内锁失效后，如何用数据库版本 + 幂等键保证同一轮只应用一次？
5. **缓存淘汰**：`_results` 无限增长会占内存，LRU 还是持久化？
6. **失败重试**：提取失败的 turn 要不要进入重试队列？重试几次？退避策略是什么？
7. **可观测性**：应该记录哪些指标？提取耗时、失败率、空 delta 率、版本冲突数？
8. **读己之写**：如果下一轮不想等后台更新，能否接受“最终一致”？需要向用户说明什么？
9. **取消**：用户取消回答后，已经 schedule 的状态更新要不要取消？
10. **安全**：如果本轮消息里包含 prompt injection，提取器失败/产生恶意 delta，StateService 的哪些校验能兜底？

---

## 12. 下一课

第 20 课把三条数据流统一起来：

```text
messages / RollingSummary / StateSnapshot
              ↓
        HistoryStore
              ↓
     按 session 持久化、恢复
```

届时会把：

- 历史消息追加；
- 摘要更新；
- 状态快照版本；
- `turn_id` 幂等记录；

放进同一套存储与恢复流程，并讨论“先写回答还是先写状态”“进程崩溃后如何恢复”。
