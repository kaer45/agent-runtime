# 第 14 课：状态持久化

> 模块三 · 上下文与状态（第 5/11 课）
> 前置：第 13 课（`StateExtractor` 已能产出 `StateDelta`）
> 下一课：第 15 课做多会话隔离与并发安全
> 预计用时：2-3 小时
> 本课分支：`dev`

---

## 0. 开工前检查

先确认第 13 课是干净的：

```powershell
git switch dev
git status --short
.venv\Scripts\python.exe -m pytest --ignore=tests/unit/test_lesson14_state_store.py -q
```

期望：`139 passed`。

第 14 课不新增依赖，使用 Python 标准库 `sqlite3`。

> 本课测试是“先红后绿”：实现 `state/store.py` 之前，运行
> `pytest tests/unit/test_lesson14_state_store.py -q` 会因
> `ModuleNotFoundError: No module named 'state.store'` 失败。

---

## 1. 本课目标

第 12 课定义了状态，第 13 课能从对话里提取增量，但状态仍然只在内存里：

```text
进程重启 / 服务重启 / 换一个请求 → 状态没了
```

本课把 `SessionState` 写进 SQLite：

```text
SessionState
     │
     ▼
 StateStore
     │  save / load / delete / exists
     ▼
 SQLite 文件（session_states 表）
```

学完你应该能回答：

1. 为什么状态要持久化，而不是只放在内存或摘要里？
2. 为什么本课用 SQLite，而不是 JSON 文件、Redis 或 Postgres？
3. 为什么 `session_id` 放在存储层，而不是加进 `SessionState`？
4. 为什么状态先存成 JSON 文本，而不是拆成很多张表？
5. 为什么 `load()` 遇到损坏数据要抛错，而不是返回空状态？
6. 为什么本课不处理版本、冲突和 delta 应用？

**本课产出**

| 交付物 | 说明 |
|---|---|
| `state/store.py` | `StateStore` / `StateStoreError` |
| `state/__init__.py` | 导出存储类型 |
| `tests/unit/test_lesson14_state_store.py` | 11 个测试用例 |
| `scripts/lesson14_smoke.py` | 不调用模型、使用临时 SQLite 的冒烟脚本 |

**本课明确不做**

- 不做 `StateDelta` 的应用（第 16 课 `StateService`）
- 不做版本号、乐观锁、冲突处理（第 16 课）
- 不做多会话隔离策略与并发锁（第 15 课）
- 不做 delta 审计表（第 16/19 课）
- 不做加密、备份、数据迁移（生产化阶段）
- 不修改 `AgentRuntime`

---

## 2. 问题：内存状态活不过进程重启

当前 `SessionState` 只是一个 Pydantic 对象。请求结束、进程退出、服务重启，它就没了。

常见错误做法：

| 做法 | 问题 |
|---|---|
| 只靠摘要 | 摘要适合给模型读，不适合程序按字段查询/合并 |
| 每次启动重新问用户 | 用户体验差，且历史消息也不一定还在 |
| 把状态写进日志再解析 | 日志是观测数据，不是事务存储 |
| 直接 `json.dump` 到文件 | 并发写、原子性、损坏恢复都要自己处理 |
| 放 Redis | 多一个服务依赖；本课还不需要跨进程共享 |

本课选择 SQLite：

- Python 标准库自带，零额外依赖；
- 单文件，适合本地 Agent；
- 有事务和并发读写的基础能力；
- 后续第 15 课可以在同一张表上做 `session_id` 隔离；
- 数据量小，不需要上 Postgres。

### SQLite / JSON 文件 / Redis 对比

| 方案 | 优点 | 缺点 | 适用 |
|---|---|---|---|
| JSON 文件 | 最简单、可读 | 并发写容易互相覆盖；无事务；损坏恢复要自己做 | 单进程 demo |
| SQLite | 标准库、事务、单文件、支持并发读 | 仍是单机；写并发有限 | 本地 Agent、单机服务 |
| Redis | 快、跨进程 | 额外服务；持久化配置复杂 | 多实例、短 TTL 状态 |
| Postgres | 强一致、复杂查询、多人 | 运维成本高 | 生产多用户 |

本课先用 SQLite；等真的多实例部署，再换存储实现，接口不变。

---

## 3. 设计决策

### 3.1 一张表 + 一个 JSON 快照

表结构：

```sql
CREATE TABLE IF NOT EXISTS session_states (
    session_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

为什么不是把 `preferences`、`goals`、`facts` 拆成多张表：

- 当前阶段只需要“按会话读写整份状态”，不需要按字段查询；
- 状态 schema 还在演进（第 16 课会加版本，后面可能加 goal id）；
- Pydantic 已经是状态 schema 的唯一事实来源，拆表会多一份映射；
- JSON 快照可以用 `SessionState.model_validate()` 直接恢复，校验逻辑不分叉。

代价：

- 不能直接用 SQL 查“预算大于 500 的会话”；
- 字段级更新要读出整份状态再写回；
- 数据量大时 JSON 解析有成本。

这些在当前规模都不是问题。等需要分析/检索时，再考虑投影表或独立查询存储。

### 3.2 `session_id` 是存储 key，不是状态字段

`SessionState` 仍然保持第 12 课的样子：

```python
class SessionState(BaseModel):
    preferences: ...
    goals: ...
    constraints: ...
    progress: ...
    facts: ...
```

`session_id` 放在 `StateStore` 的方法参数里：

```python
store.save("session-1", state)
store.load("session-1")
```

为什么：

- `session_id` 是“这份状态属于谁”的元数据，不是用户偏好/目标/事实；
- 同一个状态对象可以被不同存储层使用，不应该绑定数据库主键；
- 第 15 课做多会话隔离时，隔离逻辑在存储/服务层，不需要污染状态模型；
- 第 16 课做版本时，版本号同样是存储元数据，也不应塞进 `SessionState`。

> 第 12 课说“`session_id` 第 15 课再加”，指的是**状态模型**里不加；
> 本课把它作为存储 key 使用，不矛盾。

### 3.3 保存是 upsert，不是 insert

同一个 `session_id` 多次保存，应该覆盖旧快照：

```sql
INSERT INTO session_states (session_id, state_json, updated_at)
VALUES (?, ?, ?)
ON CONFLICT(session_id) DO UPDATE SET
    state_json = excluded.state_json,
    updated_at = excluded.updated_at;
```

为什么不用 `INSERT OR REPLACE`：

- `INSERT OR REPLACE` 在语义上是“删掉旧行再插入新行”；
- 未来如果有外键/触发器，可能产生意外副作用；
- `ON CONFLICT ... DO UPDATE` 更明确表达“更新同一行”。

### 3.4 每次操作一个连接，用完就关

`StateStore` 不长期持有一个 `sqlite3.Connection`，而是每次 `save/load/...` 打开连接：

```python
conn = sqlite3.connect(self.db_path, timeout=5)
try:
    with conn:          # 成功提交，异常回滚
        ...
finally:
    conn.close()
```

原因：

- 避免 `check_same_thread` 问题：FastAPI/线程池可能在不同线程调用；
- 每次操作都是独立事务，不会把上一次的未提交状态带进来；
- 连接泄漏风险小，`finally` 一定关闭。

代价：

- 每次都有连接开销；当前规模可以接受；
- 高频写入时需要连接池/长连接，后面再优化。

### 3.5 `updated_at` 先存，但不暴露

保存时写入 UTC ISO 时间：

```python
datetime.now(timezone.utc).isoformat(timespec="seconds")
```

本课不提供 `get_updated_at()`，因为还没有消费者。它的用途：

- 排查“状态是什么时候写的”；
- 第 15/16 课做并发/冲突时，可以作为辅助信息；
- 未来做状态过期、清理、审计。

时间统一用 UTC，避免本地时区/夏令时问题；展示给用户时再转本地时间。

### 3.6 损坏数据要显式报错

`load()` 遇到以下情况不返回空状态，而是抛 `StateStoreError`：

- `state_json` 不是合法 JSON；
- JSON 能解析，但不是 `SessionState` 的结构（例如多了未知字段）；
- 数据库读取失败。

为什么不能返回 `SessionState()`：

- 空状态会被误认为“用户没有偏好/目标”，导致后续逻辑静默丢数据；
- 损坏是运维/迁移问题，应该暴露出来，而不是掩盖；
- 调用方可以选择降级，但必须知道发生了错误。

`load()` 只在“行不存在”时返回 `None`；这两个语义要分开：

```text
不存在      → None
存在但坏了  → StateStoreError
```

### 3.7 本课不碰版本和 delta 应用

`StateStore` 只做快照读写：

```text
StateStore: 存/取 SessionState
StateService: 应用 StateDelta、版本、冲突（第 16 课）
```

如果本课就加 `version`，会提前绑定第 16 课的方案；现在只保留 `updated_at`，版本列等 `StateService` 设计确定后再加。

`StateDelta` 也先不落库：

- 它只是“变更建议”，还没被应用；
- 没有消费者之前存 delta 只是堆积日志；
- 第 16/19 课需要审计/回放时，再加 `state_deltas` 表。

### 3.8 同步 `sqlite3` 的边界

`sqlite3` 是同步阻塞 API。当前操作很小（单行读写），先直接调用；但在 async Web 服务里，长时间阻塞会卡事件循环。

后续可选方案：

- `asyncio.to_thread(store.load, ...)` 把同步调用丢到线程池；
- `aiosqlite` 提供 async 接口（新增依赖）；
- 连接池/读写分离。

本课先把存储逻辑做对，不引入异步复杂度。

---

## 4. 接口与数据结构

### 4.1 表结构

```sql
CREATE TABLE IF NOT EXISTS session_states (
    session_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

### 4.2 `StateStore`

```python
class StateStore:
    def __init__(self, db_path: str | Path): ...

    def initialize(self) -> None: ...
    def save(self, session_id: str, state: SessionState) -> None: ...
    def load(self, session_id: str) -> SessionState | None: ...
    def exists(self, session_id: str) -> bool: ...
    def delete(self, session_id: str) -> bool: ...
```

语义：

| 方法 | 行为 |
|---|---|
| `initialize()` | 创建父目录和表；可重复调用 |
| `save()` | 插入或覆盖快照；`state` 必须是 `SessionState` |
| `load()` | 不存在返回 `None`；损坏抛 `StateStoreError` |
| `exists()` | 只查是否存在，不反序列化 |
| `delete()` | 返回是否真的删掉了一行 |

### 4.3 `StateStoreError`

```python
class StateStoreError(RuntimeError):
    """状态保存/读取失败，或数据库中的状态已损坏。"""
```

消息前缀：

```text
初始化状态库失败：...
保存状态失败：...
状态序列化失败：...
读取状态失败：...
状态数据损坏：...
检查状态失败：...
删除状态失败：...
```

调用方可以按前缀/异常类型决定日志和降级策略。

### 4.4 `session_id` 规范

- 必须是字符串；
- 首尾空白会被 strip；
- 空字符串 / 全空白 → `ValueError`；
- 非字符串 → `TypeError`。

这属于存储层的输入校验，不是 `StateDelta`/`SessionState` 的职责。

---

## 5. 实现步骤

### 第 1 步：建 `state/store.py`

```text
state/
├── __init__.py
├── models.py       # 第 12 课
├── extractor.py    # 第 13 课
└── store.py        # 本课
```

✅ 完成标志：`from state.store import StateStore, StateStoreError` 可用。

### 第 2 步：写 schema 和 `initialize()`

- `CREATE TABLE IF NOT EXISTS`；
- 自动创建父目录；
- 可重复调用；
- 数据库异常转 `StateStoreError`。

✅ 完成标志：同一个路径调用两次 `initialize()` 不报错。

### 第 3 步：实现 `save()`

- 校验 `session_id`；
- 校验 `state` 类型；
- `state.model_dump_json()` 序列化；
- upsert；
- 写入 UTC `updated_at`。

✅ 完成标志：保存后能读回同一个状态。

### 第 4 步：实现 `load()`

- 查 `state_json`；
- 不存在 → `None`；
- `json.loads` + `SessionState.model_validate`；
- 损坏 → `StateStoreError`。

✅ 完成标志：正常状态往返；坏 JSON 抛错。

### 第 5 步：实现 `exists()` / `delete()`

- `exists` 用 `SELECT 1 ... LIMIT 1`；
- `delete` 返回 `rowcount > 0`。

✅ 完成标志：删除前 true，删除后 false。

### 第 6 步：更新 `state/__init__.py`

```python
from .store import StateStore, StateStoreError

__all__ = [
    ...,
    "StateStore",
    "StateStoreError",
]
```

✅ 完成标志：`from state import StateStore` 可用。

### 第 7 步：测试和冒烟

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson14_state_store.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m scripts.lesson14_smoke
```

期望：

```text
11 passed
150 passed
是否读到状态: True
```

---

## 6. 测试代码

### 6.1 `tests/unit/test_lesson14_state_store.py`

> 当前测试的临时数据库放在工作区根目录，而不是系统临时目录：
> 本项目的 DSH 沙箱对系统 `tmp_path` 没有稳定写权限。fixture 会在结束时删除文件。
> 如果你本地 `tmp_path` 正常，也可以改成 `tmp_path / "state.db"`。

```python
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from state.models import GoalStatus, SessionState
from state.store import StateStore, StateStoreError


def sample_state(budget: int = 500) -> SessionState:
    return SessionState(
        preferences={"style": "dry", "budget": budget},
        goals=[{"text": "买两瓶干型酒", "status": "done"}],
        constraints=["预算不超过 500 元"],
        progress={"选酒": "done"},
        facts={"产区": "波尔多"},
    )


@pytest.fixture
def db_path() -> Path:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson14-",
        suffix=".db",
        delete=False,
    ) as handle:
        path = Path(handle.name)

    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


@pytest.fixture
def store(db_path: Path) -> StateStore:
    store = StateStore(db_path)
    store.initialize()
    return store


def test_initialize_is_idempotent(store):
    store.initialize()

    state = sample_state()
    store.save("session-1", state)

    assert store.load("session-1") == state


def test_save_and_load_round_trip(store):
    state = sample_state()

    store.save("session-1", state)
    loaded = store.load("session-1")

    assert loaded == state
    assert loaded.preferences["style"] == "dry"
    assert loaded.goals[0].status is GoalStatus.DONE
    assert loaded.progress["选酒"] == "done"
    assert loaded.facts["产区"] == "波尔多"


def test_load_unknown_session_returns_none(store):
    assert store.load("missing") is None


def test_save_overwrites_existing_snapshot(store):
    store.save("session-1", sample_state(budget=500))
    store.save("session-1", sample_state(budget=800))

    loaded = store.load("session-1")

    assert loaded is not None
    assert loaded.preferences["budget"] == 800


def test_exists_and_delete(store):
    assert store.exists("session-1") is False

    store.save("session-1", sample_state())
    assert store.exists("session-1") is True

    assert store.delete("session-1") is True
    assert store.delete("session-1") is False
    assert store.exists("session-1") is False
    assert store.load("session-1") is None


def test_state_persists_across_store_instances(db_path):
    state = sample_state()

    first = StateStore(db_path)
    first.initialize()
    first.save("session-1", state)

    second = StateStore(db_path)
    second.initialize()

    assert second.load("session-1") == state


def test_initialize_creates_parent_directory():
    base = Path.cwd() / f"lesson14-{uuid.uuid4().hex}"
    db_path = base / "nested" / "data" / "state.db"

    try:
        store = StateStore(db_path)
        store.initialize()

        assert db_path.exists()
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.parametrize("session_id", ["", "   "])
def test_blank_session_id_is_rejected(store, session_id):
    with pytest.raises(ValueError, match="session_id"):
        store.save(session_id, sample_state())

    with pytest.raises(ValueError, match="session_id"):
        store.load(session_id)

    with pytest.raises(ValueError, match="session_id"):
        store.exists(session_id)

    with pytest.raises(ValueError, match="session_id"):
        store.delete(session_id)


def test_save_rejects_non_session_state(store):
    with pytest.raises(TypeError, match="SessionState"):
        store.save("session-1", {"preferences": {"style": "dry"}})


def test_corrupt_state_json_raises_store_error(store):
    store.save("session-1", sample_state())

    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        conn.execute(
            "UPDATE session_states SET state_json = ? WHERE session_id = ?",
            ("{not-json", "session-1"),
        )

    with pytest.raises(StateStoreError, match="状态数据损坏"):
        store.load("session-1")

    with closing(sqlite3.connect(store.db_path)) as conn, conn:
        conn.execute(
            "UPDATE session_states SET state_json = ? WHERE session_id = ?",
            ('{"unknown_field": "x"}', "session-1"),
        )

    with pytest.raises(StateStoreError, match="状态数据损坏"):
        store.load("session-1")
```

### 6.2 这 11 个用例分别防什么

| 测试 | 证明 |
|---|---|
| `test_initialize_is_idempotent` | 重复初始化不会炸 |
| `test_save_and_load_round_trip` | 五类字段和 `GoalStatus` 能完整往返 |
| `test_load_unknown_session_returns_none` | “不存在”和“损坏”语义分开 |
| `test_save_overwrites_existing_snapshot` | 同一 session 是覆盖，不是插入第二行 |
| `test_exists_and_delete` | 删除返回布尔值，删除后读不到 |
| `test_state_persists_across_store_instances` | 数据真的落盘，不是进程内缓存 |
| `test_initialize_creates_parent_directory` | 嵌套路径能自动建目录 |
| `test_blank_session_id_is_rejected` | 空/全空白 session_id 不会写进库（2 个参数） |
| `test_save_rejects_non_session_state` | 存储层不接受任意 dict |
| `test_corrupt_state_json_raises_store_error` | 坏 JSON / schema 漂移会显式报错 |

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson14_state_store.py -q
```

期望 `11 passed`；全量期望 `150 passed`。

---

## 7. 冒烟脚本

`scripts/lesson14_smoke.py`：

```python
import tempfile
from pathlib import Path

from state.models import SessionState
from state.store import StateStore


def main() -> None:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson14-smoke-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    try:
        store = StateStore(db_path)
        store.initialize()

        state = SessionState(
            preferences={"style": "dry", "budget": 800},
            goals=[{"text": "买两瓶干型酒", "status": "done"}],
            constraints=["预算不超过 800 元"],
            progress={"选酒": "done"},
            facts={"产区": "勃艮第"},
        )

        store.save("demo-session", state)

        # 新建一个 store，证明数据确实落到了 SQLite 文件。
        reopened = StateStore(db_path)
        reopened.initialize()
        loaded = reopened.load("demo-session")

        print("是否读到状态:", loaded is not None)
        if loaded is not None:
            print(loaded.model_dump_json(indent=2))

        print("删除前是否存在:", store.exists("demo-session"))
        print("是否删除成功:", store.delete("demo-session"))
        print("删除后是否存在:", store.exists("demo-session"))
    finally:
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
```

运行：

```powershell
.venv\Scripts\python.exe -m scripts.lesson14_smoke
```

期望输出包含：

```text
是否读到状态: True
删除前是否存在: True
是否删除成功: True
删除后是否存在: False
```

以及一份和保存前一致的 `SessionState` JSON。

---

## 8. 参考实现

> 建议先自己写，再对照。重点看连接/事务/错误包装，不是背代码。

<details>
<summary>展开 `state/store.py` 参考实现</summary>

```python
"""用 SQLite 持久化会话状态快照。

本模块只负责保存/读取 ``SessionState``；状态合并、版本和冲突处理
留给第 16 课的 ``StateService``，多会话隔离留给第 15 课。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from state.models import SessionState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_states (
    session_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_UPSERT = """
INSERT INTO session_states (session_id, state_json, updated_at)
VALUES (?, ?, ?)
ON CONFLICT(session_id) DO UPDATE SET
    state_json = excluded.state_json,
    updated_at = excluded.updated_at
"""


class StateStoreError(RuntimeError):
    """状态保存/读取失败，或数据库中的状态已损坏。"""


class StateStore:
    """SQLite 状态存储。

    Example:
        store = StateStore("data/state.db")
        store.initialize()
        store.save("session-1", state)
        restored = store.load("session-1")
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        """创建父目录和表；可重复调用。"""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as conn:
                conn.execute(_SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            raise StateStoreError(f"初始化状态库失败：{exc}") from exc

    def save(self, session_id: str, state: SessionState) -> None:
        """保存或覆盖一个会话的状态快照。"""
        session_id = self._normalize_session_id(session_id)
        if not isinstance(state, SessionState):
            raise TypeError("state 必须是 SessionState")

        try:
            payload = state.model_dump_json()
        except Exception as exc:
            raise StateStoreError(f"状态序列化失败：{exc}") from exc

        try:
            with self._connection() as conn:
                conn.execute(
                    _UPSERT,
                    (
                        session_id,
                        payload,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
        except sqlite3.Error as exc:
            raise StateStoreError(f"保存状态失败：{exc}") from exc

    def load(self, session_id: str) -> SessionState | None:
        """读取会话状态；不存在返回 ``None``。"""
        session_id = self._normalize_session_id(session_id)

        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT state_json FROM session_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError(f"读取状态失败：{exc}") from exc

        if row is None:
            return None

        try:
            return SessionState.model_validate(json.loads(row[0]))
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise StateStoreError(f"状态数据损坏：{exc}") from exc

    def exists(self, session_id: str) -> bool:
        """会话状态是否存在。"""
        session_id = self._normalize_session_id(session_id)

        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM session_states WHERE session_id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise StateStoreError(f"检查状态失败：{exc}") from exc

        return row is not None

    def delete(self, session_id: str) -> bool:
        """删除会话状态；返回是否真的删掉了一行。"""
        session_id = self._normalize_session_id(session_id)

        try:
            with self._connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM session_states WHERE session_id = ?",
                    (session_id,),
                )
                deleted = cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise StateStoreError(f"删除状态失败：{exc}") from exc

        return deleted

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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
from .store import StateStore, StateStoreError

__all__ = [
    "Goal",
    "GoalStatus",
    "SessionState",
    "STATE_TOOL_NAME",
    "StateDelta",
    "StateExtractionError",
    "StateExtractor",
    "StateStore",
    "StateStoreError",
]
```

---

## 9. 验收清单（DoD）

- [x] `state/store.py` 存在，不依赖网络和第三方库
- [x] `initialize()` 可重复调用，能创建父目录和表
- [x] `save()` 同一 session 覆盖旧快照
- [x] `load()` 能完整还原五个字段和 `GoalStatus`
- [x] 不存在的 session 返回 `None`
- [x] 损坏 JSON / schema 漂移抛 `StateStoreError`
- [x] `exists()` / `delete()` 返回值符合语义
- [x] 空 `session_id` 被拒绝
- [x] `db_path` 指向新路径时，新建 `StateStore` 能读到旧数据
- [x] `pytest tests/unit/test_lesson14_state_store.py -q` → 11 passed
- [x] 全量 `pytest -q` → 150 passed
- [x] 冒烟脚本创建并清理自己的临时数据库

---

## 10. 常见坑

| 坑 | 现象 | 处理 |
|---|---|---|
| 每次操作新建连接但不关闭 | Windows 文件被占用，测试删不掉 DB | `finally: conn.close()` |
| `with sqlite3.connect(...)` 以为会关闭连接 | 事务提交了，连接还在 | `with conn` 只管事务，关闭要自己做 |
| 用 `INSERT` 保存第二次 | `UNIQUE constraint failed` | 用 upsert |
| 损坏 JSON 返回空状态 | 用户状态“悄悄清空” | 抛 `StateStoreError` |
| 把 `session_id` 加进 `SessionState` | 状态模型绑定存储 | 作为方法参数/列 |
| 用本地时间写 `updated_at` | 跨时区/夏令时混乱 | 统一 UTC |
| 在 async 路由里直接长时间查 SQLite | 阻塞事件循环 | 小操作先接受，后续用线程池/aiosqlite |
| 测试用系统 `tmp_path` 但沙箱无权限 | 测试全 ERROR | 用工作区临时文件，或修 sandbox 权限 |
| 把数据库文件提交进 Git | 仓库变大、状态泄露 | `.gitignore` 忽略 `*.db` |

---

## 11. 扩展与思考题

1. **JSON 快照 vs 拆表**：什么时候值得把 `goals` 拆成独立表？查询、索引、并发更新分别带来什么收益？
2. **版本号**：第 16 课要加 `version`，是加列、加独立表，还是用 `updated_at` 做乐观锁？各自的问题是什么？
3. **并发写**：两个请求同时 `save` 同一个 session，后写覆盖先写，如何检测/避免？
4. **异步 SQLite**：`asyncio.to_thread` 和 `aiosqlite` 的取舍是什么？
5. **WAL 模式**：`PRAGMA journal_mode=WAL` 对读写并发有什么帮助？会多出哪些文件？
6. **迁移**：`SessionState` 加字段后，旧 JSON 怎么迁移？`extra="forbid"` 会不会让旧数据读不出来？
7. **加密**：预算、地址等敏感信息要不要加密存储？密钥放哪里？
8. **备份/损坏恢复**：SQLite 文件损坏时，怎么检测、备份、重建？

---

## 12. 与第 15 课的衔接

本课已经能按 `session_id` 存/取状态，但还没有回答：

```text
两个会话同时读写，会不会串？
同一个会话并发写，谁赢？
```

第 15 课做多会话隔离与并发安全：

```text
StateStore（单会话读写）
        │
        ▼
SessionRegistry / 锁 / 隔离测试
├─ session A 与 B 不串数据
├─ 同 session 串行写
└─ 并发读不互相污染
```

一句话：

> **第 14 课把状态放进 SQLite，第 15 课保证多个会话互不串，第 16 课再决定 delta 怎么安全地应用。**
