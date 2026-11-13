# 第 20.6 课：组件接入 Runtime（加餐）

> 模块三 · 上下文与状态（加餐）
> 前置：第 20 课（`HistoryStore` / `ContextPlanner`）、第 20.5 课（接入设计）
> 下一课：第 21 课（任务状态机）
> 预计用时：2-3 小时
> 本课性质：**实现课**

---

## 0. 开工前检查

```powershell
git switch dev
git status --short

.venv\Scripts\python.exe -m pytest tests/unit/test_lesson20_6_turn_orchestrator.py -q
# 8 passed

.venv\Scripts\python.exe -m pytest -q
# 278 passed
```

本课只接聊天路径；任务路径仍然走旧的 `PlanRunner`，留到 20.7 再处理。

---

## 1. 本课目标

把第 20.5 课定下的契约真正接进 Runtime：

```text
API /chat?session_id=...
  → AgentRuntime
    → IntentRouter
      ├─ CHAT → TurnOrchestrator
      │         ├─ begin_turn
      │         ├─ run_chat
      │         └─ finish_turn
      └─ TASK → PlanRunner（旧链路）
```

目标链路：

```text
begin_turn
  ├─ await updater.wait(session_id)
  ├─ state = await state_service.get(session_id)
  └─ history = history_store.load(session_id)

run_chat
  ├─ ContextPlanner.plan(...)
  ├─ 请求级摘要 + save_summary(...)
  ├─ ContextBuilder.build(...)
  └─ ChatResponder.answer(messages)

finish_turn
  ├─ TurnStateUpdater.update(...)
  ├─ HistoryStore.append_once(...)
  ├─ HistoryStore.plan_trim(...)
  └─ commit_trim(plan, None / summary)
```

核心不变量：

- 请求级裁剪只影响本轮 prompt，不删除数据库消息；
- `covered_through_id` 可以领先于物理删除；
- 历史追加以 `(session_id, turn_id)` 幂等；
- 状态更新失败不撤回回答；
- 历史/摘要失败不丢消息，只记录 warning。

---

## 2. 文件清单

| 文件 | 改动 |
|---|---|
| `history/store.py` | 新增 `session_turns`、`append_once()`、锁冲突重试 |
| `agent/turn_orchestrator.py` | 新增 `TurnContext`、`TurnFinishResult`、`TurnOrchestrator` |
| `agent/chat_responder.py` | 新增 `answer(messages)`；`run()` 兼容旧文本和 messages |
| `agent/runtime.py` | 接入 state/history/builder/planner/summarizer/updater；`run` 增加 `session_id` |
| `api/server.py` | `/chat` 接收可选 `session_id`，返回 `X-Session-Id` |
| `static/index.html` | 用 `localStorage` 保存并传递 `session_id` |
| `events/models.py` | `RuntimeEvent.session_id`、`STATE_UPDATE_FAILED` |
| `tests/unit/test_lesson20_history_store.py` | `append_once` 幂等、回滚、重试测试 |
| `tests/unit/test_lesson20_6_turn_orchestrator.py` | 编排、两级裁剪、失败降级、重启恢复测试 |
| `scripts/lesson20_6_smoke.py` | 两轮记忆 + 重启恢复冒烟 |

---

## 3. `HistoryStore.append_once()`

### 3.1 为什么需要它

`append()` 是低层写入，不具备幂等性。同一个 `finish_turn()` 被重复调用时，
如果直接 `append()`，会出现：

```text
turn-1 第一次：user + assistant
turn-1 第二次：user + assistant  ← 重复
```

状态更新有 `TurnStateUpdater` 的 `(session_id, turn_id)` 缓存，但历史没有。
所以历史需要一个持久化幂等入口。

### 3.2 表结构

```sql
CREATE TABLE IF NOT EXISTS session_turns (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, turn_id)
);
```

不需要给 `session_messages` 的每一行加 `turn_id`。一轮聊天只需要一个
“这一轮已经写过”的标记。

### 3.3 API

```python
def append_once(
    self,
    session_id: str,
    turn_id: str,
    messages: Sequence[dict],
) -> bool:
    """写入一轮消息；重复 turn_id 返回 False，不重复写。"""
```

语义：

| 返回值 | 含义 |
|---|---|
| `True` | 本次写入成功 |
| `False` | `(session_id, turn_id)` 已存在，跳过 |
| 抛 `HistoryStoreError` | 写入失败 |

### 3.4 事务

```text
BEGIN
  INSERT INTO session_turns(session_id, turn_id, created_at)
  ON CONFLICT(session_id, turn_id) DO NOTHING

  如果 rowcount == 0:
      COMMIT
      return False

  INSERT session_messages(...)
COMMIT
```

turn 标记和消息必须**在同一个事务里**。否则可能出现：

```text
标记写入了，消息没写入
→ 下一轮重试被判定为重复
→ 这一轮永久丢失
```

### 3.5 重试

只对暂时性 SQLite 错误重试：

```text
database is locked
database is busy
```

最多 3 次尝试，短退避。因为 `append_once()` 本身幂等，即使第一次其实已经提交，
第二次看到重复标记返回 `False`，也不会重复写消息。

不要重试：

- `TypeError`：消息不是 dict；
- `ValueError`：role 不合法；
- `session_id` / `turn_id` 为空。

### 3.6 约束

`append_once()` 要求一轮的完整消息一次性传入：

```python
append_once(
    session_id,
    turn_id,
    [user_message, assistant_message],
)
```

聊天路径正好是两条；任务路径以后也建议在回合结束时一次性写
`[user, final_answer]`。如果未来需要分多次写一轮消息，再加
`turn_id + seq`，不要现在把问题复杂化。

---

## 4. `TurnOrchestrator`

### 4.1 `TurnContext`

```python
@dataclass(frozen=True)
class TurnContext:
    session_id: str
    turn_id: str
    state: StateSnapshot
    history: HistorySnapshot
```

`HistorySnapshot` 必须包含：

```text
messages
message_ids
summary
covered_through_id
```

没有 `message_ids`，请求级裁剪无法判断哪些 dropped 消息已经进入摘要。

### 4.2 `begin_turn()`

```python
async def begin_turn(self, session_id, *, turn_id=None) -> TurnContext:
    await self.updater.wait(session_id)
    state = await self.state_service.get(session_id)
    history = await asyncio.to_thread(self.history_store.load, session_id)
    return TurnContext(...)
```

先 `wait` 是为了读到上一轮后台状态更新；然后只读一次状态和历史。

`HistoryStore` 是同步 SQLite，所以用 `asyncio.to_thread()` 包一层，避免阻塞事件循环。

### 4.3 `run_chat()`

```text
RUN_STARTED
  ↓
ContextPlanner.plan(...)
  ↓
请求级摘要（只摘要未覆盖的 dropped 消息）
  ↓
ContextBuilder.build(...)   ← 只执行一次
  ↓
ChatResponder.answer(messages)
  ↓
FINAL_ANSWER
  ↓
finish_turn(...)
  ↓
（可选）STATE_UPDATE_FAILED
  ↓
RUN_FINISHED（可带 warnings）
```

`ChatResponder` 不再自己拼 `system + user`，而是接收已组装的 messages。

### 4.4 `finish_turn()`

```text
1. TurnStateUpdater.update(session_id, turn_id, [user, assistant])
2. HistoryStore.append_once(session_id, turn_id, [user, assistant])
3. HistoryStore.plan_trim(session_id, keep_last=...)
4. covered-only: commit_trim(plan, None)
5. uncovered: summarizer.update(current_summary, plan.uncovered_messages)
6. uncovered: commit_trim(plan, summary)
```

注意：第 5 步不能直接用 `TurnContext.history.summary`，因为请求级裁剪可能已经
推进了摘要和 coverage。`finish_turn()` 在 `plan_trim()` 前重新读取摘要。

---

## 5. 请求级裁剪与存储级裁剪

### 5.1 请求级裁剪

```python
plan = planner.plan(
    user_input=user_input,
    system_prompt=builder.system_prompt,
    summary=history.summary,
    history=history.messages,
    message_ids=history.message_ids,
    budget=builder.budget,
)
```

只摘要 `id > covered_through_id` 的 dropped 消息：

```python
covered = history.covered_through_id or 0
uncovered_pairs = [
    (message_id, message)
    for message_id, message in zip(
        plan.dropped_message_ids,
        plan.dropped_history,
    )
    if message_id > covered
]

if uncovered_pairs:
    summary = await summarizer.update(
        history.summary,
        [message for _, message in uncovered_pairs],
    )
    history_store.save_summary(
        session_id,
        summary,
        covered_through_id=max(message_id for message_id, _ in uncovered_pairs),
    )
```

关键点：

- `save_summary()` 只推进 coverage，不删除消息；
- 已覆盖的 dropped 消息不再摘要；
- coverage 可以领先于物理删除；
- 这样本轮 prompt 不会丢失被裁掉的信息，下一轮也不会重复摘要。

### 5.2 存储级裁剪

```python
history_store.append_once(session_id, turn_id, [user, assistant])

plan = history_store.plan_trim(session_id, keep_last=...)
if plan.is_empty:
    return

if plan.uncovered_count == 0:
    history_store.commit_trim(plan, None)
else:
    current = history_store.load(session_id).summary
    summary = await summarizer.update(
        current,
        plan.uncovered_messages,
    )
    history_store.commit_trim(plan, summary)
```

### 5.3 两级配合

```text
本轮 prompt 丢了 m1~m3
→ 请求级摘要 m1~m3，coverage 推进到 3
→ m1~m3 仍在数据库
→ 本轮结束 append 新消息
→ 存储裁剪发现 m1~m3 已覆盖
→ commit_trim(plan, None) 直接删除
→ 不再调用 LLM 摘要
```

---

## 6. 失败处理

### 6.1 状态更新失败

`TurnStateUpdater.update()` 返回 `StateUpdateResult.failed=True` 时：

- 回答已经发出，不撤回；
- 发 `STATE_UPDATE_FAILED` 事件；
- 继续追加历史；
- 记录日志和 warning。

### 6.2 历史追加失败

`HistoryStoreError`：

- 回答已经发出，不撤回；
- 不执行裁剪（避免历史缺一轮还删旧消息）；
- 在 `RUN_FINISHED.data["warnings"]` 里报告；
- 下一轮 `begin_turn()` 仍然可以继续。

### 6.3 请求级摘要落库失败

- 本轮仍可用内存里的 summary 组装；
- 不假装 coverage 已推进；
- `finish_turn()` 重新读取数据库时看到旧 coverage；
- `plan_trim()` 会把这些消息重新摘要一次（至少一次语义）；
- 允许重复摘要，但不能丢消息。

### 6.4 存储级摘要失败

- 不调用 `commit_trim()`；
- 旧消息保留；
- 记录 warning；
- 下一轮 `plan_trim()` 会重新尝试。

---

## 7. `session_id` 接入

### 7.1 Runtime

```python
async def run(
    self,
    user_input: str,
    session_id: str | None = None,
):
```

`session_id` 为空时由 Runtime 生成：

```text
s_<run_id>
```

`run_id`、`turn_id`、`session_id` 都会进入：

- `TurnContext`；
- `RuntimeEvent.session_id`；
- 状态读写；
- 历史读写；
- 日志。

### 7.2 API

```text
GET /chat?message=...&session_id=...
```

`session_id` 可选；不传时服务端生成，并通过响应头返回：

```text
X-Session-Id: s_...
```

SSE 每个事件的 `data` 外层也带 `session_id`，前端可以保存并复用。

`static/index.html` 已经用 `localStorage` 保存 `agent_session_id`，每次请求都带上：

```javascript
const sessionId =
    localStorage.getItem("agent_session_id")
    || ("web-" + crypto.randomUUID());

localStorage.setItem("agent_session_id", sessionId);
```

### 7.3 事件字段

`RuntimeEvent` 新增：

```python
session_id: str | None = None
```

`to_dict()` 会输出：

```json
{
  "type": "final_answer",
  "run_id": "...",
  "task_id": null,
  "session_id": "session-1",
  "data": {"content": "..."}
}
```

---

## 8. 测试

### 8.1 `HistoryStore`

新增 5 个测试：

- `append_once` 同一 turn 只写一次；
- 不同 session 的相同 turn_id 互不影响；
- 消息写入失败时 turn 标记回滚；
- 暂时性锁冲突会重试；
- 空 `turn_id` 被拒绝。

`tests/unit/test_lesson20_history_store.py`：34 passed。

### 8.2 `TurnOrchestrator`

`tests/unit/test_lesson20_6_turn_orchestrator.py`，8 个测试：

- `begin_turn` 先 wait 再加载 state/history；
- `run_chat` 使用组装 messages 并追加历史；
- 请求级裁剪摘要但不删除消息；
- 存储级裁剪先摘要再删除；
- 状态失败仍回答并追加历史；
- 同一 turn 重复 `finish_turn` 不重复追加；
- 摘要落库失败降级；
- Runtime 两轮记忆 + 重启恢复。

### 8.3 全量

```powershell
.venv\Scripts\python.exe -m pytest -q
# 278 passed
```

---

## 9. 冒烟

```powershell
.venv\Scripts\python.exe -m scripts.lesson20_6_smoke
```

期望（终端中文可能因编码显示为乱码，但结构应一致）：

```text
events: ['intent_detected', 'run_started', 'final_answer', 'run_finished']
turn 1: 推荐波尔多。
history: 2
state: {"preferences":{"style":"dry"},...}
state version: 1

events: ['intent_detected', 'run_started', 'final_answer', 'run_finished']
turn 2: 可以看波尔多。
turn 2 prompt has previous user: True
history: 4

turn 3 after restart: 预算 800 的波尔多。
restart prompt has turn 1: True
history after restart: 6
final state: {"preferences":{"style":"dry"},...}
```

---

## 10. 常见坑

### 10.1 把 turn 标记和消息分开写

不要先写 `session_turns` 再单独写消息。必须同事务，否则会出现“标记成功、
消息失败”的永久缺口。

### 10.2 用 `append()` 代替 `append_once()`

`append()` 不幂等。聊天路径必须走 `append_once()`；`append()` 只保留给
低层脚本和测试。

### 10.3 状态失败就不写历史

状态和历史是两个独立事务。状态失败不应阻止历史追加，否则下一轮会缺少
上一轮对话。

### 10.4 用 `TurnContext.history.summary` 做存储级摘要

请求级裁剪可能已经推进了摘要；`finish_turn()` 必须重新读取当前摘要。

### 10.5 请求级裁剪后删消息

`save_summary()` 只推进 coverage，不删除。物理删除只发生在
`commit_trim()`。

### 10.6 再 build 一次

不要 `build → 摘要 → build`。`ContextPlanner` 固定配额，`ContextBuilder`
只 build 一次；兜底丢弃只记录 warning。

### 10.7 任务路径直接套聊天 builder

`PlanRunner._compose_answer()` 目前是 `system + 原始问题 + 任务结果`，
不满足 `ContextBuilder` 的“当前 user 最后”契约。任务路径留到 20.7。

---

## 11. 验收清单

- [x] `HistoryStore` 有 `session_turns` 和 `append_once()`
- [x] 同一 `(session_id, turn_id)` 不重复追加
- [x] turn 标记和消息同事务
- [x] 暂时性锁冲突有界重试
- [x] `TurnOrchestrator.begin_turn` 先 wait 再加载
- [x] `run_chat` 使用 `ContextPlanner` + `ContextBuilder` + `ChatResponder`
- [x] 请求级裁剪摘要未覆盖消息但不删除
- [x] 存储级裁剪区分 covered / uncovered
- [x] 状态失败仍回答并追加历史
- [x] 历史/摘要失败可观测
- [x] `session_id` 进入 Runtime、事件和 API
- [x] 两轮记忆 + 重启恢复测试通过
- [x] 全量测试通过

---

## 12. 下一课

第 21 课回到任务状态机：

```text
1. 显式 FSM + 合法转换守卫
2. 任务路径接入 TurnOrchestrator / task_builder
3. 任务结束后的状态与历史更新
4. 工具消息与最终回答的分离
```

本课只把聊天路径接进 Runtime；任务路径仍然使用旧的 `PlanRunner`。
