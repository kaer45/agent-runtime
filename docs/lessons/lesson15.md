# 第 15 课：多会话状态隔离

> 模块三 · 上下文与状态（第 6/11 课）
> 前置：第 14 课（`StateStore` 已能把状态写进 SQLite）
> 下一课：第 16 课做 `StateService`，应用 delta、版本和冲突处理
> 预计用时：2-3 小时
> 本课分支：`dev`

---

## 0. 开工前检查

先确认第 14 课是干净的：

```powershell
git switch dev
git status --short
.venv\Scripts\python.exe -m pytest --ignore=tests/unit/test_lesson15_session_registry.py -q
```

期望：`150 passed`。

本课不新增依赖，继续使用 `asyncio` 和标准库 `sqlite3`。

> 本课测试是“先红后绿”：实现 `state/session.py` 之前，运行
> `pytest tests/unit/test_lesson15_session_registry.py -q` 会因
> `ModuleNotFoundError: No module named 'state.session'` 失败。

---

## 1. 本课目标

第 14 课已经能按 `session_id` 存取状态：

```python
store.save("session-a", state_a)
store.save("session-b", state_b)
```

但这还不够。真实请求是并发的：

```text
请求 A：读 session-1 -> 改预算 -> 写回
请求 B：读 session-1 -> 改目标 -> 写回
```

如果两个请求都先 `load` 再 `save`，后写的会覆盖先写的，产生**丢失更新**。

本课加一层 `SessionRegistry`：

```text
StateStore（持久化，按 session_id 分区）
        │
        ▼
SessionRegistry（隔离 + 每会话锁 + 原子更新）
├─ same session  -> 串行
├─ different session -> 互不阻塞
├─ load/save/delete
└─ update(mutate) 原子读-改-写
```

学完你应该能回答：

1. `StateStore` 已经按 `session_id` 分区了，为什么还需要 `SessionRegistry`？
2. 为什么不能用一个全局锁保护所有会话？
3. 为什么 `load()` 每次返回新对象，而不是缓存同一个 `SessionState`？
4. `asyncio.Lock` 能解决多进程/多 worker 的并发吗？
5. 为什么 `update(mutate)` 比“先 load、再 save”安全？
6. 为什么临界区里不应该调用 LLM 或做网络请求？

**本课产出**

| 交付物 | 说明 |
|---|---|
| `state/session.py` | `SessionRegistry` / `StateMutator` |
| `state/__init__.py` | 导出 `SessionRegistry` |
| `tests/unit/test_lesson15_session_registry.py` | 11 个测试用例 |
| `scripts/lesson15_smoke.py` | 两会话并发更新的冒烟脚本 |

**本课明确不做**

- 不做 delta 应用（第 16 课 `StateService`）
- 不做版本号/乐观锁/冲突合并（第 16 课）
- 不做跨进程/跨 worker 分布式锁（生产化阶段）
- 不做锁淘汰/LRU（先保证正确性）
- 不做状态渲染进 prompt（第 17 课）
- 不修改 `AgentRuntime`

---

## 2. 问题：SQLite 有事务，为什么还会丢更新

假设两个请求同时执行：

```python
state = store.load("session-1")     # 都读到 budget=500
state.preferences["budget"] = 800   # A
store.save("session-1", state)

state = store.load("session-1")     # B 读到的也是 500
state.preferences["budget"] = 1000  # B
store.save("session-1", state)
```

最终是 1000，A 的 800 被覆盖。

SQLite 的事务保证的是**单次事务内**的一致性；这里 A 和 B 是两次独立事务：

```text
A: BEGIN -> SELECT -> COMMIT
B: BEGIN -> SELECT -> COMMIT
```

`SELECT` 和 `UPDATE` 之间没有锁，读-改-写不是原子的。

解决方式：

| 方案 | 作用范围 | 问题 |
|---|---|---|
| 全局锁 | 所有会话 | 会话 B 被会话 A 阻塞，吞吐差 |
| 每会话锁 | 同一个 session | 单进程有效；多进程无效 |
| DB 行锁/版本号 | 跨进程 | 第 16 课再做 |

本课做每会话锁：**同一个 session 串行，不同 session 并行**。

---

## 3. 设计决策

### 3.1 `StateStore` 管持久化，`SessionRegistry` 管并发

职责分离：

```text
StateStore
├─ SQLite schema
├─ save / load / delete
└─ 单次读写事务

SessionRegistry
├─ session_id 校验
├─ 每会话 asyncio.Lock
├─ 隔离读写
└─ 原子 update
```

为什么不把锁塞进 `StateStore`：

- 存储层不应该知道事件循环/async；
- 同一个 `StateStore` 可能被同步脚本、测试、后台任务共用；
- 第 16 课 `StateService` 需要组合“锁 + load + apply + save”，锁是更上层的编排关注点。

### 3.2 每会话一把锁

```python
_locks: dict[str, asyncio.Lock]
```

- 同一个 `session_id` 永远拿到同一个 `asyncio.Lock`；
- 不同 `session_id` 拿到不同锁，互不阻塞；
- 锁的创建用 `_locks_guard` 保护，避免并发首次访问创建出两把锁。

为什么不用全局锁：

```text
session A: 慢操作 1s
session B: 被同一个全局锁挡住 1s
```

多会话场景下，全局锁把并发退化成串行；每会话锁只串行化真正共享同一状态的操作。

### 3.3 锁表只增不减

当前 `_locks` 不会删除：

- 删除 session 时如果立刻删锁，可能和正在等待/持有的任务产生两把锁；
- 锁对象很小，当前规模可以接受；
- 真正需要时再做引用计数/LRU 淘汰。

代价：大量一次性 session 会导致 `_locks` 增长。第 36 课多用户/后台任务时再处理。

### 3.4 `load()` 每次返回新对象，不共享缓存

`SessionRegistry.load()` 直接从 `StateStore` 读，返回新的 `SessionState`：

```python
first = registry.load("session-a")
first.preferences["budget"] = 999
second = registry.load("session-a")   # 仍然是 500
```

为什么不做内存缓存：

- 缓存会让两个请求共享同一个可变对象，隔离性变差；
- 缓存和 SQLite 的一致性需要额外同步；
- 当前读性能不是瓶颈，先保证正确性。

以后要加缓存，也应该返回 `model_copy(deep=True)` 或不可变快照，而不是共享引用。

### 3.5 `update(mutate)` 提供原子读-改-写

手写：

```python
state = registry.load("s")
state.preferences["count"] += 1
registry.save("s", state)
```

仍然可能丢更新，因为 load/save 之间没有锁。

`update()` 把三步放进同一个临界区：

```python
async with registry.acquire("s"):
    state = load("s")
    state = mutate(state)
    save("s", state)
```

调用方只需要写：

```python
await registry.update(
    "s",
    lambda state: state.model_copy(
        update={"preferences": {**state.preferences, "count": 1}}
    ),
)
```

`mutate` 必须是同步函数；不要在里面 `await` 网络/LLM。

### 3.6 临界区要短

锁内只做：

- `load`
- 内存中的状态修改
- `save`

不要做：

- 调用 LLM
- HTTP 请求
- 长时间计算
- 等待用户输入

否则同一个 session 的其他请求会被长时间阻塞，甚至超时。

### 3.7 单进程锁的边界

`asyncio.Lock` 只在**当前进程、当前事件循环**内有效：

- 多个 uvicorn worker：每个进程有独立的锁，彼此不知道；
- 多台机器：更不可能共享；
- 同步多线程调用：`asyncio.Lock` 不适用。

跨进程/多 worker 需要：

- SQLite 事务 + 版本号/乐观锁（第 16 课）；
- 或 Redis/数据库分布式锁（生产化）。

本课先把单进程做对，并在文档里明确边界。

### 3.8 `session_id` 在 registry 里再校验一次

`StateStore` 已经校验，但 `lock()` 不经过 store，所以 registry 也要校验：

- 必须是字符串；
- strip；
- 空/全空白 → `ValueError`。

这样 `registry.lock("")` 不会创建一把“空 ID”的锁。

---

## 4. 接口与数据结构

### 4.1 `SessionRegistry`

```python
class SessionRegistry:
    def __init__(self, store: StateStore): ...

    def load(self, session_id: str) -> SessionState: ...
    def save(self, session_id: str, state: SessionState) -> None: ...
    def exists(self, session_id: str) -> bool: ...
    def delete(self, session_id: str) -> bool: ...

    async def lock(self, session_id: str) -> asyncio.Lock: ...

    @asynccontextmanager
    async def acquire(self, session_id: str) -> AsyncIterator[None]: ...

    async def update(
        self,
        session_id: str,
        mutate: Callable[[SessionState], SessionState],
    ) -> SessionState: ...
```

语义：

| 方法 | 行为 |
|---|---|
| `load()` | 不存在返回空 `SessionState()`，不是 `None` |
| `save()` | 直接委托 store；调用方负责加锁 |
| `exists()` | 委托 store |
| `delete()` | 委托 store；不删除锁 |
| `lock()` | 同一 session 返回同一个 `asyncio.Lock` |
| `acquire()` | `async with` 会话锁 |
| `update()` | 锁内 load → mutate → save，返回新状态 |

### 4.2 `StateMutator`

```python
StateMutator = Callable[[SessionState], SessionState]
```

约定：

- 同步函数；
- 必须返回 `SessionState`；
- 可以原地改，也可以返回 `model_copy`；
- 抛异常则不保存，锁自动释放。

### 4.3 锁的生命周期

```text
第一次 lock("a") -> 创建 Lock
后续 lock("a")   -> 返回同一对象
delete("a")      -> 只删状态，不删锁
```

本课不做锁淘汰；锁数量上限 = 进程内出现过的 session 数。

---

## 5. 实现步骤

### 第 1 步：建 `state/session.py`

```text
state/
├── __init__.py
├── models.py       # 第 12 课
├── extractor.py    # 第 13 课
├── store.py        # 第 14 课
└── session.py      # 本课
```

✅ 完成标志：`from state.session import SessionRegistry` 可用。

### 第 2 步：委托 load/save/exists/delete

- `load` 调 store，`None` 转成 `SessionState()`；
- 其他方法直接委托；
- 不做缓存。

✅ 完成标志：两个 session 的预算互不影响。

### 第 3 步：实现锁表

- `_locks: dict[str, asyncio.Lock]`；
- `_locks_guard: asyncio.Lock` 保护创建；
- `lock()` 返回同一个对象；
- `acquire()` 包成 async context manager。

✅ 完成标志：`await lock("a") is await lock("a")`。

### 第 4 步：实现 `update()`

- 先校验 session_id；
- `async with acquire`；
- `load` → `mutate` → 类型检查 → `save`；
- 返回更新后的状态。

✅ 完成标志：10 个并发 increment 最终是 10，不是 1。

### 第 5 步：更新 `state/__init__.py`

```python
from .session import SessionRegistry, StateMutator
from .store import StateStore, StateStoreError
```

✅ 完成标志：`from state import SessionRegistry` 可用。

### 第 6 步：测试和冒烟

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson15_session_registry.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m scripts.lesson15_smoke
```

期望：

```text
11 passed
161 passed
alice budget= 500 count= 5
bob budget= 800 count= 3
```

---

## 6. 测试代码

### 6.1 `tests/unit/test_lesson15_session_registry.py`

> 临时数据库同样放在工作区根目录，避免当前 DSH 沙箱对系统 `tmp_path` 的权限问题；
> fixture 结束时删除文件。

```python
import asyncio
import tempfile
from pathlib import Path

import pytest

from state.models import SessionState
from state.session import SessionRegistry
from state.store import StateStore


def sample_state(budget: int = 500) -> SessionState:
    return SessionState(
        preferences={"style": "dry", "budget": budget},
        goals=[{"text": "买两瓶干型酒"}],
        constraints=["预算不超过 500 元"],
        progress={"选酒": "in_progress"},
        facts={"产区": "波尔多"},
    )


@pytest.fixture
def registry() -> SessionRegistry:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson15-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    store = StateStore(db_path)
    store.initialize()

    try:
        yield SessionRegistry(store)
    finally:
        db_path.unlink(missing_ok=True)


def test_sessions_are_isolated(registry):
    registry.save("session-a", sample_state(budget=500))
    registry.save("session-b", sample_state(budget=800))

    assert registry.load("session-a").preferences["budget"] == 500
    assert registry.load("session-b").preferences["budget"] == 800


def test_missing_session_returns_empty_state(registry):
    assert registry.exists("missing") is False
    assert registry.load("missing") == SessionState()


def test_delete_removes_session_state(registry):
    registry.save("session-a", sample_state())

    assert registry.delete("session-a") is True
    assert registry.delete("session-a") is False
    assert registry.exists("session-a") is False
    assert registry.load("session-a") == SessionState()


def test_load_returns_independent_state_objects(registry):
    registry.save("session-a", sample_state(budget=500))

    first = registry.load("session-a")
    first.preferences["budget"] = 999

    second = registry.load("session-a")

    assert second.preferences["budget"] == 500


def test_same_session_lock_is_reused(registry):
    async def main():
        first = await registry.lock("session-a")
        second = await registry.lock("session-a")
        other = await registry.lock("session-b")

        assert first is second
        assert first is not other

    asyncio.run(main())


def test_same_session_updates_are_serialized(registry):
    async def increment() -> None:
        async with registry.acquire("session-a"):
            state = registry.load("session-a")
            count = state.preferences.get("count", 0)
            # 故意让出事件循环，放大“读-改-写”竞争。
            await asyncio.sleep(0.01)
            state.preferences["count"] = count + 1
            registry.save("session-a", state)

    async def main() -> None:
        await asyncio.gather(*(increment() for _ in range(10)))

    asyncio.run(main())

    assert registry.load("session-a").preferences["count"] == 10


def test_different_sessions_do_not_block_each_other(registry):
    async def main() -> None:
        a_started = asyncio.Event()
        release_a = asyncio.Event()

        async def hold_a() -> None:
            async with registry.acquire("session-a"):
                a_started.set()
                await release_a.wait()

        async def use_b() -> str:
            await a_started.wait()
            async with registry.acquire("session-b"):
                return "session-b"

        task = asyncio.create_task(hold_a())
        await a_started.wait()

        try:
            result = await asyncio.wait_for(use_b(), timeout=0.5)
            assert result == "session-b"
        finally:
            release_a.set()
            await task

    asyncio.run(main())


def test_update_runs_under_session_lock(registry):
    def mutate(state: SessionState) -> SessionState:
        state.preferences["style"] = "dry"
        return state

    result = asyncio.run(registry.update("session-a", mutate))

    assert result.preferences["style"] == "dry"
    assert registry.load("session-a").preferences["style"] == "dry"


def test_update_rejects_non_state_result(registry):
    with pytest.raises(TypeError, match="SessionState"):
        asyncio.run(
            registry.update("session-a", lambda state: {"preferences": {}})
        )

    assert registry.load("session-a") == SessionState()


@pytest.mark.parametrize("session_id", ["", "   "])
def test_blank_session_id_is_rejected(registry, session_id):
    with pytest.raises(ValueError, match="session_id"):
        registry.load(session_id)

    with pytest.raises(ValueError, match="session_id"):
        registry.save(session_id, sample_state())

    with pytest.raises(ValueError, match="session_id"):
        registry.exists(session_id)

    with pytest.raises(ValueError, match="session_id"):
        registry.delete(session_id)

    with pytest.raises(ValueError, match="session_id"):
        asyncio.run(registry.lock(session_id))

    with pytest.raises(ValueError, match="session_id"):
        asyncio.run(registry.update(session_id, lambda state: state))
```

### 6.2 这些测试分别防什么

| 测试 | 证明 |
|---|---|
| `test_sessions_are_isolated` | 两个 session 的预算/状态不串 |
| `test_missing_session_returns_empty_state` | 未初始化会话有确定行为 |
| `test_delete_removes_session_state` | 删除语义和返回值正确 |
| `test_load_returns_independent_state_objects` | 修改返回值不会污染存储 |
| `test_same_session_lock_is_reused` | 同一 session 不会创建两把锁 |
| `test_same_session_updates_are_serialized` | 并发读-改-写不丢更新 |
| `test_different_sessions_do_not_block_each_other` | 锁粒度是 session，不是全局 |
| `test_update_runs_under_session_lock` | 原子更新路径正常 |
| `test_update_rejects_non_state_result` | mutate 返回错类型不会写库 |
| `test_blank_session_id_is_rejected` | 空 ID 不会创建锁/状态（2 个参数） |

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson15_session_registry.py -q
```

期望 `11 passed`；全量期望 `161 passed`。

---

## 7. 冒烟脚本

`scripts/lesson15_smoke.py`：

```python
import asyncio
import tempfile
from pathlib import Path

from state.models import SessionState
from state.session import SessionRegistry
from state.store import StateStore


async def bump(registry: SessionRegistry, session_id: str, times: int) -> None:
    for _ in range(times):
        async with registry.acquire(session_id):
            state = registry.load(session_id)
            count = state.preferences.get("count", 0)
            await asyncio.sleep(0.001)
            state.preferences["count"] = count + 1
            registry.save(session_id, state)


async def main() -> None:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson15-smoke-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    try:
        store = StateStore(db_path)
        store.initialize()
        registry = SessionRegistry(store)

        registry.save(
            "alice",
            SessionState(preferences={"budget": 500}, facts={"产区": "波尔多"}),
        )
        registry.save(
            "bob",
            SessionState(preferences={"budget": 800}, facts={"产区": "勃艮第"}),
        )

        await asyncio.gather(
            bump(registry, "alice", 5),
            bump(registry, "bob", 3),
        )

        for session_id in ("alice", "bob"):
            state = registry.load(session_id)
            print(
                session_id,
                "budget=", state.preferences["budget"],
                "count=", state.preferences["count"],
                "产区=", state.facts["产区"],
            )
    finally:
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
```

运行：

```powershell
.venv\Scripts\python.exe -m scripts.lesson15_smoke
```

期望：

```text
alice budget= 500 count= 5 产区= 波尔多
bob budget= 800 count= 3 产区= 勃艮第
```

`count` 正好等于调用次数，说明同会话的读-改-写没有丢更新；两个会话的预算/产区不同，说明隔离生效。

---

## 8. 参考实现

> 建议先自己写，再对照。重点是锁的创建和 `update()` 的边界。

<details>
<summary>展开 `state/session.py` 参考实现</summary>

```python
"""按 session_id 管理状态读写和并发锁。

本模块只解决单进程内的会话隔离与同会话串行化；
版本号、冲突合并和 delta 应用留给第 16 课的 ``StateService``。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from state.models import SessionState
from state.store import StateStore

StateMutator = Callable[[SessionState], SessionState]


class SessionRegistry:
    """多会话状态入口：隔离、按会话加锁、原子更新。

    Example:
        registry = SessionRegistry(store)
        state = registry.load("session-1")

        async with registry.acquire("session-1"):
            state = registry.load("session-1")
            ...
            registry.save("session-1", state)
    """

    def __init__(self, store: StateStore):
        self.store = store
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def load(self, session_id: str) -> SessionState:
        """读取会话状态；不存在时返回空状态。"""
        state = self.store.load(session_id)
        return state if state is not None else SessionState()

    def save(self, session_id: str, state: SessionState) -> None:
        """保存会话状态快照。"""
        self.store.save(session_id, state)

    def exists(self, session_id: str) -> bool:
        """会话状态是否存在。"""
        return self.store.exists(session_id)

    def delete(self, session_id: str) -> bool:
        """删除会话状态。"""
        return self.store.delete(session_id)

    async def lock(self, session_id: str) -> asyncio.Lock:
        """获取某个会话的锁；同一 session 永远返回同一个对象。"""
        session_id = self._normalize_session_id(session_id)

        async with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock

    @asynccontextmanager
    async def acquire(self, session_id: str) -> AsyncIterator[None]:
        """在会话锁内执行一段临界区。"""
        lock = await self.lock(session_id)
        async with lock:
            yield

    async def update(
        self,
        session_id: str,
        mutate: StateMutator,
    ) -> SessionState:
        """在会话锁内完成一次原子读-改-写。"""
        session_id = self._normalize_session_id(session_id)

        async with self.acquire(session_id):
            state = self.load(session_id)
            updated = mutate(state)

            if not isinstance(updated, SessionState):
                raise TypeError("mutate 必须返回 SessionState")

            self.save(session_id, updated)
            return updated

    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        if not isinstance(session_id, str):
            raise TypeError("session_id 必须是字符串")

        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id 不能为空")
        return session_id
```

</details>

### 8.1 `state/__init__.py`

```python
from .extractor import (
    STATE_TOOL_NAME,
    StateDelta,
    StateExtractionError,
    StateExtractor,
)
from .models import Goal, GoalStatus, SessionState
from .session import SessionRegistry, StateMutator
from .store import StateStore, StateStoreError

__all__ = [
    "Goal",
    "GoalStatus",
    "SessionState",
    "STATE_TOOL_NAME",
    "StateDelta",
    "StateExtractionError",
    "StateExtractor",
    "StateMutator",
    "SessionRegistry",
    "StateStore",
    "StateStoreError",
]
```

---

## 9. 验收清单（DoD）

- [ ] `state/session.py` 存在，不依赖网络和第三方库
- [ ] 两个 session 的状态互不影响
- [ ] 未初始化 session 返回空 `SessionState`
- [ ] `load()` 返回独立对象，修改返回值不污染存储
- [ ] 同一 session 的 `lock()` 返回同一个对象
- [ ] 不同 session 的锁不同，慢操作不互相阻塞
- [ ] 10 个并发 increment 最终计数为 10
- [ ] `update()` 在锁内完成 load → mutate → save
- [ ] `mutate` 返回非 `SessionState` 时不写库
- [ ] 空 `session_id` 在 load/save/exists/delete/lock/update 都被拒绝
- [ ] `pytest tests/unit/test_lesson15_session_registry.py -q` → 11 passed
- [ ] 全量 `pytest -q` → 161 passed
- [ ] 冒烟脚本输出 alice count=5、bob count=3

---

## 10. 常见坑

| 坑 | 现象 | 处理 |
|---|---|---|
| 用全局锁 | 会话 B 被会话 A 的慢操作阻塞 | 锁按 `session_id` 分片 |
| 不加锁直接 load→save | 并发时后写覆盖先写 | 用 `acquire` 或 `update` |
| 在锁内调 LLM/HTTP | 同会话请求长时间排队 | 临界区只放状态读写 |
| 嵌套 `acquire` | `asyncio.Lock` 不可重入，死锁 | 不要在 `update`/锁内再 `acquire` 同一会话 |
| 把缓存对象直接返回 | 两个请求共享同一可变状态 | 每次 load 新对象，或返回 deep copy |
| 删除 session 时删锁 | 等待中的任务可能拿到新锁 | 本课不删锁；以后用引用计数 |
| 以为 `asyncio.Lock` 跨进程有效 | 多 worker 下仍然丢更新 | 第 16 课用版本号/DB 锁 |
| `lock()` 没做 ID 校验 | 空 ID 的锁污染锁表 | registry 自己也校验 |
| 锁表无限增长 | 一次性 session 很多时内存上涨 | 后续做 LRU/引用计数 |

---

## 11. 扩展与思考题

1. **锁淘汰**：怎么在不破坏等待者的情况下安全删除锁？引用计数、弱引用、还是定时清理？
2. **多进程**：两个 uvicorn worker 同时写同一个 session，`asyncio.Lock` 为什么无效？版本号怎么帮忙？
3. **读写锁**：多个读请求能否并行？Python 标准库没有现成的 async RWLock，值得自己实现吗？
4. **会话级任务队列**：把同一个 session 的请求排队串行执行，和锁相比有什么优缺点？
5. **缓存**：如果在 registry 里缓存 `SessionState`，如何保证 TTL、失效和多实例一致性？
6. **锁超时**：`asyncio.wait_for(lock.acquire(), timeout=...)` 超时后怎么处理请求？
7. **可观测**：如何记录锁等待时间、每个 session 的并发度、锁表大小？
8. **公平性**：`asyncio.Lock` 的等待队列是否公平？高并发下会不会有请求饿死？

---

## 12. 与第 16 课的衔接

本课解决的是：

```text
同一个 session 的读-改-写不要丢更新
不同 session 不要互相串/互相阻塞
```

但还没有回答：

```text
StateDelta 怎么应用到 SessionState？
目标完成/删除怎么校验？
版本冲突怎么办？
```

第 16 课做 `StateService`：

```text
StateService
├─ load(state) + lock
├─ apply(StateDelta)
├─ 语义校验（目标是否存在、状态转换）
├─ version / 冲突处理
└─ save
```

一句话：

> **第 14 课把状态存起来，第 15 课把会话隔离开，第 16 课才把 delta 安全地应用进去。**
