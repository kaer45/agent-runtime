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
| 2026-10-27 | 第 8 课 | 工具目录两处渲染，参数说明和能力边界丢失 | 已解决（ToolCatalog） |
| 2026-10-27 | 第 8 课 | `Literal` 只生成 `type`，没生成 `enum` | 已解决 |
| 2026-10-27 | 第 8 课 | Planner / Router 各自构建目录快照 | 已解决（共享实例） |
| 2026-10-28 | 第 8 课遗留 | `float \| None` 被渲染成 `string` | 已解决（UnionType） |
| 2026-10-28 | 环境 | `tools/` 子目录在受限沙箱下无法写/删 | 已解决（完全权限） |
| 2026-10-29 | 第 10 课 | 按单条消息裁剪会把 tool_call / tool 拆开 | 已解决（原子组 + 校验） |
| 2026-10-29 | 第 10 课 | 并行完成顺序与 tool 消息顺序不是一回事 | 已明确（按 tool_calls 排序） |
| 2026-10-29 | 第 10 课 | token 估算不等于模型真实 token | 已明确（估算 + 安全余量） |
| 2026-10-29 | 第 10 课 | 本地协议校验只覆盖工具序列 | 已明确（剩余交给 API） |
| 2026-10-30 | 第 13 课 | StateDelta 操作语义：dict/list/状态机实体怎么设计 API | 已明确 |
| 2026-10-30 | 第 13 课 | 校验分层：内部不变量 vs 上下文语义 | 已明确 |
| 2026-10-30 | 第 13 课 | Extractor fail fast 与 Planner 修复循环的取舍 | 已明确 |
| 2026-10-31 | 第 14-15 课 | StateStore 返回 `None`、SessionRegistry 返回空状态：两层语义 | 已明确 |
| 2026-10-31 | 第 15 课 | 锁表只增不减：删除锁的竞态与代价 | 已明确 |
| 2026-10-31 | 第 15 课 | 无缓存不等于一致性：`load` 只是快照 | 已明确 |
| 2026-10-31 | 第 15 课 | 存储层切换需要 `Protocol`，异步存储会改变接口 | 已明确 |

---

## 环境问题（与课程代码无关）

### 2026-10-26 · git 无法创建分支 / 提交

- **现象**：`git switch -c dev` 报 `cannot lock ref 'refs/heads/dev' ... Permission denied`，但工作区根目录可以写。
- **根因**：DSH 沙箱以低完整性（Low Integrity）运行，而 `.git` 目录是普通完整性（Medium）。Windows 的「低完整性不能写高完整性」规则直接拒绝，和普通 ACL 权限是两回事。
- **尝试过**：修复 ACL（无效）、给 `.git` 打低完整性标记（需要提权，且只解决一半）。
- **最终解决**：把会话切到「完全权限」（full access），git 正常；之前为修权限做的 ACL/标签改动已撤销。
- **教训**：涉及 `.git` 写操作（切分支、commit）时，优先用完全权限会话；不要在 ACL 上耗时间。

### 2026-10-28 · workspace-write 下 `tools/` 子目录无法写/删

- **现象**：准备第 8 课材料时，临时实现的 `tools/catalog.py` 需要撤掉；`Remove-Item` 报 `Access to the path ... is denied`。同一会话里工作区根目录可以正常新建/删除文件，但 `tools/` 下不行。
- **根因**：DSH 的受限沙箱对该子目录的写入授权不完整；不是文件只读属性，也不是 Python 进程占用。`tools/` 的 ACL 缺少沙箱进程需要的授权，而工作区根目录有。
- **尝试过**：`Remove-Item -Force`、`cmd /c del`、`git checkout` 恢复文件，均被拒绝；没有手动改 ACL。
- **最终解决**：把这一次清理命令切到「完全权限」执行，恢复 4 个源文件并删除临时 `catalog.py`；没有留下 ACL 改动。
- **验证**：`git status --short` 只剩预期的教程和测试文件。
- **教训**：如果工作区根目录可写、某个子目录却稳定报 Access denied，先怀疑沙箱授权，不要反复试删除；清理临时文件时直接申请一次完全权限。

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

## 第 8 课：让规划器理解工具的真实能力

### 8.1 · 工具目录两处手写拼接，参数说明和能力边界丢失

- **现象**：Planner 和 Router 各自遍历 Registry 拼字符串：

  ```text
  Planner: - get_weather(city: string): 根据城市名获取当前天气信息...
  Router:  - get_weather: 根据城市名获取当前天气信息...
  ```

  Planner 只用了参数类型和必填，丢掉了参数说明、默认值；Router 连参数签名都没有。同一个工具在两处描述不一致。

- **根因**：渲染逻辑有两份，且都只消费了 `ToolRegistry` Schema 的一部分。Schema 里明明已经有 `description`、`default`、`enum` 等字段，却没有被带进 prompt。

- **解决**：新增 `ToolCatalog`，统一从 `registry.get_all_schemas()` 生成 `ToolSpec`：

  - `render()`：详细视图，给 Planner，包含参数名、类型、必填/可选、默认值、参数说明；
  - `render_compact()`：紧凑视图，给 Router，保留签名和完整能力边界；
  - Planner / Router 不再自己遍历 Registry。

- **验证**：
  - `18 passed`，全量 `78 passed`；
  - Planner prompt 包含 `city: string（必填）`、`城市名，使用中文或拼音`、`不能查询未来预报`；
  - Router prompt 包含 `- get_weather(city: string):`，但不包含 `### get_weather`。

- **教训**：**目录不是第二份工具定义，而是 Schema 的另一种渲染**。一旦两处各自维护描述，就一定会漂移。

### 8.2 · `Literal` 只生成 `type`，没生成 `enum`

- **现象**：参数写成 `Literal["dry", "sweet"]` 时，生成的 Schema 只有：

  ```json
  {"type": "string"}
  ```

  模型不知道只能填 `dry` / `sweet`，目录也显示不出可选值。

- **根因**：`_to_json_schema_type()` 只处理了基础类型、`list`、`Union`、`dict`，其他类型统一回退成 `string`。`Literal` 的 `get_origin` 不在已处理分支里。

- **解决**：增加 `Literal` 分支，生成 `type + enum`；`ToolParameter` 增加 `enum`，详细视图渲染成：

  ```text
  style: string（必填，可选值: dry | sweet）
  ```

- **验证**：`test_registry_literal_emits_enum_and_catalog_renders_it` 通过。

- **教训**：类型只说明「是什么」，`enum` 才说明「能取什么」。做工具目录时不能只搬 `type`。

### 8.3 · `float | None` 被渲染成 `string`（第 8 课遗留）

- **现象**：给新工具写 `budget: float | None = None`，Schema 和目录都显示成：

  ```text
  budget: string（可选，默认 null）
  ```

  类型完全错了；第 9 课如果用这份 Schema 校验，字符串预算也会被放行。

- **根因**：Python 3.10+ 的 `X | None` 是 `types.UnionType`，而 `typing.Optional[X]` 是 `typing.Union`。代码只判断了后者，前者落到了 `return {"type": "string"}`。

- **解决**：

  ```python
  if origin is Union or origin is types.UnionType:
      ...
  ```

- **验证**：`test_recommend_wine_pairing_schema_has_complex_types` 断言 `budget` 是 `number`；新工具测试 `4 passed`，基线 `82 passed`。

- **教训**：Python 3.10 之后有两种 union 写法，测试可选数值参数时一定要看生成的 Schema，不要只看函数注解。

### 8.4 · Planner / Router 各自构建目录快照

- **现象**：两边都调用 `ToolCatalog.from_registry(registry)`，内容相同，但不是同一个对象。当前 Registry 静态不变所以没出错，但：

  - 重复解析一遍 Schema；
  - 如果以后动态注册工具，两个快照可能版本不一致；
  - 目录指纹也变成两份。

- **根因**：构造函数默认各自建目录，没有注入点。

- **解决**：

  - `TaskPlanner` / `IntentRouter` 增加可选关键字参数 `catalog`；
  - `AgentRuntime` 只构建一次，`self.catalog` 保存并传给两边；
  - 启动时打印 `catalog.fingerprint()`。

- **验证**：

  ```python
  assert runtime.task_runner.planner.catalog is runtime.catalog
  assert runtime.router.catalog is runtime.catalog
  ```

- **教训**：**「同一来源」不等于「同一实例」**。静态场景下内容一致就够了，但只要以后会变更，就应该注入同一个快照。

### 8.5 · docstring 没写能力边界，目录也变不出来

- **现象**：`get_weather` 的描述只说「获取当前天气」，没有明确「不能查未来预报」。Router 仍然可能把「明天成都天气」判成 task。

- **根因**：`ToolCatalog` 只能放大 docstring 里已有的信息；docstring 不写限制，目录再智能也无法推断。

- **解决**：在 docstring 的 long description 里补：

  ```text
  只返回当前时刻的天气，不能查询未来预报或历史天气。
  ```

  紧凑视图保留完整 description，不截成第一句。

- **验证**：Router prompt 包含「不能查询未来预报」；第 7 课的天气路由测试仍然通过。

- **教训**：**目录的上限是 docstring**。想让模型选得准，先把「做什么、什么时候不该用、参数格式、返回什么」写清楚。

### 8.6 · 本课设计结论

- `ToolRegistry` 负责 Python 签名 + docstring → JSON Schema；`ToolCatalog` 只负责 Schema → 两种 prompt 视图。
- 工具名/参数结构由函数签名决定，工具解释由 docstring 决定；不维护第三份手写目录。
- Planner 用详细视图，Router 用紧凑视图；紧凑视图可以压缩参数，但不能删能力边界。
- 目录是启动时快照；需要动态注册时重建，或注入新的 Catalog。
- `enum`、共享实例、目录指纹是第 8 课的扩展，不改变「Schema 是唯一事实来源」这个原则。

---

## 第 10 课：上下文预算管理

### 10.1 · 按单条消息裁剪会把 tool_call / tool 拆开

- **现象**：最初的 `TokenBudget.fit()` 按单条消息选择保留/丢弃。遇到：

  ```text
  0 assistant(tool_calls=[call_1])
  1 tool(tool_call_id=call_1)
  ```

  预算不够时，可能只保留 `assistant`，也可能只保留 `tool`。结果是：

  ```text
  assistant(tool_calls) 没有 tool 结果
  ```

  或：

  ```text
  tool 消息前面没有 assistant tool_calls
  ```

  两种都可能被 API 直接拒绝。

- **根因**：预算算法只看到「单条消息的 token 成本」，没有看到 `tool_calls[].id` 和 `tool_call_id` 的配对关系。

- **解决**：

  - 新增 `_message_groups()`；
  - `assistant(tool_calls)` 与其后连续的 `tool` 消息合并成一个原子组；
  - 组成本 = 组内 token 之和；
  - 组要么全保留，要么全丢弃；
  - `system` / 最新 user 所在组受保护。

- **验证**：
  - `test_fit_keeps_tool_call_and_tool_result_together`
  - `test_fit_does_not_leave_orphan_tool_messages`
  - 本课 `18 passed`，全量 `112 passed`。

- **教训**：**预算裁剪不只是 token 问题，也是消息协议问题**。不能为了省 token 产生非法消息序列。

### 10.2 · 并行完成顺序和 tool 消息顺序不是一回事

- **现象**：并行执行会「先完成先返回」，例如 `call_2` 先完成，但 `assistant.tool_calls` 的顺序是 `[call_1, call_2]`。这时如果把完成顺序直接当成 tool 消息顺序，就可能和目标协议不一致。

- **根因**：把「执行顺序」和「序列化顺序」混在一起了。执行可以并发乱序，但发给模型的消息应该稳定。

- **解决**：
  - 执行层继续并发；
  - 结果按 `tool_call_id` 建 map；
  - 构造 tool 消息时按 `assistant.tool_calls` 的顺序生成；
  - `validate_message_sequence()` 保留严格顺序校验，作为最终边界。

- **验证**：
  - `test_validate_message_sequence_rejects_wrong_tool_order`
  - `test_validate_message_sequence_requires_assistant_after_tools`
  - 当前测试覆盖「拒绝错序」；真正的重排逻辑等接入 chat loop 时放在消息构造处，不放进 `TokenBudget`。

- **教训**：**`tool_call_id` 负责语义匹配，数组顺序负责兼容性**。并发执行不要求按完成顺序返回，但发给模型前必须规范化。

### 10.3 · token 估算不等于模型真实 token

- **现象**：`estimate_tokens()` 用「中文 1 字 1 token，英文约 4 字符 1 token」估算；它和厂商 tokenizer、聊天模板、tool schema 的真实计数都可能不同。

- **根因**：第 10 课不想引入 `tiktoken` 等依赖，只需要判断「哪条消息大、超了多少」，不是精确计费。

- **解决**：
  - `TokenCounter` 做成 `Callable[[str], int]`，测试注入 `len`；
  - 生产环境可替换成厂商 tokenizer；
  - 预留 `reserve_output`，并建议再留 10%–20% 安全余量；
  - 当前只统计 messages，工具 Schema 的 token 还没算，接入 LLM 时要注意。

- **验证**：预算逻辑用 `counter=len` 的测试完全确定；真实 tokenizer 只影响估算精度，不影响裁剪逻辑。

- **教训**：**预算是规划，不是计费**。估算器可以替换，但低估会导致上下文溢出，必须留余量。

### 10.4 · 本地协议校验只覆盖工具序列

- **现象**：`validate_message_sequence()` 能拒绝孤儿 tool、缺少 tool 结果、tool 结果错序、tool 后不是 assistant，但它不校验：

  - 多模态 `content` 的 part；
  - `developer` 角色；
  - tool schema 的完整结构；
  - 所有厂商的聊天模板差异。

- **根因**：完整 OpenAI 消息 Schema 很大，本课只需要保证预算裁剪不会破坏工具调用。

- **解决**：
  - 只校验和 tool_calls 相关的结构不变量；
  - 超出范围的交给 API 或后续 `ContextBuilder`；
  - 文档里明确这是「工具序列校验」，不是「完整协议校验」。

- **验证**：6 个 `validate_message_sequence` 测试覆盖合法组、孤儿 tool、缺失结果、错序、tool 后非 assistant、`fit()` 入口拒绝非法序列。

- **教训**：本地校验要明确边界；不要声称「保证完整 OpenAI 协议」，只能说「保证本模块关心的不变量」。

### 10.5 · 本课设计结论

- 预算裁剪的单位不是「消息」，而是「消息组」；工具组必须原子化。
- system 和最新 user 是不可裁剪的；其余按新到旧尝试。
- 执行可以并发乱序，消息必须按 `tool_calls` 顺序规范化。
- token 计数是可注入的估算，预算必须留安全余量。
- 本地校验只覆盖工具序列，完整协议最终仍以 API 校验为准。

---

## 第 13 课：自动提取状态变更

### 13.1 · StateDelta 的 API 由数据语义决定，不是容器类型

- **背景**：设计 `StateDelta` 时先问“这个字段是 dict 还是 list”，但只按容器类型选 API 不够。`constraints` 是 list，业务上是集合；`goals` 也是 list，业务上是带状态机的实体集合。
- **结论**：

  | 数据角色 | 例子 | 合适的操作 |
  |---|---|---|
  | 映射（按 key 覆盖） | `preferences` / `facts` / `progress` | `set` / `remove` |
  | 集合（无序、按值去重） | `constraints` | `add` / `remove` |
  | 状态机实体集合 | `goals` | `add` + `change_status` |
  | 序列（顺序有意义） | messages、执行步骤 | `append` / `insert` / `move` |
  | 图结构 | DAG tasks | `add/remove` 之外还要校验依赖和环 |

- **本项目的落地**：
  - `preferences_set` / `facts_set` / `progress_set`：dict 的 upsert，按 key 覆盖；
  - `constraints_add` / `constraints_remove`：自由文本集合的 add/remove，不提供 `constraints_set`；
  - `goals_add` / `goals_complete` / `goals_abandon`：新增实体 + 领域专用状态转换；
  - 当前 `goals_add` 只能新增 `active` 目标，不能“新增即完成”；如果业务需要，后续让 `goals_add` 带初始 `status`，或允许 `add + complete` 同一目标。
- **为什么 `constraints` 没有 `constraints_set`**：
  - `_set` 的准确语义是“按 key 覆盖”，`constraints` 是自由文本，没有稳定 key；
  - 如果 `constraints_set` 表示整表替换，模型必须完整复述所有旧约束，漏一条就丢一条；
  - 修改一条约束的自然表示是“`constraints_remove` 旧原文 + `constraints_add` 新原文”，既增量又可审计；
  - 只有把约束改成 `dict[str, str]`（如 `budget_max=800`）时，`constraints_set` 才有明确的 key 语义。
- **教训**：**API 服从数据角色、访问模式和一致性需求，不是“看到 list 就 add/remove”**。容器类型只是第一层线索。

### 13.2 · 校验分四层，StateDelta 只做前两层

- **背景**：最初把校验分成“语法校验 / 语义校验”两层。这个二分方向对，但“内部数据一致性”其实已经是语义，只是局部语义；叫“语法”会掩盖边界。
- **结论**：

  | 层 | 负责 | 例子 |
  |---|---|---|
  | 1. Schema / shape | 类型、必填、未知字段 | `extra="forbid"`、`dict[str, Any]` |
  | 2. 内部不变量（intrinsic） | 同一份数据内部不能矛盾 | 同一个 key 不能同时 set/remove；不能重复 |
  | 3. 上下文语义（contextual） | 需要外部状态才能判断 | 目标是否存在、状态转换是否合法、预算是否合理 |
  | 4. 执行/策略语义 | 工具、权限、并发、图 | 工具参数 Schema、DAG 环、审批 |

- **本项目的映射**：
  - `StateDelta`：第 1、2 层；不接收 `SessionState`，因此不做“目标是否存在”这类判断；
  - `StateService`（第 16 课）：第 3 层，负责应用、版本、冲突和目标匹配；
  - `TaskPlan`：第 1、2 层，外加一部分第 3 层（依赖存在、`from_task` 引用、环检测）；
  - `ToolArgumentValidator` / `TaskDAG` / 权限层：第 4 层。
- **注意 `TaskPlan` 不是“只做语法校验”**：当前 `TaskPlan.model_validator` 已经检查重复任务 ID、依赖存在、`from_task` 引用，并直接构造 `TaskDAG` 做环检测。它把“模型契约”和“图语义”耦合在一个 validator 里；第 22 课做预检时，可以再拆成 `PlanValidator` 链（依赖 → 引用 → DAG → 参数）。
- **教训**：**“内部一致性”是局部语义，不是语法**。StateDelta 只保证“这份 delta 自身说得通”；“它对当前 SessionState 说不说得通”必须交给持有状态的上层。

### 13.3 · Fail fast 与修复循环：由关键路径和恢复价值决定

- **背景**：`TaskPlanner` 校验失败会把错误回填给模型重试；`StateExtractor` 则一次失败就抛 `StateExtractionError`。两者不是随意选择。
- **结论**：
  - **Fail fast**：错误在边界立刻暴露，不静默返回空 delta；
  - **非阻塞**：调用方不 `await`，或把提取放到后台任务，失败不影响主回答；
  - 两者正交：`StateExtractor` 现在做到 fail fast，但还没做到非阻塞；非阻塞是第 19 课调用方的策略。
- **Planner 值得修复的原因**：
  - 在用户等待的关键路径上；没有合法计划就无法执行；
  - JSON / Schema 错误通常可恢复，把错误回填给模型有实际收益；
  - 修复必须有界（当前 `max_repairs=2`），否则延迟和成本失控。
- **Extractor 选择 fail fast 的原因**：
  - 不在当前回答的关键路径上，失败可以跳过或后台重试；
  - 空 delta 是正常结果（“这一轮没有状态变化”），不能和失败混为一谈；
  - 如果静默返回空 delta，状态会悄悄变旧，比抛错更难排查。
- **异步提取的配套条件**（第 19 课）：
  1. 后台任务的异常必须被捕获/记录，避免 “Task exception was never retrieved”；
  2. 同一 session 的 delta 要按轮次串行应用，避免后写覆盖先写；
  3. 需要版本号/幂等键，防止重复应用；
  4. 处理“读己之写”：下一轮读状态前，要么等待前一轮 apply，要么明确接受最终一致；
  5. 提取失败要进日志/指标，不能只靠用户发现状态没更新。
- **教训**：**fail fast 不等于阻塞，非阻塞也不等于吞错**。是否重试，取决于“在不在关键路径、错误是否可恢复、有没有降级路径、延迟预算够不够”。

### 13.4 · 本课设计结论

- `StateDelta` 是“变更建议”，不是“新状态”；提取器不修改 `SessionState`。
- 操作语义跟着数据角色走：映射用 set/remove，集合用 add/remove，状态机实体用 add/transition。
- `StateDelta` 只做 Schema + 内部不变量；上下文语义交给 `StateService`。
- `StateExtractor` 失败 fail fast；是否异步、是否重试由上层决定。
- 异步提取不是“fire-and-forget”，必须配套顺序、版本、幂等和可观测。

---

## 第 14-15 课：状态存储与多会话并发

### 14.1 · `StateStore.load()` 返回 `None`，`SessionRegistry.load()` 返回空状态

- **现象**：同一个“查状态”动作，两层返回值不同：
  - `StateStore.load()` 不存在时返回 `None`；
  - `SessionRegistry.load()` 不存在时返回 `SessionState()`。
- **根因**：两层契约不同。
  - `StateStore` 是持久化适配器，要如实回答“这一行有没有”；
  - `SessionRegistry` 是会话生命周期入口，要回答“当前能用什么状态”；
  - 新会话是正常路径，不是异常，空状态是合理的领域默认值。
- **解决**：
  - `StateStore`：`None` 表示不存在，损坏/读取失败抛 `StateStoreError`；
  - `SessionRegistry` 只把 `None` 映射成空状态，不吞 `StateStoreError`；
  - 需要区分“从未保存”和“已保存但内容为空”时用 `exists()`。
- **注意**：
  - `SessionRegistry.load()` 返回空状态**不会创建数据库行**；
  - `exists()` + `load()` 不是原子的；要判断“这次是不是新建”，应在会话锁内做，或提供 `get_or_create()`；
  - 如果觉得 `load` 名字有歧义，可以拆成 `load_or_default()` / `load()`。
- **教训**：**存储层保留歧义，管理层给默认值**。`load` 不是“一定读到”，而是“读不到就给一个可用的初始状态”。

### 15.1 · 锁表只增不减

- **现象**：`SessionRegistry._locks` 为每个访问过的 `session_id` 创建一把 `asyncio.Lock`；`delete(session_id)` 只删状态，不删锁，锁表在进程生命周期内只增不减。
- **根因**：
  - 删除锁时如果还有持有者/等待者，新请求会发现表里没有锁，重新创建一把；
  - 同一个 session 于是出现两把锁，两个任务可以同时进入临界区；
  - `asyncio.Lock` 没有公开的等待者计数，当前也没有引用计数，无法判断“现在删是否安全”。
- **代价**：
  - 锁表大小 = 进程内需要加锁的 distinct session 数；
  - 每个锁对象很小，但大量一次性 session 会持续占内存；
  - `delete()` 不释放锁；重启进程才清空；
  - 如果 session_id 由用户输入，随机 ID 可能变成无界内存增长点。
- **后续方案**：
  - 引用计数：`acquire` 进入加一、退出减一，计数为 0 且无人持有时再删；要求所有路径只走 `acquire`，不能暴露裸 `lock()`；
  - 固定分片锁：`hash(session_id) % N`，内存有上限，但不同 session 可能伪共享；
  - TTL/LRU：只淘汰空闲且无等待者的锁，实现更复杂；
  - 不建议只用 `WeakValueDictionary`：锁被任务持有时 map entry 可能先消失，仍会出现两把锁。
- **教训**：**锁的生命周期和状态的生命周期不是一回事**。当前先保证正确性，锁表增长作为已知限制；等会话量上来再做引用计数或分片。

### 15.2 · 无缓存不等于一致性，`load` 只是快照

- **现象**：容易把“`load` 每次从 SQLite 读、不共享缓存”理解成“保证一致性、不会脏读”。
- **根因**：几个概念被混在一起：
  - **无缓存**：避免两个请求共享同一个可变 `SessionState`，避免进程内缓存与 DB 不一致；
  - **脏读**：读到未提交数据；SQLite 默认隔离已经避免，和无缓存不是同一件事；
  - **丢失更新**：两个请求各自 `load` 再 `save`，后写覆盖先写；`load` 单独不解决；
  - **跨进程一致性**：`asyncio.Lock` 只管当前进程，多 worker 无效。
- **解决**：
  - 同一会话的读-改-写必须走 `acquire()` 或 `update()`；
  - `load()` 只提供“读已提交快照”，不提供跨调用的快照隔离；
  - `save()` 是裸写入口，调用方绕过锁仍然会丢更新；如果要在 API 上强制，应该把 `save` 私有化，只暴露 `update`；
  - 跨进程/多 worker 需要第 16 课的版本号/乐观锁，或外部锁。
- **验证**：`test_same_session_updates_are_serialized` 用 10 个并发 increment 证明锁内 RMW 不丢更新；但把 `save` 放到锁外，测试就会退化。
- **教训**：**无缓存解决“共享对象”，锁/事务解决“并发一致性”**；不要用一句“不会脏读”把两者混过去。

### 15.3 · 存储层可切换，但需要接口和 async 边界

- **现象**：说“`StateStore` 只是存储层，以后可以换”，但当前 `SessionRegistry` 直接依赖具体 `StateStore`。
- **根因**：没有抽象接口；SQLite 实现细节（同步、连接、SQL）和上层耦合。
- **解决**：
  - 定义 `StateRepository` Protocol：`initialize/save/load/exists/delete`；
  - SQLite 实现改名为 `SQLiteStateStore`，`SessionRegistry` 依赖 Protocol；
  - 换 Redis/Postgres 时，注意存储 API 可能变成 async，`SessionRegistry.load/save` 也要跟着 async；
  - 同步 SQLite 在 async 路由里会阻塞事件循环；当前单行操作很快，后续可用 `asyncio.to_thread` 或 `aiosqlite`。
- **教训**：**“可以换存储”不是口号，接口要先抽出来**；同步换异步不是换实现，而是改调用契约。

### 15.4 · 本课设计结论

- `StateStore` 管持久化，`SessionRegistry` 管单进程会话并发；两层语义不同。
- 每个 `session_id` 一把锁；`_locks_guard` 只保护“锁表创建”，不保护业务临界区。
- 锁表只增不减是当前正确性优先的取舍；未来用引用计数/固定分片解决。
- `load()` 不共享缓存，但只是快照；一致性由 `acquire`/`update` 和数据库事务共同保证。
- 多进程/多 worker 下 `asyncio.Lock` 无效，版本号/乐观锁留给第 16 课。

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
