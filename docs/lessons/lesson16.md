# 第 16 课：状态变更闭环

> 模块三 · 上下文与状态（第 7/11 课）
> 前置：第 15 课（`SessionRegistry` 已能按会话加锁）
> 下一课：第 17 课把状态渲染进模型上下文
> 预计用时：2-3 小时
> 本课分支：`dev`

> **后续变更（2026-11-02）**：`Goal` 已增加 `id`，`goals_complete` / `goals_abandon` 已改为目标 ID；本文的测试代码和参考实现是第 16 课当时版本。最新设计见 [goal-id-design.md](goal-id-design.md)。

---

## 0. 开工前检查

先确认第 15 课是干净的：

```powershell
git switch dev
git status --short
.venv\Scripts\python.exe -m pytest --ignore=tests/unit/test_lesson16_state_service.py -q
```

期望：`161 passed`。

> 本课测试是“先红后绿”：实现 `state/service.py` 和 `StateStore` 的版本方法之前，
> 运行 `pytest tests/unit/test_lesson16_state_service.py -q` 会因
> `ModuleNotFoundError: No module named 'state.service'` 失败。

---

## 1. 本课目标

到这里，状态相关的三块能力已经有了：

```text
第 12 课：SessionState        状态长什么样
第 13 课：StateDelta          这一轮改了什么
第 14 课：StateStore          状态存在哪里
第 15 课：SessionRegistry     同一会话串行、不同会话隔离
```

但 `StateDelta` 还只是“变更建议”，没有真正应用到状态：

```text
StateDelta
   │
   ▼
StateService
├─ 读取当前状态 + 版本
├─ 应用 delta
├─ 语义校验（目标是否存在、状态转换是否合法）
├─ 版本检查（乐观锁）
└─ 保存新状态 + 新版本
```

学完你应该能回答：

1. 为什么 `StateDelta` 不能自己应用到 `SessionState`？
2. 版本号存在哪里？为什么不放进 `SessionState`？
3. `expected_version=0` 和 `expected_version=N` 分别代表什么？
4. 本地已有 `asyncio.Lock`，为什么还需要版本号？
5. 为什么重复添加同一个目标/约束要幂等，而完成不存在的目标要报错？
6. 空 delta 为什么不应该增加版本号？

**本课产出**

| 交付物 | 说明 |
|---|---|
| `state/service.py` | `StateService` / `StateApplyError` / `apply_delta` |
| `state/store.py` | 增加 `version` 列、`StateSnapshot`、`StateConflictError`、版本读写 |
| `state/session.py` | 增加 `load_snapshot` / `save_snapshot` 委托 |
| `tests/unit/test_lesson16_state_service.py` | 13 个测试用例 |
| `scripts/lesson16_smoke.py` | 应用 delta、版本冲突的冒烟脚本 |

**本课明确不做**

- 不做状态渲染进 prompt（第 17 课）
- 不做 `ContextBuilder`（第 18 课）
- 不做回合后自动提取/异步更新（第 19 课）
- 不做 delta 审计表、事件回放（第 19/20 课）
- 不做复杂冲突合并（字段级 merge/人工确认）
- 不做跨进程分布式锁

---

## 2. 问题：有锁了，为什么还需要版本

第 15 课的锁解决的是**单进程内**的竞争：

```text
同一个进程、同一个事件循环
T1 acquire("s") -> T2 acquire("s") 等待
```

但下面这些场景它管不了：

- 两个 uvicorn worker，各自有一把锁；
- 两个进程/两台机器同时写同一个 session；
- 客户端拿着旧状态提交修改；
- 提取器在后台跑，结果回来时状态已经被其他轮次改过。

所以需要数据库层的**乐观锁**：

```text
读取：state + version
修改：基于这个 version
保存：UPDATE ... WHERE version = ?
      rowcount = 0 -> 冲突
```

这就是 `expected_version` 的意义。

---

## 3. 设计决策

### 3.1 版本号放在存储层，不放进 `SessionState`

`SessionState` 仍然只表达业务事实：

```python
preferences / goals / constraints / progress / facts
```

版本号是存储/并发元数据：

```sql
ALTER TABLE session_states ADD COLUMN version INTEGER NOT NULL DEFAULT 0;
```

为什么不放进 `SessionState`：

- 领域状态不应该知道数据库版本；
- 同一个状态对象可以用于不同存储实现；
- 版本号变化不应该触发 `SessionState` 的业务校验；
- 第 17 课渲染状态时，也不希望把 `version` 混进用户偏好。

### 3.2 `StateSnapshot`：状态 + 版本

```python
@dataclass(frozen=True)
class StateSnapshot:
    state: SessionState
    version: int
```

`StateStore.load_snapshot()` 返回它：

```python
snapshot = store.load_snapshot("session-1")
# snapshot.state, snapshot.version
```

原来的 `load()` 保留，返回 `state` 或 `None`，兼容第 14/15 课。

### 3.3 版本语义

| 操作 | 版本变化 |
|---|---|
| 新会话第一次 `apply` | 0 → 1 |
| 已有会话继续 `apply` | N → N+1 |
| 空 delta | 不变 |
| 应用失败（目标不存在等） | 不变 |
| 版本冲突 | 不变 |

`StateService.get()` 对不存在的会话返回：

```python
StateSnapshot(state=SessionState(), version=0)
```

注意：这不会创建数据库行。

### 3.4 乐观锁的保存方式

`StateStore.save_snapshot()` 分三种情况：

```text
expected_version = None
    无条件 upsert，version + 1（给不关心冲突的调用方/兼容旧 API）

expected_version = 0
    创建：先尝试 UPDATE ... WHERE version = 0
    如果更新不到，再 INSERT version = 1
    如果 INSERT 撞主键 -> 冲突

expected_version = N > 0
    UPDATE ... SET version = version + 1
    WHERE session_id = ? AND version = N
    rowcount = 0 -> 冲突
```

为什么 `expected_version=0` 不直接 `INSERT`：

- 第 14/15 课创建的旧表可能没有 `version` 列；
- 迁移后旧行默认 `version=0`；
- 直接 `INSERT` 会主键冲突，无法把旧行升级到版本 1；
- 先 `UPDATE version=0` 可以兼容旧数据，再 `INSERT` 处理全新会话。

### 3.5 本地锁 + 数据库版本

两者职责不同：

```text
SessionRegistry 的 asyncio.Lock
    单进程内，避免同会话并发读-改-写交错

StateStore 的 version
    跨进程/跨 worker，检测“我读到的版本已经被别人改过”
```

流程：

```text
acquire(session_id)
  snapshot = load_snapshot()       # 拿到 version
  check expected_version
  apply_delta
  save_snapshot(expected_version=snapshot.version)
release
```

- 单进程并发：锁会把它们排队；
- 多进程并发：锁不知道对方，但数据库 `WHERE version = ?` 会让后写者冲突；
- 冲突后调用方可以重读、重试，或者报错让上层处理。

### 3.6 `apply_delta` 是纯函数

```python
def apply_delta(state: SessionState, delta: StateDelta) -> SessionState:
    ...
```

规则：

- 不修改传入的 `state`；
- `deepcopy` 偏好/事实，避免嵌套对象共享；
- goals 用 `model_copy(deep=True)`；
- 最后构造新的 `SessionState`，让 Pydantic 再校验一次。

这样 `StateService` 可以在保存前安全地计算新状态；失败时旧状态不变。

### 3.7 幂等与错误的边界

| 操作 | 语义 |
|---|---|
| `preferences_set` / `facts_set` / `progress_set` | upsert，重复应用结果相同 |
| `*_remove` 不存在的 key | no-op |
| `constraints_add` 已存在 | no-op（幂等） |
| `constraints_remove` 不存在 | no-op |
| `goals_add` 已存在 | no-op（幂等，避免重试重复） |
| `goals_complete` 不存在 | `StateApplyError` |
| `goals_complete` 已 done | no-op |
| `goals_complete` 已 abandoned | `StateApplyError` |
| `goals_abandon` 不存在 | `StateApplyError` |
| `goals_abandon` 已 abandoned | no-op |
| `goals_abandon` 已 done | `StateApplyError` |

为什么“删除不存在”是 no-op，而“完成不存在”是错误：

- 删除是集合操作，目标本来就不在集合里，结果已经满足；
- 完成目标是在做状态转换，目标不存在说明提取器/调用方语义错了，不能静默成功。

### 3.8 空 delta 不写库

`StateDelta.is_empty()` 为真时，`StateService.apply()` 直接返回当前快照，不调用 `save_snapshot`：

```text
没有变化 -> 不涨版本 -> 不写 SQLite
```

这避免“每轮对话都涨版本”，也让“这一轮没有状态变化”成为正常结果。

### 3.9 版本冲突的边界

本课处理：

- 同一进程并发：`SessionRegistry` 锁；
- 跨进程/旧客户端：`expected_version` + `WHERE version = ?`；
- 冲突：抛 `StateConflictError`，让调用方决定重试/合并/报错。

本课不处理：

- 字段级自动合并；
- 冲突时保留两人的修改；
- 冲突审计/人工确认。

这些可以在第 19/20/36 课继续做。

---

## 4. 接口与数据结构

### 4.1 `StateSnapshot`

```python
@dataclass(frozen=True)
class StateSnapshot:
    state: SessionState
    version: int
```

### 4.2 `StateStore` 新增

```python
def load_snapshot(self, session_id: str) -> StateSnapshot | None: ...
def save_snapshot(
    self,
    session_id: str,
    state: SessionState,
    expected_version: int | None = None,
) -> int: ...
```

`save()` 保留，内部转调 `save_snapshot(expected_version=None)`，返回新版本号（旧调用方可以忽略）。

### 4.3 `StateService`

```python
class StateService:
    def __init__(self, registry: SessionRegistry): ...

    async def get(self, session_id: str) -> StateSnapshot: ...

    async def apply(
        self,
        session_id: str,
        delta: StateDelta,
        *,
        expected_version: int | None = None,
    ) -> StateSnapshot: ...
```

### 4.4 `apply_delta`

```python
def apply_delta(state: SessionState, delta: StateDelta) -> SessionState: ...
```

### 4.5 错误类型

```python
class StateConflictError(StateStoreError):
    """期望版本和数据库当前版本不一致。"""

class StateApplyError(ValueError):
    """目标不存在，或目标状态转换非法。"""
```

---

## 5. 实现步骤

### 第 1 步：给 `StateStore` 加版本

- schema 增加 `version INTEGER NOT NULL DEFAULT 0`；
- `initialize()` 检查 `PRAGMA table_info`，旧表缺列就 `ALTER TABLE`；
- 增加 `StateSnapshot` / `StateConflictError`；
- 实现 `load_snapshot()`；
- 实现 `save_snapshot()` 的 `None / 0 / N` 三条路径。

✅ 完成标志：新建会话版本从 1 开始；旧版本保存抛冲突。

### 第 2 步：给 `SessionRegistry` 加版本委托

```python
def load_snapshot(self, session_id: str) -> StateSnapshot | None: ...
def save_snapshot(self, session_id, state, expected_version) -> int: ...
```

✅ 完成标志：`StateService` 不需要直接访问 `registry.store`。

### 第 3 步：实现 `apply_delta`

- 复制五个字段；
- 先删后 set；
- constraints/goals 去重；
- 目标状态转换；
- 返回新的 `SessionState`。

✅ 完成标志：传入的 state 不被修改。

### 第 4 步：实现 `StateService.apply`

- `acquire(session_id)`；
- `load_snapshot()`；
- 检查 `expected_version`；
- 空 delta 直接返回；
- `apply_delta()`；
- `save_snapshot(expected_version=current.version)`；
- 返回新 `StateSnapshot`。

✅ 完成标志：10 个并发 apply 版本从 1 到 10。

### 第 5 步：更新导出

```python
from .service import StateApplyError, StateService, apply_delta
from .store import StateConflictError, StateSnapshot, StateStore, StateStoreError
```

✅ 完成标志：`from state import StateService, StateSnapshot` 可用。

### 第 6 步：测试和冒烟

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson16_state_service.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m scripts.lesson16_smoke
```

期望：

```text
13 passed
174 passed
第一次 apply: version = 1
第二次 apply: version = 2
旧版本写入被拒绝: 状态版本冲突...
```

---

## 6. 测试代码

### 6.1 `tests/unit/test_lesson16_state_service.py`

```python
import asyncio
import tempfile
from pathlib import Path

import pytest

from state.extractor import StateDelta
from state.models import Goal, GoalStatus, SessionState
from state.service import StateApplyError, StateService, apply_delta
from state.session import SessionRegistry
from state.store import StateConflictError, StateStore


@pytest.fixture
def service() -> StateService:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson16-",
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


def test_get_missing_session_returns_version_zero(service):
    snapshot = asyncio.run(service.get("session-1"))

    assert snapshot.version == 0
    assert snapshot.state == SessionState()
    assert service.registry.exists("session-1") is False


def test_apply_creates_new_session_with_version_one(service):
    delta = StateDelta(
        preferences_set={"style": "dry"},
        goals_add=["买两瓶干型酒"],
    )

    snapshot = asyncio.run(service.apply("session-1", delta))

    assert snapshot.version == 1
    assert snapshot.state.preferences["style"] == "dry"
    assert snapshot.state.goals[0].text == "买两瓶干型酒"

    loaded = asyncio.run(service.get("session-1"))
    assert loaded.version == 1
    assert loaded.state == snapshot.state


def test_apply_all_delta_operations(service):
    async def main() -> None:
        await service.apply(
            "session-1",
            StateDelta(
                preferences_set={"style": "dry", "budget": 500},
                facts_set={"产区": "波尔多"},
                progress_set={"选酒": "in_progress", "比价": "pending"},
                constraints_add=["预算不超过 500 元"],
                goals_add=["买酒", "买酒杯"],
            ),
        )

        snapshot = await service.apply(
            "session-1",
            StateDelta(
                preferences_set={"budget": 800},
                preferences_remove=["style"],
                facts_set={"年份": "2019"},
                facts_remove=["产区"],
                progress_set={"选酒": "done"},
                progress_remove=["比价"],
                constraints_add=["不送人"],
                constraints_remove=["预算不超过 500 元"],
                goals_add=["再买两瓶"],
                goals_complete=["买酒"],
                goals_abandon=["买酒杯"],
            ),
        )

        assert snapshot.version == 2
        assert snapshot.state.preferences == {"budget": 800}
        assert snapshot.state.facts == {"年份": "2019"}
        assert snapshot.state.progress == {"选酒": "done"}
        assert snapshot.state.constraints == ["不送人"]
        assert [goal.text for goal in snapshot.state.goals] == [
            "买酒",
            "买酒杯",
            "再买两瓶",
        ]
        assert snapshot.state.goals[0].status is GoalStatus.DONE
        assert snapshot.state.goals[1].status is GoalStatus.ABANDONED
        assert snapshot.state.goals[2].status is GoalStatus.ACTIVE

    asyncio.run(main())


def test_empty_delta_does_not_create_or_bump(service):
    async def main() -> None:
        missing = await service.apply("missing", StateDelta())
        assert missing.version == 0
        assert service.registry.exists("missing") is False

        first = await service.apply(
            "session-1",
            StateDelta(preferences_set={"style": "dry"}),
        )
        second = await service.apply("session-1", StateDelta())

        assert first.version == 1
        assert second.version == 1
        assert second.state == first.state

    asyncio.run(main())


def test_expected_version_mismatch_raises_conflict(service):
    async def main() -> None:
        first = await service.apply(
            "session-1",
            StateDelta(preferences_set={"style": "dry"}),
        )
        second = await service.apply(
            "session-1",
            StateDelta(preferences_set={"budget": 500}),
            expected_version=first.version,
        )

        with pytest.raises(StateConflictError, match="版本冲突"):
            await service.apply(
                "session-1",
                StateDelta(preferences_set={"budget": 800}),
                expected_version=first.version,
            )

        current = await service.get("session-1")
        assert second.version == 2
        assert current.version == 2
        assert current.state.preferences == {"style": "dry", "budget": 500}

    asyncio.run(main())


def test_complete_missing_goal_raises_apply_error(service):
    async def main() -> None:
        await service.apply("session-1", StateDelta(goals_add=["买酒"]))

        with pytest.raises(StateApplyError, match="不存在"):
            await service.apply(
                "session-1",
                StateDelta(goals_complete=["不存在的目标"]),
            )

        current = await service.get("session-1")
        assert current.version == 1
        assert current.state.goals[0].status is GoalStatus.ACTIVE

    asyncio.run(main())


def test_complete_abandoned_goal_raises_apply_error(service):
    async def main() -> None:
        await service.apply("session-1", StateDelta(goals_add=["买酒"]))
        await service.apply("session-1", StateDelta(goals_abandon=["买酒"]))

        with pytest.raises(StateApplyError, match="放弃"):
            await service.apply(
                "session-1",
                StateDelta(goals_complete=["买酒"]),
            )

    asyncio.run(main())


def test_duplicate_goal_and_constraint_are_idempotent(service):
    delta = StateDelta(
        goals_add=["买酒"],
        constraints_add=["预算不超过 500 元"],
    )

    async def main() -> None:
        await service.apply("session-1", delta)
        snapshot = await service.apply("session-1", delta)

        assert snapshot.version == 2
        assert [goal.text for goal in snapshot.state.goals] == ["买酒"]
        assert snapshot.state.constraints == ["预算不超过 500 元"]

    asyncio.run(main())


def test_state_and_version_persist_across_service_instances(service):
    async def main() -> None:
        await service.apply(
            "session-1",
            StateDelta(
                preferences_set={"style": "dry"},
                goals_add=["买酒"],
            ),
        )

        other = StateService(SessionRegistry(service.registry.store))
        snapshot = await other.get("session-1")

        assert snapshot.version == 1
        assert snapshot.state.preferences["style"] == "dry"
        assert snapshot.state.goals[0].text == "买酒"

    asyncio.run(main())


def test_concurrent_apply_serializes_versions(service):
    async def add_goal(index: int):
        return await service.apply(
            "session-1",
            StateDelta(goals_add=[f"目标-{index}"]),
        )

    async def main() -> None:
        snapshots = await asyncio.gather(*(add_goal(i) for i in range(10)))

        assert sorted(snapshot.version for snapshot in snapshots) == list(
            range(1, 11)
        )

        current = await service.get("session-1")
        assert current.version == 10
        assert len(current.state.goals) == 10

    asyncio.run(main())


def test_apply_delta_does_not_mutate_input():
    state = SessionState(
        preferences={"style": "dry"},
        goals=[Goal(text="买酒")],
    )
    delta = StateDelta(
        preferences_set={"budget": 800},
        preferences_remove=["style"],
        constraints_add=["不送人"],
        goals_complete=["买酒"],
    )

    updated = apply_delta(state, delta)

    assert state.preferences == {"style": "dry"}
    assert state.constraints == []
    assert state.goals[0].status is GoalStatus.ACTIVE

    assert updated.preferences == {"budget": 800}
    assert updated.constraints == ["不送人"]
    assert updated.goals[0].status is GoalStatus.DONE


def test_store_snapshot_save_detects_stale_version(service):
    store = service.registry.store
    state = SessionState(preferences={"style": "dry"})

    assert store.save_snapshot("session-1", state, expected_version=0) == 1
    assert store.save_snapshot("session-1", state, expected_version=1) == 2

    with pytest.raises(StateConflictError, match="版本冲突"):
        store.save_snapshot("session-1", state, expected_version=1)


def test_blank_session_id_is_rejected(service):
    with pytest.raises(ValueError, match="session_id"):
        asyncio.run(service.get(""))

    with pytest.raises(ValueError, match="session_id"):
        asyncio.run(service.apply("   ", StateDelta()))
```

### 6.2 这 13 个用例分别防什么

| 测试 | 证明 |
|---|---|
| `test_get_missing_session_returns_version_zero` | 未初始化会话 version=0，不建行 |
| `test_apply_creates_new_session_with_version_one` | 第一次 apply 版本 1，状态持久化 |
| `test_apply_all_delta_operations` | 11 个 delta 字段都能正确应用 |
| `test_empty_delta_does_not_create_or_bump` | 空 delta 不写库、不涨版本 |
| `test_expected_version_mismatch_raises_conflict` | 旧版本写入被拒绝 |
| `test_complete_missing_goal_raises_apply_error` | 目标不存在不会静默成功 |
| `test_complete_abandoned_goal_raises_apply_error` | 非法状态转换被拒绝 |
| `test_duplicate_goal_and_constraint_are_idempotent` | 重试不会重复加目标/约束 |
| `test_state_and_version_persist_across_service_instances` | 版本和状态都落盘 |
| `test_concurrent_apply_serializes_versions` | 并发 apply 版本 1..10，无丢更新 |
| `test_apply_delta_does_not_mutate_input` | apply_delta 是纯函数 |
| `test_store_snapshot_save_detects_stale_version` | 存储层乐观锁生效 |
| `test_blank_session_id_is_rejected` | 空 ID 不会创建锁/状态 |

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson16_state_service.py -q
```

期望 `13 passed`；全量期望 `174 passed`。

---

## 7. 冒烟脚本

`scripts/lesson16_smoke.py`：

```python
import asyncio
import tempfile
from pathlib import Path

from state.extractor import StateDelta
from state.service import StateService
from state.session import SessionRegistry
from state.store import StateConflictError, StateStore


async def main() -> None:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson16-smoke-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    try:
        store = StateStore(db_path)
        store.initialize()
        service = StateService(SessionRegistry(store))

        first = await service.apply(
            "demo-session",
            StateDelta(
                preferences_set={"style": "dry", "budget": 500},
                facts_set={"产区": "波尔多"},
                goals_add=["买两瓶干型酒"],
                constraints_add=["预算不超过 500 元"],
            ),
        )
        print("第一次 apply: version =", first.version)
        print(first.state.model_dump_json(indent=2))

        second = await service.apply(
            "demo-session",
            StateDelta(
                preferences_set={"budget": 800},
                constraints_remove=["预算不超过 500 元"],
                constraints_add=["预算不超过 800 元"],
                goals_complete=["买两瓶干型酒"],
            ),
            expected_version=first.version,
        )
        print("第二次 apply: version =", second.version)

        try:
            await service.apply(
                "demo-session",
                StateDelta(goals_add=["再买两瓶"]),
                expected_version=first.version,
            )
        except StateConflictError as exc:
            print("旧版本写入被拒绝:", exc)

        # 新 service 实例从同一个 SQLite 读取，验证版本持久化。
        reopened = StateService(SessionRegistry(store))
        current = await reopened.get("demo-session")
        print("重新读取: version =", current.version)
        print("当前预算:", current.state.preferences["budget"])
        print("目标状态:", current.state.goals[0].status.value)
    finally:
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
```

运行：

```powershell
.venv\Scripts\python.exe -m scripts.lesson16_smoke
```

期望输出：

```text
第一次 apply: version = 1
第二次 apply: version = 2
旧版本写入被拒绝: 状态版本冲突...
重新读取: version = 2
当前预算: 800
目标状态: done
```

---

## 8. 参考实现

### 8.1 `StateStore` 版本相关改动

```python
@dataclass(frozen=True)
class StateSnapshot:
    state: SessionState
    version: int


class StateConflictError(StateStoreError):
    """乐观锁版本冲突：期望版本和数据库当前版本不一致。"""
```

schema 增加：

```sql
CREATE TABLE IF NOT EXISTS session_states (
    session_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);
```

`initialize()` 迁移旧表：

```python
columns = {
    row[1]
    for row in conn.execute("PRAGMA table_info(session_states)")
}
if "version" not in columns:
    conn.execute(
        "ALTER TABLE session_states "
        "ADD COLUMN version INTEGER NOT NULL DEFAULT 0"
    )
```

`load_snapshot()`：

```python
def load_snapshot(self, session_id: str) -> StateSnapshot | None:
    session_id = self._normalize_session_id(session_id)
    with self._connection() as conn:
        row = conn.execute(
            "SELECT state_json, version "
            "FROM session_states WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    if row is None:
        return None

    try:
        return StateSnapshot(
            state=SessionState.model_validate(json.loads(row[0])),
            version=int(row[1]),
        )
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise StateStoreError(f"状态数据损坏：{exc}") from exc
```

`save_snapshot()` 的三条路径：

```python
if expected_version is None:
    conn.execute(_UPSERT, (session_id, payload, now))
    row = conn.execute(
        "SELECT version FROM session_states WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row[0])

if expected_version == 0:
    cursor = conn.execute(_UPDATE_ZERO, (payload, now, session_id))
    if cursor.rowcount == 0:
        try:
            conn.execute(_INSERT, (session_id, payload, now))
        except sqlite3.IntegrityError as exc:
            raise StateConflictError(
                f"状态版本冲突：session={session_id}, expected=0"
            ) from exc
    return 1

cursor = conn.execute(
    _UPDATE_VERSIONED,
    (payload, now, session_id, expected_version),
)
if cursor.rowcount == 0:
    raise StateConflictError(
        f"状态版本冲突：session={session_id}, "
        f"expected={expected_version}"
    )
return expected_version + 1
```

`save()` 保留旧签名：

```python
def save(self, session_id, state, *, expected_version=None) -> int:
    return self.save_snapshot(session_id, state, expected_version)
```

### 8.2 `SessionRegistry` 委托

```python
def load_snapshot(self, session_id: str) -> StateSnapshot | None:
    return self.store.load_snapshot(session_id)

def save_snapshot(self, session_id, state, expected_version) -> int:
    return self.store.save_snapshot(
        session_id,
        state,
        expected_version=expected_version,
    )
```

### 8.3 `state/service.py`

<details>
<summary>展开核心参考实现（省略部分 docstring）</summary>

```python
"""把 StateDelta 应用到 SessionState，并处理版本与冲突。"""

from __future__ import annotations

from copy import deepcopy

from state.extractor import StateDelta
from state.models import Goal, GoalStatus, SessionState
from state.session import SessionRegistry
from state.store import StateConflictError, StateSnapshot


class StateApplyError(ValueError):
    """Delta 无法应用到当前状态。"""


def apply_delta(state: SessionState, delta: StateDelta) -> SessionState:
    """把 delta 应用到一个新状态，不修改传入的 state。"""
    if not isinstance(state, SessionState):
        raise TypeError("state 必须是 SessionState")
    if not isinstance(delta, StateDelta):
        raise TypeError("delta 必须是 StateDelta")

    preferences = deepcopy(state.preferences)
    facts = deepcopy(state.facts)
    progress = dict(state.progress)
    constraints = list(state.constraints)
    goals = [goal.model_copy(deep=True) for goal in state.goals]

    for key in delta.preferences_remove:
        preferences.pop(key, None)
    for key, value in delta.preferences_set.items():
        preferences[key] = deepcopy(value)

    for key in delta.facts_remove:
        facts.pop(key, None)
    for key, value in delta.facts_set.items():
        facts[key] = deepcopy(value)

    for key in delta.progress_remove:
        progress.pop(key, None)
    for key, value in delta.progress_set.items():
        progress[key] = value

    for text in delta.constraints_remove:
        if text in constraints:
            constraints.remove(text)
    for text in delta.constraints_add:
        if text not in constraints:
            constraints.append(text)

    for text in delta.goals_add:
        if not any(goal.text == text for goal in goals):
            goals.append(Goal(text=text))

    for text in delta.goals_complete:
        index = _find_goal(goals, text)
        goal = goals[index]
        if goal.status == GoalStatus.DONE:
            continue
        if goal.status == GoalStatus.ABANDONED:
            raise StateApplyError(f"已放弃的目标不能标记完成：{text}")
        goals[index] = goal.model_copy(update={"status": GoalStatus.DONE})

    for text in delta.goals_abandon:
        index = _find_goal(goals, text)
        goal = goals[index]
        if goal.status == GoalStatus.ABANDONED:
            continue
        if goal.status == GoalStatus.DONE:
            raise StateApplyError(f"已完成的目标不能标记放弃：{text}")
        goals[index] = goal.model_copy(update={"status": GoalStatus.ABANDONED})

    return SessionState(
        preferences=preferences,
        goals=goals,
        constraints=constraints,
        progress=progress,
        facts=facts,
    )


def _find_goal(goals: list[Goal], text: str) -> int:
    for index, goal in enumerate(goals):
        if goal.text == text:
            return index
    raise StateApplyError(f"目标不存在：{text}")


class StateService:
    """带版本和冲突检查的状态服务。"""

    def __init__(self, registry: SessionRegistry):
        self.registry = registry

    async def get(self, session_id: str) -> StateSnapshot:
        async with self.registry.acquire(session_id):
            snapshot = self.registry.load_snapshot(session_id)
            return snapshot if snapshot is not None else StateSnapshot(
                state=SessionState(),
                version=0,
            )

    async def apply(
        self,
        session_id: str,
        delta: StateDelta,
        *,
        expected_version: int | None = None,
    ) -> StateSnapshot:
        if not isinstance(delta, StateDelta):
            raise TypeError("delta 必须是 StateDelta")
        if expected_version is not None:
            if (
                not isinstance(expected_version, int)
                or isinstance(expected_version, bool)
                or expected_version < 0
            ):
                raise ValueError("expected_version 必须是非负整数或 None")

        async with self.registry.acquire(session_id):
            snapshot = self.registry.load_snapshot(session_id)
            current = snapshot if snapshot is not None else StateSnapshot(
                state=SessionState(),
                version=0,
            )

            if (
                expected_version is not None
                and expected_version != current.version
            ):
                raise StateConflictError(
                    f"状态版本冲突：期望 {expected_version}，"
                    f"当前 {current.version}"
                )

            if delta.is_empty():
                return current

            updated = apply_delta(current.state, delta)
            new_version = self.registry.save_snapshot(
                session_id,
                updated,
                expected_version=current.version,
            )
            return StateSnapshot(state=updated, version=new_version)
```

</details>

### 8.4 `state/__init__.py`

```python
from .models import Goal, GoalStatus, SessionState
from .service import StateApplyError, StateService, apply_delta
from .session import SessionRegistry, StateMutator
from .store import (
    StateConflictError,
    StateSnapshot,
    StateStore,
    StateStoreError,
)
```

---

## 9. 验收清单（DoD）

- [x] `session_states` 有 `version` 列，旧表会自动 `ALTER TABLE`
- [x] `load_snapshot()` 返回 `state + version`
- [x] `save_snapshot(expected=0)` 能创建新状态
- [x] `save_snapshot(expected=N)` 用 `WHERE version=N` 检测冲突
- [x] `apply_delta()` 不修改输入状态
- [x] 11 个 delta 字段都能正确应用
- [x] 重复目标/约束幂等，不重复插入
- [x] 完成不存在的目标抛 `StateApplyError`
- [x] 已完成的目标不能 abandon，已放弃的不能 complete
- [x] 空 delta 不写库、不涨版本
- [x] `StateService.apply` 的 `expected_version` 不匹配时抛 `StateConflictError`
- [x] 10 个并发 apply 版本为 1..10
- [x] `pytest tests/unit/test_lesson16_state_service.py -q` → 13 passed
- [x] 全量 `pytest -q` → 174 passed
- [x] 冒烟脚本能展示版本 1、2 和冲突

---

## 10. 常见坑

| 坑 | 现象 | 处理 |
|---|---|---|
| 把 version 放进 `SessionState` | 领域状态耦合存储元数据 | 放 `StateSnapshot` / 数据库列 |
| `save` 不带 expected_version | 跨进程仍会后写覆盖先写 | 使用 `save_snapshot(expected_version=...)` |
| 创建也用 `UPDATE ... WHERE version=0` 后不 INSERT | 新会话永远保存不了 | 先 UPDATE，rowcount=0 再 INSERT |
| 旧表没有 version 列 | `no such column: version` | `initialize()` 里 `PRAGMA table_info` + `ALTER TABLE` |
| 应用前不复制状态 | 失败时旧状态已被改了一半 | `deepcopy` / `model_copy(deep=True)` |
| 重复 add 直接 append | 重试产生重复目标/约束 | 按文本去重，保持幂等 |
| 完成不存在目标静默 no-op | 状态和用户意图不一致 | 抛 `StateApplyError` |
| 空 delta 也 save | 每轮都涨版本、写 SQLite | `delta.is_empty()` 直接返回 |
| 只靠 asyncio.Lock | 多 worker 仍冲突 | 锁 + 数据库 version 两层 |
| 冲突后自动覆盖 | 丢失另一个进程的修改 | 抛错，让调用方重读/重试/合并 |

---

## 11. 扩展与思考题

1. **目标 ID**：现在按 `goal.text` 精确匹配；如果模型改写文本，完成操作会失败。加 `Goal.id` 后 Delta 应该怎么变？设计见 [goal-id-design.md](goal-id-design.md)。
2. **冲突合并**：`preferences_set` 冲突时能否字段级 last-write-wins？`goals` 冲突能否按目标 ID 合并？
3. **Delta 日志**：是否要把每次 `StateDelta` 存成 append-only 表，支持审计/回放/撤销？
4. **版本粒度**：整个 session 一个版本，还是每个字段一个版本？代价是什么？
5. **空 delta 与“无变化”**：如果 delta 非空但应用后状态完全相同（重复 add），要不要也跳过写库？
6. **批量应用**：一次对话提取出多个 delta，如何保证它们在同一版本事务里应用？
7. **失败重试**： `StateConflictError` 时自动重读+重试几次？如何避免无限重试？
8. **多进程存储**：换 Postgres/Redis 后，`save_snapshot` 的乐观锁 SQL/命令怎么写？

---

## 12. 与第 17 课的衔接

本课已经能安全地把 delta 应用进状态，并持久化版本。但模型还看不到状态：

```text
StateService
     │
     ▼
StateSnapshot
     │
     ▼
??? 怎么把状态放进 prompt？
```

第 17 课做 `StateRenderer`：

```text
StateRenderer
├─ 把 SessionState 渲染成稳定、紧凑的文本
├─ 控制 token 预算
├─ 只渲染与当前任务相关的字段
└─ 放进 system / user 上下文
```

一句话：

> **第 16 课把 delta 安全地写进状态，第 17 课把状态安全地读给模型。**
