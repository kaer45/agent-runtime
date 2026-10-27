# 学习阻塞点记录

> 记录学习中真正卡住的问题，按课次/时间追加，方便以后回看和避免重复踩坑。
> 每条包含：现象 → 根因 → 解决 → 验证 → 教训。
> 新增记录请复制文末模板。

## 快速索引

| 日期 | 课次 | 阻塞点 | 状态 |
|---|---|---|---|
| 2026-10-26 | 环境 | 沙箱下 git 无法写 `.git` | 已解决（切完全权限） |
| 2026-10-26 | 第 5 课 | DeepSeek 400：thinking 模式不支持强制 tool_choice | 已解决 |
| 2026-10-26 | 第 5 课 | 工具参数名 `time` vs `current_time` | 已解决（prompt 补参数） |
| 2026-10-27 | 第 7 课 | 同一句话两次路由结果不同（chat / task） | 已解决（规则 + temperature=0） |
| 2026-10-27 | 第 7 课 | 日志不输出、业务日志被 HTTP 日志淹没 | 已解决（logging_config） |
| 2026-10-27 | 第 7 课 | 新增 `intent_detected` 导致 2 个 runtime 测试失败 | 已解决 |
| 2026-10-27 | 第 7 课 | `temperature` 参数与 `FakeLLM` 不匹配 | 已解决 |

---

## 环境问题（与课程代码无关）

### 2026-10-26 · git 无法创建分支 / 提交

- **现象**：`git switch -c dev` 报 `cannot lock ref 'refs/heads/dev' ... Permission denied`，但工作区根目录可以写。
- **根因**：DSH 沙箱以低完整性（Low Integrity）运行，而 `.git` 目录是普通完整性（Medium）。Windows 的「低完整性不能写高完整性」规则直接拒绝，和普通 ACL 权限是两回事。
- **尝试过**：修复 ACL（无效）、给 `.git` 打低完整性标记（需要提权，且只解决一半）。
- **最终解决**：把会话切到「完全权限」（full access），git 正常；之前为修权限做的 ACL/标签改动已撤销。
- **教训**：涉及 `.git` 写操作（切分支、commit）时，优先用完全权限会话；不要在 ACL 上耗时间。

---

## 第 5 课：让模型按协议生成任务计划

### 5.1 · 400 Thinking mode does not support this tool_choice

- **现象**：planner 用 `tool_choice` 强制模型调用 `submit_task_plan`，冒烟直接报：

  ```text
  openai.BadRequestError: Error code: 400 -
  {'error': {'message': 'Thinking mode does not support this tool_choice'}}
  ```

- **根因**：`deepseek-flash` 默认开启思考模式（thinking enabled，effort 默认 high）；DeepSeek API 明确规定 thinking 模式下不支持 `required` 和「指定函数」这两种 `tool_choice`。
- **解决**：规划请求关闭思考模式。

  ```python
  # agent/llm.py
  if thinking is not None:
      kwargs["extra_body"] = {
          "thinking": {"type": "enabled" if thinking else "disabled"}
      }

  # agent/planner.py
  response = await self.llm.chat(..., thinking=False)
  ```

- **验证**：冒烟通过，能拿到 T1/T2/T3 计划。
- **补充**：thinking 模式 + `tools` 还要求后续请求把 `reasoning_content` 原样回传，否则同样 400；本课直接关闭 thinking，绕开这一层复杂度。
- **教训**：`FakeLLM` 只验证逻辑，不验证厂商 API 约束；新参数先跑冒烟。

### 5.2 · 工具参数名错了：`time` vs `current_time`

- **现象**：冒烟输出的 T3 是：

  ```json
  "arguments": {
    "weather": {"from_task": "T1"},
    "time": {"from_task": "T2"}
  }
  ```

  `TaskPlan` 校验通过，但真正执行时会：

  ```text
  TypeError: brewing() got an unexpected keyword argument 'time'
  ```

- **根因**：system prompt 里只给了 `- brewing: 根据天气和当前时间...`，没有给参数名和类型，模型只能按语义猜。
- **解决**：把工具目录渲染成「名字(参数: 类型)」，参数名来自 `ToolRegistry.get_tool_schema()`：

  ```text
  - brewing(weather: string, current_time: string): 根据天气和当前时间判断...
  ```

  实现见 `TaskPlanner._format_tool()`；冒烟后 `current_time` 稳定出现。
- **后续**：prompt 是软约束，参数级硬校验在第 9 课补；在第 9 课之前，计划合法不代表能执行。
- **教训**：「协议合法」和「工具可调用」是两回事，中间隔着参数校验。

---

## 第 7 课：区分普通对话与任务规划

### 7.1 · 同一句话两次路由结果不同（chat / task）

- **现象**：同一个输入：

  ```text
  今天我在成都出去外面吃饭了，天气特别好，明天适合出去吃饭吗？
  ```

  第一次走 `chat`，第二次走 `task`；第二次还规划出了：

  ![](../../static/images/第7节测试图2.png)

  ```text
  T1 get_weather
  T2 get_current_time
  T3 brewing
  ```

  `brewing` 和“出去吃饭”毫无关系。

- **根因**：
  1. 分类器没有设置 `temperature`，默认采样；同一个 prompt 两次可能给出不同 `intent`。
  2. 输入本身两可：既有个人经历/闲聊，又提到成都、天气、明天。
  3. 工具只有 `get_weather`（当前天气），没有天气预报；prompt 没有明确“工具做不到就选 chat”。
  4. 规划器被“至少一个任务”约束，只能硬选一个工具，于是选中了 `brewing`。

- **解决**：
  - 规则预筛只处理高置信度的闲聊；
  - `IntentRouter` 和 `TaskPlanner` 请求加 `temperature=0`；`LLM.chat` 默认改为 `None`，只对显式调用生效；
  - 分类 prompt 增加“工具能力边界”：未来预报/实时新闻等现有工具做不到时选 chat；
  - 增加“不要为了让工具‘有用’而把无关问题判成 task”；
  - 分类失败统一回退 `chat`。

- **验证**：同一句话多跑几次，路由稳定；聊天路径截图：

  ![第 7 课：聊天路径直接回答「曹操的儿子叫什么」](../../static/images/第7节测试图.png)

  这条请求没有 `plan_*` / `task_*`，直接 `final_answer`，说明意图分流生效。

- **教训**：**LLM 分类器不是确定性的**；规则、temperature、prompt 边界、回退策略要一起用。

### 7.2 · 日志不输出、业务日志被 HTTP 日志淹没

- **现象**：`logger.info` 完全没有输出；打开日志后又被 `httpx2` 的每条 HTTP 200 刷屏，看不到意图和任务完成。
- **根因**：
  - 只有 `main.py` 里配置了 `basicConfig`；
  - `scripts/*.py` 直接运行，根 logger 还是默认 WARNING；
  - uvicorn `reload=True` 的子进程不会执行 `main.py`；
  - `httpx` / `httpx2` 的 INFO 日志和业务日志混在一起。
- **解决**：
  - 新增 `logging_config.py`，在 `main.py`、`api/server.py`、`scripts/*` 统一切调用 `setup_logging()`；
  - INFO 下把 `httpx` / `httpx2` / `httpcore` 压到 WARNING；
  - 增加路由意图、任务完成（带耗时）、执行汇总、最终回答完成等日志。
- **验证**：

  ```text
  [INFO] agent.runtime: 路由意图: task (source=llm, rule=-)
  [INFO] tasks.scheduler: 任务完成 T2 (工具: get_current_time) 耗时 2.01s
  [INFO] tasks.scheduler: 任务完成 T1 (工具: get_weather) 耗时 3.01s
  [INFO] agent.plan_runner: 任务执行结束: 成功=2, 失败=0
  ```

### 7.3 · 新增 `intent_detected` 导致 runtime 测试失败

- **现象**：全量测试从 `38 passed` 变成 `36 passed, 2 failed`。
- **根因**：事件序列多了一条 `INTENT_DETECTED`，原测试期望 3 条事件，且 `final_answer` 的下标从 `events[1]` 变成 `events[2]`。
- **解决**：更新 `test_runtime_routes_chat` / `test_runtime_routes_task` 的期望序列，并补：
  - `intent_detected.data` 的 `intent/source/rule/reason`；
  - 所有事件 `run_id` 一致；
  - 规则命中时只有 1 次 LLM 调用。
- **验证**：`60 passed`。

### 7.4 · `temperature` 参数与 `FakeLLM` 不匹配

- **现象**：给 `IntentRouter` / `TaskPlanner` 加 `temperature=0` 后，12 个测试失败：

  ```text
  TypeError: FakeLLM.chat() got an unexpected keyword argument 'temperature'
  ```

  分类调用被 `except Exception` 吞掉后回退 chat，导致 task 相关测试也连锁失败。

- **根因**：`LLM.chat` 新增了参数，但测试替身 `tests/fixtures/fake_llm.py` 的 `chat` 签名没同步。
- **解决**：给 `FakeLLM.chat` 增加 `temperature=None`，并在 `calls` 里记录。
- **验证**：`60 passed`。
- **教训**：**改 LLM 客户端签名时，必须同步所有测试替身**；否则失败会伪装成“路由逻辑错”。

### 7.5 · 顺手修的两个小问题

- `agent/__init__.py` 的 `__all__` 写了 `ChatResponder / Intent / IntentRouter`，但没有 import；`from agent import ChatResponder` 会失败。已补全。
- `runtime.py` 里 `decision.reason[:100]`，当模型返回 `"reason": null` 会 `TypeError`；已改成 `str(decision.reason or "")[:100]`。

### 7.6 · 本课设计结论

- 规则文件独立：`agent/chat_rules.py` 只做字符串匹配，不依赖 `Intent` / LLM。
- 规则只产出 `chat`，任务判断永远交给 LLM；失败回退 `chat`。
- `intent_detected` 由 `AgentRuntime` 发，`run_id` 统一生成后传给两个 runner。
- 结构化调用（路由、规划）用 `temperature=0`；聊天回答保留模型默认温度。

---

## 待复习

- **模拟测试**：`FakeLLM` 的 `responses`/`calls`、pytest fixture、`asyncio.run` 目前能看懂但不够熟；第 6 课接入 Runtime 后再回看。
- **Google 风格 docstring**：关键函数（如 `plan()`、`__init__()`）用 `Args` / `Returns` / `Raises`，简单内部函数一句话即可。
- **pytest fixture 生命周期**：为什么每个测试都拿到新的 registry。

## 已解决的概念疑问

- **为什么 `plan()` 成功后不把本轮对话 append 进 `messages`？**
  `messages` 只是本次规划的临时草稿。失败时需要它是因为还要发起下一次请求；成功时直接返回 `TaskPlan`，后面不再调用模型，append 没有消费者。失败路径的 `assistant(tool_calls) + tool(error)` 是给「下一次请求」看的。

---

## 新增记录模板

```markdown
### YYYY-MM-DD · 第 N 课 · 标题

- **现象**：
- **根因**：
- **解决**：
- **验证**：
- **教训/后续**：
```
