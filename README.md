# 🍷 葡萄酒酿制 Agent (Wine Brewing Agent)

基于大语言模型（LLM）的智能体（Agent）系统。它可以自动分析用户意图，规划任务依赖图（DAG），并发调用外部工具，并通过 SSE（Server-Sent Events）实时推送思考与执行过程。

## ✨ 核心特性

- **🧠 自动任务规划**：基于 LLM 的 Function Calling，自动解析用户需求并拆分工具调用。
- **🔀 DAG 任务调度**：内置 Kahn 算法的拓扑排序与环检测，支持带依赖的任务并发执行，将总耗时优化至关键路径长度。
- **⚡ 事件驱动与实时流**：全链路异步生成器（Async Generator），配合 SSE 技术，让前端实时看到 Agent 的“思考过程”和工具执行结果。
- **🔌 数据流与结果传递**：支持 `{"from_task": "T1"}` 参数占位符，自动将前置任务的输出注入后续任务的输入中。
- **🛡️ 容错与重试**：工具执行支持超时控制、重试退避，任务图支持失败原因多源 BFS 传播。
- **🏗️ 模块化架构**：严格遵循单向依赖原则，事件、工具、任务、Agent、API 彻底解耦。

## 📂 项目结构

```text
agent-runtime/
├── agent/                  # Agent 核心层
│   ├── llm.py              # LLM 客户端封装 (AsyncOpenAI)
│   ├── model_tool_loop.py  # 模型与工具交互的主循环
│   └── runtime.py          # 全局单例装配器 (AgentRuntime)
├── api/                    # 接口层
│   └── server.py           # FastAPI 路由与 SSE 流式响应
├── events/                 # 事件流定义层
│   └── models.py           # EventType 枚举与 RuntimeEvent 数据类
├── tasks/                  # 任务调度层
│   ├── models.py           # Task 数据结构与状态管理
│   └── scheduler.py        # 事件驱动 DAG 调度器 (环检测/并发)
├── tools/                  # 工具执行层
│   ├── builtin.py          # 具体的业务工具函数
│   ├── registry.py         # 工具注册与 JSON Schema 生成
│   └── runner.py           # 工具执行引擎 (超时/重试/并发)
├── static/                 # 前端测试页面
│   └── index.html
├── main.py                 # 服务启动入口
├── requirements.txt        # 依赖清单
├── .env                    # 本地环境变量配置 (不提交到 Git)
└── .env.example            # 环境变量配置模板
```

## 🚀 快速启动

### 1. 准备环境

确保你安装了 Python 3.10+。建议使用虚拟环境：

```bash
# 创建并激活虚拟环境
python -m venv .venv

# Windows:
.venv\Scripts\activate

# macOS/Linux:
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置环境变量

复制环境变量模板，并填入你的大模型 API 配置（兼容 OpenAI 接口格式的任意模型，如 DeepSeek、通义千问等）：

```bash
# 复制模板文件 (Windows 使用 copy .env.example .env)
cp .env.example .env
```

编辑 `.env` 文件，填入你的真实密钥：

```env
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL_ID=deepseek-chat
```

### 3. 启动服务

```bash
python main.py
```

服务将运行在 `http://127.0.0.1:8000`。

## 🧪 测试与体验

### 方式一：浏览器直接体验（推荐）

直接打开浏览器访问：
`http://127.0.0.1:8000/chat?message=今天波尔多适合酿酒吗`

你将在浏览器中看到源源不断的 JSON 事件流（SSE）：

```json
data: {"type": "model_started", "run_id": "...", "data": {"message": "正在分析用户请求..."}}
data: {"type": "model_finished", "run_id": "...", "data": {"tool_call_count": 2}}
data: {"type": "task_started", "task_id": "T1", "data": {"tool_name": "get_weather", ...}}
data: {"type": "task_success", "task_id": "T1", "data": {"result": "波尔多晴天25℃"}}
...
data: {"type": "final_answer", "run_id": "...", "data": {"content": "综合结果：天气是..."}}
```

### 方式二：访问 Web UI

打开 `http://127.0.0.1:8000/`，我们提供了一个原生的测试网页来可视化这些流式事件。

### 方式三：API 文档

FastAPI 自带 Swagger 文档：`http://127.0.0.1:8000/docs`

## ⚙️ 核心组件设计说明

1. **事件驱动（Event-Driven）**：`TaskScheduler` 不再等待整层任务完成，而是使用 `asyncio.wait(..., FIRST_COMPLETED)`。一旦某个任务完成，立刻释放其下游依赖，实现最短关键路径耗时。
2. **无状态 `ToolRunner`**：只负责接收工具名和参数，带超时重试执行。不感知 `Task` 对象，彻底消除 `tools` 和 `tasks` 模块的循环依赖。
3. **单例 `AgentRuntime`**：Web 服务启动时实例化一次，所有请求共享 `LLM` 客户端（复用连接池）和 `ToolRegistry`，极大地降低了并发时的开销。
