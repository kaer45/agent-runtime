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
