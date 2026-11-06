# 第 20 课：统一历史管理与持久化

> 模块三 · 上下文与状态（第 11/11 课）
> 前置：第 19 课（`TurnStateUpdater`）
> 下一课：第 20.5 课接入设计，第 20.6 课接 Runtime
> 说明：本课采用 **D 模型**（摘要覆盖可以领先于物理删除）+ `ContextPlanner`

---

## 0. 开工前检查

```powershell
git switch dev
git status --short
.venv\Scripts\python.exe -m pytest --ignore=tests/unit/test_lesson20_history_store.py --ignore=tests/unit/test_lesson20_context_planner.py -q
```

期望：`217 passed`。

测试命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson20_history_store.py -q
# 29 passed

.venv\Scripts\python.exe -m pytest tests/unit/test_lesson20_context_planner.py -q
# 9 passed

.venv\Scripts\python.exe -m pytest -q
# 256 passed
```

---

## 1. 本课目标

```text
messages
   +
RollingSummary
   ↓
HistoryStore
├─ 消息按 id 追加
├─ 摘要按 session 覆盖
├─ covered_through_id：摘要覆盖水位线
├─ append 只写
├─ plan_trim 只读
└─ commit_trim 删除 + 摘要 + 水位线
```

并增加：

```text
ContextPlanner
├─ state 配额
├─ summary 配额
├─ history 配额
└─ 预裁剪历史
```

核心变化：

> **`covered_through_id` 不再是“删除边界”，而是“摘要覆盖边界”。**
> 消息可以在 `id <= covered_through_id` 时仍然留在库里。

---

## 2. 为什么需要 D 模型

如果只在物理删除时生成摘要，会出现：

```text
TokenBudget 本轮丢了 m1~m3
但 m1~m3 还在数据库
摘要没覆盖它们
→ 本轮 system 缺信息
→ 下一轮存储裁剪又把它们摘要一次
```

D 模型允许：

```text
本轮 prompt 丢了 m1~m3
→ 摘要 m1~m3
→ covered_through_id = 3
→ 消息暂时不删
→ 下一轮存储裁剪：m1~m3 已覆盖，直接删，不再摘要
```

这样：

- 本轮上下文完整；
- 不重复摘要；
- 物理删除可以延后。

---

## 3. 表结构

```sql
CREATE TABLE session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE session_summaries (
    session_id TEXT PRIMARY KEY,
    summary_json TEXT NOT NULL,
    covered_through_id INTEGER,
    updated_at TEXT NOT NULL
);
```

`covered_through_id` 语义：

```text
所有 id <= covered_through_id 的消息，都已经进入当前摘要
```

不表示已经删除。

本课不处理旧表迁移（当前没有历史表）。

---

## 4. API

### 4.1 `append()`

```python
def append(
    self,
    session_id: str,
    messages: Sequence[dict],
) -> None:
```

只写消息：

- 不裁剪；
- 不更新摘要；
- 不推进水位线。

### 4.2 `save_summary()`

```python
def save_summary(
    self,
    session_id: str,
    summary: RollingSummary,
    *,
    covered_through_id: int | None = None,
) -> None:
```

| 参数 | 行为 |
|---|---|
| `None` | 只更新摘要正文 |
| `N` | 更新摘要，并把覆盖水位线推进到 N |

校验：

- 水位线单调不减；
- 不能超过当前会话最大消息 id；
- 推进水位线时摘要不能为空。

### 4.3 `load()`

```python
@dataclass(frozen=True)
class HistorySnapshot:
    messages: list[dict]
    message_ids: list[int]
    summary: RollingSummary
    covered_through_id: int | None
```

`messages[i]` 对应 `message_ids[i]`。

### 4.4 `plan_trim()`

```python
@dataclass(frozen=True)
class TrimPlan:
    session_id: str
    keep_last: int
    drop_through_id: int | None
    drop_count: int
    covered_count: int
    uncovered_count: int
    uncovered_ids: tuple[int, ...]
    uncovered_messages: tuple[dict, ...]
    remaining: int
    target_covered_through_id: int | None
```

只读：

- `drop_through_id`：准备删除的最大 id；
- `covered_count`：其中已覆盖的数量；
- `uncovered_*`：需要新摘要的消息；
- `target_covered_through_id`：提交后应达到的水位线。

### 4.5 `commit_trim()`

```python
def commit_trim(
    self,
    plan: TrimPlan,
    summary: RollingSummary | None = None,
) -> None:
```

| 情况 | 行为 |
|---|---|
| 空 plan | 直接返回 |
| `uncovered_count == 0` | `summary` 必须为 `None`，只删已覆盖消息 |
| `uncovered_count > 0` | 必须提供非空摘要 |
| 混合 | 删除全部，保存摘要，水位线推进到 `drop_through_id` |

删除按 cutoff：

```sql
DELETE FROM session_messages
WHERE session_id = ? AND id <= ?
```

并校验 `rowcount == drop_count`。

---

## 5. ContextPlanner

新增 `context/planner.py`：

```python
@dataclass(frozen=True)
class ContextPlan:
    state_max_tokens: int
    summary_max_tokens: int
    summary_text_max_tokens: int
    history_max_tokens: int
    summary_tokens: int
    history_tokens: int
    kept_history: tuple[dict, ...]
    kept_message_ids: tuple[int, ...]
    dropped_history: tuple[dict, ...]
    dropped_message_ids: tuple[int, ...]
```

预算公式：

```text
fixed = system prompt + user + 2 * MESSAGE_OVERHEAD
remaining = available - fixed - safety

state_quota = min(state_max_tokens, remaining)
summary_quota = min(summary_max_tokens, remaining - state_quota)
history_quota = remaining - state_quota - summary_quota
```

历史从最新往旧选择，`assistant(tool_calls) + tool` 作为原子组。

规划阶段：

- 不调用 LLM；
- 不读数据库；
- 不渲染状态；
- 只做预算和历史选择。

---

## 6. 一轮请求的流程

```text
1. load state
2. load history
3. plan = ContextPlanner.plan(...)

4. 处理 plan.dropped_history：
   - id <= coverage：已覆盖，不需要摘要
   - id > coverage：uncovered，需要摘要

5. if uncovered:
       summary = await HistorySummarizer.update(
           history.summary,
           uncovered_messages,
       )
       HistoryStore.save_summary(
           session_id,
           summary,
           covered_through_id=max(uncovered_ids),
       )

6. built = ContextBuilder.build(
       user_input,
       state=snapshot.state,
       summary=new_summary,
       history=plan.kept_history,
       state_max_tokens=plan.state_max_tokens,
   )
```

只 build 一次；`TokenBudget.fit` 只做兜底。

如果 build 后仍有 `dropped_messages`：

- 记录指标/日志；
- 最多用 safety margin 再缩一次 history；
- 不允许循环 `build → 摘要 → build`。

---

## 7. 后续存储裁剪

```text
plan = HistoryStore.plan_trim(session_id, keep_last=N)

if plan.is_empty:
    return

if plan.uncovered_count == 0:
    HistoryStore.commit_trim(plan, summary=None)
else:
    summary = await HistorySummarizer.update(
        current_summary,
        plan.uncovered_messages,
    )
    HistoryStore.commit_trim(plan, summary)
```

已覆盖消息直接删；未覆盖消息先摘要。

---

## 8. 测试

### 8.1 `tests/unit/test_lesson20_history_store.py`（29 个）

覆盖：

- 缺失 session 返回空快照和空 message_ids；
- 消息 JSON/tool_calls 完整保留；
- 消息 id 与 messages 对齐；
- `append()` 不裁剪；
- `save_summary` 可以推进 coverage，不删消息；
- coverage 不能倒退、不能超过最大 id；
- `plan_trim` 拆分 covered/uncovered；
- covered-only 删除不需要摘要；
- 未覆盖删除必须提供摘要；
- 混合裁剪只摘要 uncovered；
- 过期 plan 冲突并回滚；
- coverage 持久化；
- StateStore/HistoryStore 共库。

### 8.2 `tests/unit/test_lesson20_context_planner.py`（9 个）

覆盖：

- state/summary/history 配额分配；
- 历史从新到旧保留；
- tool_calls 组原子；
- dropped IDs 正确；
- 摘要超配额报错；
- 空历史；
- 非法配额参数。

### 8.3 `ContextBuilder` 补充

- `state_max_tokens` 可在 build 时覆盖；
- `BuiltContext` 暴露 `dropped_messages` / `dropped_history_indices`；
- 预算裁剪后 dropped 与历史下标对齐。

---

## 9. 冒烟

```powershell
.venv\Scripts\python.exe -m scripts.lesson20_smoke
```

期望：

```text
before trim: 5
drop through: 2
drop count: 2
covered: 2
uncovered: 0
messages: 3
summary: 用户喜欢干型酒，预算 800 元。
covered_messages: 2
covered_through_id: 2
roles: ['user', 'assistant', 'user']
contents: ['推荐一款。', '试试上梅多克。', '预算改成 1000。']
state version: 1
state: {...}
clear: True
after clear: 0
```

---

## 10. 常见坑

### 10.1 `append()` 自动裁剪

不要。保留策略必须显式走 `plan_trim/commit_trim`。

### 10.2 删除前没有摘要

未覆盖消息不能删除。`commit_trim` 会拒绝空摘要。

### 10.3 coverage 倒退

`save_summary` 和 `commit_trim` 都必须保证水位线单调不减。

### 10.4 只摘要不推进 coverage

会导致下一轮再次摘要。本轮摘要必须和 `save_summary(covered_through_id=...)` 或 `commit_trim` 配套。

### 10.5 反复 build

不要 `build → 摘要 → build` 循环。先 `ContextPlanner` 规划，再 build 一次。

### 10.6 请求预算裁剪当存储裁剪

`TokenBudget.fit` 的 dropped 只影响本轮 prompt，不自动改数据库。

---

## 11. 验收清单

- [x] `context/history.py` 提供 message_ids / coverage / plan_trim / commit_trim
- [x] `context/planner.py` 提供 ContextPlanner
- [x] `append` 不裁剪
- [x] `save_summary` 可推进 coverage 但不删除
- [x] coverage 单调不减
- [x] covered-only 裁剪不需要摘要
- [x] 未覆盖裁剪必须提供摘要
- [x] build 前完成预算规划
- [x] build 只执行一次
- [x] `ContextBuilder` 暴露 dropped 信息
- [x] 全量测试通过

---

## 12. 下一课

第 20.5 课把 `ContextPlanner`、`HistoryStore`、`TurnStateUpdater` 和 `ContextBuilder` 串成 Runtime 接入设计；第 20.6 课写 TurnOrchestrator 代码。
