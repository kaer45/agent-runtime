# 第 8 课：让规划器理解工具的真实能力

> 模块二 · 模型规划接入（第 4/5 课）
> 前置：第 7 课（`IntentRouter` 已能把聊天和任务分开）
> 下一课：第 9 课用 JSON Schema 做参数级校验
> 预计用时：2-3 小时
> 本课分支：`dev`

---

## 0. 开工前检查

先确认第 7 课是干净的：

```powershell
git switch dev
git status --short
.venv\Scripts\python.exe -m pytest -q
```

期望：工作区干净，`60 passed`。

当前工具目录里只有三个工具：

```text
get_weather(city)
get_current_time()
brewing(weather, current_time)
```

第 8 课不会新增工具，也不会改调度器；我们只改「工具能力如何被描述给模型」。

---

## 1. 本课目标

把「工具清单」从两处手写拼接，升级成一份从工具 Schema 自动生成的 **`ToolCatalog`**：

```text
ToolRegistry.get_all_schemas()
        │
        ▼
   ToolCatalog
   ├─ render()          详细视图：给 TaskPlanner，含参数说明、必填/可选、默认值
   └─ render_compact()  紧凑视图：给 IntentRouter，只保留签名和能力边界
```

学完你应该能回答：

1. 为什么工具描述不能维护成一份手写的 `TOOL_DESCRIPTIONS` 字典？
2. 为什么 `ToolCatalog` 要读 `registry.get_all_schemas()`，而不是再 `inspect` 一遍函数？
3. 为什么 Planner 和 Router 需要两种视图？紧凑视图为什么仍要保留完整描述？
4. 模型把 `brewing` 用错，问题出在「模型笨」还是「工具能力没写清楚」？

**本课产出**

| 交付物 | 说明 |
|---|---|
| `tools/catalog.py` | `ToolParameter` / `ToolSpec` / `ToolCatalog` |
| `agent/planner.py` | 删除 `_format_tool()`，改用详细目录 |
| `agent/intent.py` | 改用紧凑目录 |
| `tools/builtin.py` | 补全 docstring 的能力边界和参数说明 |
| `tools/__init__.py` | 导出 `ToolCatalog` |
| `tests/unit/test_lesson08_tool_catalog.py` | 18 个测试（13 个基础 + 5 个扩展） |
| `scripts/lesson08_smoke.py` | 不调用模型、不花钱的目录冒烟脚本 |

**本课明确不做**

- 不做参数级 JSON Schema 校验（第 9 课）
- 不做「按语义检索工具」或工具分组注入（第 10 课之后）
- 不接 MCP / Skill（第 27-28 课）
- 不改 `ToolRegistry` 的 Schema 生成规则（扩展 A 例外：新增 `Literal -> enum`）
- 不改 `ToolRunner` / `TaskScheduler` / `PlanRunner`

---

## 2. 问题：Schema 里的信息，被渲染层丢掉了

`ToolRegistry.get_tool_schema()` 其实已经生成了很完整的结构。以 `get_weather` 为例：

```json
{
  "type": "function",
  "function": {
    "name": "get_weather",
    "description": "根据城市名获取当前天气信息。 返回指定城市的天气状况和气温，目前为模拟数据。",
    "parameters": {
      "type": "object",
      "properties": {
        "city": {
          "type": "string",
          "description": "城市名，使用中文或拼音，如 \"成都\" 或 \"chengdu\"。"
        }
      },
      "required": ["city"]
    }
  }
}
```

但 Planner 当前的 `_format_tool()` 只渲染：

```text
- get_weather(city: string): 根据城市名获取当前天气信息。 返回指定城市的天气状况和气温，目前为模拟数据。
```

丢掉了三样东西：

1. **参数说明**：`city` 支持中文还是拼音？模型只能猜。
2. **默认值**：以后有 `limit: int = 5` 这种可选参数，模型不知道可以省略。
3. **能力边界**：`get_weather` 只能查当前天气，不能查明天。描述里没写，模型就可能拿它回答「明天成都天气」。

Router 的问题更明显，它当前只拼 `name + description`：

```python
for name in self.registry.list_tools():
    fn = self.registry.get_tool_schema(name)["function"]
    tools.append(f"- {name}: {fn['description']}")
```

它连参数都不看，而且和 Planner 各自遍历一遍注册表。两处渲染逻辑一旦分叉，就会出现在 Router 里能看到的工具，在 Planner 里描述不一样。

**这不是模型能力问题，是上下文供给问题。** 第 8 课要做的，是把「工具真实长什么样」完整、稳定、单源地带给模型。

---

## 3. 设计决策

### 3.1 目录从 Schema 生成，不维护手写清单

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A. 手写工具说明 | 维护一个 `TOOL_DESCRIPTIONS` 字典 | 文案可控 | 加工具必改两处，迟早漂移 |
| B. 目录里重新 `inspect` 函数 | 再解析一次签名和 docstring | 不依赖 Registry | 和 Registry 的 Schema 规则重复，容易出现「目录说有、Schema 没有」 |
| C. 读 `registry.get_all_schemas()` | Schema 是唯一事实来源 | 与模型实际拿到的工具定义完全一致 | Registry 若生成错 Schema，目录也跟着错 |

**选 C。** 模型最终看到的是 `tools=[...]` 里的 JSON Schema，目录只是同一份 Schema 的「人类可读版」。如果目录另起炉灶，它描述得再漂亮，也可能和模型真正能调用的定义不一致。

这也意味着：**想改工具描述，改函数 docstring，不要改目录代码。**

### 3.2 先把 Schema 转成 `ToolSpec`，再渲染

不直接在 `render()` 里遍历 JSON：

- `ToolSpec` / `ToolParameter` 是稳定、可测试的中间层；
- 类型、必填、默认值只解析一次；
- 后面要加 `returns`、`enum`、MCP 元数据时，有地方放；
- 测试可以断言结构，而不是只对字符串做模糊匹配。

### 3.3 Planner 用详细视图，Router 用紧凑视图

| 使用方 | 需要什么 | 视图 |
|---|---|---|
| `TaskPlanner` | 参数名、类型、必填、默认值、参数说明 | `render()` |
| `IntentRouter` | 工具能做什么、不能做什么、大致签名 | `render_compact()` |

Router 不需要每个参数的完整说明，但它**需要能力边界**。所以紧凑视图保留完整 `description`，只压缩参数部分为 `city: string` / `limit: integer?`。

一个反例：如果紧凑视图只取描述的第一句话，`get_weather` 会变成「根据城市名获取当前天气信息。」，「不能查询未来预报」就丢了，Router 又会把「明天成都天气」判成 task。**可以压缩参数，不能压缩边界。**

### 3.4 目录是启动时快照

`ToolCatalog.from_registry()` 在 `TaskPlanner` / `IntentRouter` 初始化时调用一次。当前 Registry 在 Runtime 启动时注册完就不再变化，所以快照足够。

如果以后支持运行时动态注册工具（第 26 课之后），需要重建目录，或者让 `ToolCatalog` 持有 Registry 并在每次 `render()` 时刷新。**本课不提前做这个抽象。**

### 3.5 好目录的前提，是好 docstring

`ToolCatalog` 只能放大 docstring 里已有的信息。当前 `get_weather` 的 docstring 没写「不能查预报」，目录再智能也变不出来。

本课顺手把内置工具的描述补成四段：

```text
一句话说明做什么
能力边界 / 不适用场景
Args：每个参数的含义、格式、示例
Returns：返回什么（本课目录暂不渲染，但留给第 9 课和后续使用）
```

注意：docstring 里的 `Args:` 参数名必须和函数签名完全一致，否则 `docstring_parser` 取不到参数说明。

---

## 4. 接口与数据结构

### 4.1 `ToolParameter`

```python
@dataclass(frozen=True)
class ToolParameter:
    name: str
    type: str
    required: bool
    description: str = ""
    default: Any = None
    has_default: bool = False
    enum: tuple[Any, ...] = ()
```

`default` 和 `has_default` 必须分开：

- `limit: int = 5` → `has_default=True, default=5`
- `note: str | None = None` → `has_default=True, default=None`
- `city: str`（无默认） → `has_default=False, default=None`

只靠 `default is None` 判断，会把「显式默认 None」误判成「没有默认值」。

`enum` 保存 JSON Schema 的可选值；为空表示不限制。它只用于描述，不负责校验（扩展 A）。

### 4.2 `ToolSpec`

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: tuple[ToolParameter, ...] = ()

    @property
    def signature(self) -> str: ...

    def render(self) -> str: ...
    def render_compact(self) -> str: ...
```

详细渲染示例：

```text
### get_weather
描述：获取指定城市的当前天气。 只返回当前时刻的天气，不能查询未来预报或历史天气。
参数：
- city: string（必填） — 城市名，使用中文或拼音，如 "成都" 或 "chengdu"。
```

紧凑渲染示例：

```text
- get_weather(city: string): 获取指定城市的当前天气。 只返回当前时刻的天气，不能查询未来预报或历史天气。
- get_current_time(): 获取当前系统时间。 ...
```

### 4.3 `ToolCatalog`

```python
class ToolCatalog:
    def __init__(self, tools: Iterable[ToolSpec]): ...

    @classmethod
    def from_registry(cls, registry: ToolRegistry) -> "ToolCatalog": ...

    def names(self) -> list[str]: ...
    def get(self, name: str) -> ToolSpec: ...
    def render(self, names: Iterable[str] | None = None) -> str: ...
    def render_compact(self, names: Iterable[str] | None = None) -> str: ...
    def fingerprint(self) -> str: ...

    def __len__(self) -> int: ...
    def __contains__(self, name: object) -> bool: ...
```

`names` 参数是为后续「只注入相关工具」留的口子；本课不实现检索，只实现按名字筛选。

`fingerprint()` 返回目录内容的稳定短哈希，用于日志和 prompt 排查（扩展 C）。

空目录统一返回 `（无可用工具）`，不要返回空字符串——空字符串会让 prompt 里出现一个没有内容的「可用工具：」段落，模型容易困惑。

---

## 5. 实现步骤

### 第 1 步：建 `tools/catalog.py`

- 两个 dataclass；
- `ToolCatalog.from_registry()`；
- `_from_schema()` 负责 JSON → `ToolSpec`。

✅ 完成标志：`ToolCatalog.from_registry(registry).names()` 与 `registry.list_tools()` 顺序一致。

### 第 2 步：实现两种 render

- `ToolSpec.render()`：标题、描述、参数列表；无参数写「参数：无」。
- `ToolSpec.render_compact()`：`name(signature): description`。
- `ToolCatalog.render()` 用空行连接工具；`render_compact()` 用单行连接。
- 空目录返回 `（无可用工具）`。

✅ 完成标志：详细视图包含参数说明，紧凑视图只包含签名和完整描述。

### 第 3 步：处理边界

- 没有 docstring：描述写 `（无描述）`，不要抛异常；
- 参数类型是数组：`["string", "null"]` 渲染成 `string | null`；
- 重复工具名：构造时抛 `ValueError`；
- 查询不存在的工具名：抛 `KeyError`，错误信息里带上名字。

✅ 完成标志：第 6 节的 13 个基础测试都能覆盖到；扩展 A/B/C 另有 5 个测试，见第 13 节。

### 第 4 步：接入 `TaskPlanner`

- 构造时 `self.catalog = ToolCatalog.from_registry(registry)`；
- 删除 `_format_tool()`；
- `_system_prompt()` 用 `self.catalog.render()`。

✅ 完成标志：Planner 的 system prompt 里能看到：
`city: string（必填）`、`城市名，使用中文或拼音`、`不能查询未来预报`。

### 第 5 步：接入 `IntentRouter`

- 构造时同样建目录；
- `_system_prompt()` 用 `self.catalog.render_compact()`；
- 保留 `INTENT_PROMPT` 和 `INTENT_RULES`，只替换工具清单部分。

✅ 完成标志：Router 的 prompt 里有 `- get_weather(city: string):`，但没有 `### get_weather`。

### 第 6 步：补 `tools/builtin.py` 的 docstring

- `get_weather`：补「只查当前，不查预报/历史」和参数格式。
- `brewing`：补「必须同时提供天气和时间，通常来自前两个工具」。
- 不要改函数签名和模拟逻辑。

✅ 完成标志：目录详细视图能直接读出这三个工具的能力边界。

### 第 7 步：导出

`tools/__init__.py` 增加：

```python
from .catalog import ToolCatalog, ToolParameter, ToolSpec
```

✅ 完成标志：`from tools import ToolCatalog` 可用。

### 第 8 步：测试与冒烟

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson08_tool_catalog.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m scripts.lesson08_smoke
```

期望：本课 `18 passed`，全量 `78 passed`，冒烟脚本打印详细目录、紧凑目录和目录指纹。

---

## 6. 测试代码

### 6.1 `tests/unit/test_lesson08_tool_catalog.py`

> 下面是 13 个基础测试，完整可复制；扩展 A/B/C 的 5 个测试见第 13 节。

```python
import asyncio

import pytest

from agent.intent import IntentRouter
from agent.planner import TaskPlanner
from tests.fixtures.fake_llm import FakeLLM, FakeMessage, FakeToolCall
from tools.builtin import brewing, get_current_time, get_weather
from tools.catalog import ToolCatalog, ToolSpec
from tools.registry import ToolRegistry


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.register(get_weather)
    r.register(get_current_time)
    r.register(brewing)
    return r


@pytest.fixture
def catalog(registry):
    return ToolCatalog.from_registry(registry)


def search_wine(query: str, limit: int = 5) -> list[str]:
    """按关键词搜索酒款。

    只搜索本地酒库，不访问网络。

    Args:
        query: 搜索关键词，例如 "赤霞珠"。
        limit: 返回条数上限，默认 5。

    Returns:
        匹配的酒款名称列表。
    """
    return []


def undocumented(value: str) -> str:
    return value


def test_catalog_collects_tools_from_registry(catalog):
    assert catalog.names() == ["get_weather", "get_current_time", "brewing"]
    assert len(catalog) == 3
    assert "get_weather" in catalog
    assert catalog.get("get_current_time").parameters == ()


def test_catalog_reads_parameter_metadata(catalog):
    city = catalog.get("get_weather").parameters[0]

    assert city.name == "city"
    assert city.type == "string"
    assert city.required is True
    assert city.has_default is False
    assert "城市名" in city.description


def test_catalog_marks_optional_parameter_and_default():
    registry = ToolRegistry()
    registry.register(search_wine)

    spec = ToolCatalog.from_registry(registry).get("search_wine")
    limit = next(p for p in spec.parameters if p.name == "limit")

    assert limit.type == "integer"
    assert limit.required is False
    assert limit.has_default is True
    assert limit.default == 5
    assert "返回条数上限" in limit.description


def test_catalog_renders_detailed_view(catalog):
    text = catalog.render()

    assert "### get_weather" in text
    assert "获取指定城市的当前天气" in text
    assert "不能查询未来预报" in text
    assert "- city: string（必填）" in text
    assert "城市名，使用中文或拼音" in text
    assert "- current_time: string（必填）" in text
    assert "参数：无" in text


def test_catalog_renders_optional_default():
    registry = ToolRegistry()
    registry.register(search_wine)
    text = ToolCatalog.from_registry(registry).render()

    assert "- limit: integer（可选，默认 5）" in text
    assert "只搜索本地酒库" in text


def test_catalog_renders_compact_view(catalog):
    text = catalog.render_compact()

    assert "- get_weather(city: string):" in text
    assert "- get_current_time():" in text
    assert "不能查询未来预报" in text


def test_catalog_can_render_selected_tools(catalog):
    text = catalog.render(["get_current_time"])

    assert "get_current_time" in text
    assert "get_weather" not in text
    assert "brewing" not in text


def test_catalog_unknown_tool_raises(catalog):
    with pytest.raises(KeyError, match="nope"):
        catalog.render(["nope"])


def test_catalog_empty_render():
    empty = ToolCatalog([])

    assert empty.render() == "（无可用工具）"
    assert empty.render_compact() == "（无可用工具）"


def test_catalog_rejects_duplicate_names():
    with pytest.raises(ValueError, match="重复"):
        ToolCatalog([ToolSpec("dup", ""), ToolSpec("dup", "")])


def test_catalog_falls_back_when_docstring_missing():
    registry = ToolRegistry()
    registry.register(undocumented)

    text = ToolCatalog.from_registry(registry).render()

    assert "（无描述）" in text
    assert "- value: string（必填）" in text


def test_planner_prompt_uses_catalog(registry):
    planner = TaskPlanner(FakeLLM([]), registry)

    prompt = planner.build_messages("今天适合酿酒吗")[0]["content"]

    assert "### get_weather" in prompt
    assert "city: string（必填）" in prompt
    assert "城市名，使用中文或拼音" in prompt
    assert "不能查询未来预报" in prompt


def test_router_prompt_uses_compact_catalog(registry):
    response = FakeMessage(
        tool_calls=[FakeToolCall(name="classify_intent", arguments='{"intent": "task"}')]
    )
    llm = FakeLLM([response])
    router = IntentRouter(llm, registry)

    asyncio.run(router.route("今天成都天气怎么样"))

    prompt = llm.calls[0]["messages"][0]["content"]
    assert "- get_weather(city: string):" in prompt
    assert "不能查询未来预报" in prompt
    assert "### get_weather" not in prompt
```

### 6.2 这 13 个基础测试分别防什么

| 测试 | 证明 |
|---|---|
| `test_catalog_collects_tools_from_registry` | 工具集合和顺序来自 Registry，不是手写清单 |
| `test_catalog_reads_parameter_metadata` | 参数名、类型、必填、说明都被解析 |
| `test_catalog_marks_optional_parameter_and_default` | 可选参数和默认值没有丢 |
| `test_catalog_renders_detailed_view` | Planner 能拿到参数级细节和能力边界 |
| `test_catalog_renders_optional_default` | 详细视图会显示「可选，默认 5」 |
| `test_catalog_renders_compact_view` | Router 视图有签名，且保留完整边界描述 |
| `test_catalog_can_render_selected_tools` | 为后续按需注入留的口子可用 |
| `test_catalog_unknown_tool_raises` | 选错工具名不会被静默忽略 |
| `test_catalog_empty_render` | 空目录有稳定输出，不产生空 prompt 段落 |
| `test_catalog_rejects_duplicate_names` | 目录内部不会出现同名覆盖 |
| `test_catalog_falls_back_when_docstring_missing` | 没写 docstring 不会崩，且能看出缺失 |
| `test_planner_prompt_uses_catalog` | Planner 真正接入了详细目录 |
| `test_router_prompt_uses_compact_catalog` | Router 真正接入了紧凑目录，且没有退化成详细视图 |

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit/test_lesson08_tool_catalog.py -q
```

期望：基础 13 个通过；加上第 13 节的 5 个扩展测试后，本课共 `18 passed`，全量 `78 passed`。

> 当前测试是「先红后绿」：在你实现 `tools/catalog.py` 之前，这个文件会因为 `ModuleNotFoundError` 失败。这是预期，不是环境坏了。

---

## 7. 冒烟脚本

`scripts/lesson08_smoke.py`：

```python
from logging_config import setup_logging
from tools.builtin import brewing, get_current_time, get_weather
from tools.catalog import ToolCatalog
from tools.registry import ToolRegistry

setup_logging()


def main() -> None:
    registry = ToolRegistry()
    registry.register(get_weather)
    registry.register(get_current_time)
    registry.register(brewing)

    catalog = ToolCatalog.from_registry(registry)

    print("=== 工具名 ===")
    print(catalog.names())

    print("\n=== 详细目录（Planner） ===")
    print(catalog.render())

    print("\n=== 紧凑目录（Router） ===")
    print(catalog.render_compact())

    print("\n=== 目录指纹 ===")
    print(catalog.fingerprint())


if __name__ == "__main__":
    main()
```

运行：

```powershell
.venv\Scripts\python.exe -m scripts.lesson08_smoke
```

期望输出里能看到：

```text
=== 工具名 ===
['get_weather', 'get_current_time', 'brewing']

=== 详细目录（Planner） ===
### get_weather
描述：获取指定城市的当前天气。 只返回当前时刻的天气，不能查询未来预报或历史天气。
参数：
- city: string（必填） — 城市名，使用中文或拼音，如 "成都" 或 "chengdu"。
...

=== 紧凑目录（Router） ===
- get_weather(city: string): 获取指定城市的当前天气。 ...
...

=== 目录指纹 ===
55ca0e69d361
```

这个脚本不调用 LLM、不读 API key，可以随时跑。

---

## 8. 参考实现

### 8.1 `tools/catalog.py`

```python
"""工具能力目录：把 Registry 生成的 JSON Schema 转成模型可读文本。

唯一事实来源是 ToolRegistry.get_all_schemas()；本模块不维护手写工具清单。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from tools.registry import ToolRegistry


def _json_default(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _one_line(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True)
class ToolParameter:
    name: str
    type: str
    required: bool
    description: str = ""
    default: Any = None
    has_default: bool = False

    def render(self) -> str:
        marker = "必填" if self.required else "可选"
        text = f"{self.name}: {self.type}（{marker}"
        if self.has_default:
            text += f"，默认 {_json_default(self.default)}"
        text += "）"
        if self.description:
            text += f" — {_one_line(self.description)}"
        return text


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: tuple[ToolParameter, ...] = ()

    @property
    def signature(self) -> str:
        return ", ".join(
            f"{p.name}: {p.type}{'' if p.required else '?'}"
            for p in self.parameters
        )

    def render(self) -> str:
        lines = [
            f"### {self.name}",
            f"描述：{_one_line(self.description) or '（无描述）'}",
        ]
        if self.parameters:
            lines.append("参数：")
            lines.extend(f"- {p.render()}" for p in self.parameters)
        else:
            lines.append("参数：无")
        return "\n".join(lines)

    def render_compact(self) -> str:
        description = _one_line(self.description) or "（无描述）"
        return f"- {self.name}({self.signature}): {description}"


class ToolCatalog:
    def __init__(self, tools: Iterable[ToolSpec]):
        self._tools: dict[str, ToolSpec] = {}
        for spec in tools:
            if spec.name in self._tools:
                raise ValueError(f"重复的工具名: {spec.name}")
            self._tools[spec.name] = spec

    @classmethod
    def from_registry(cls, registry: ToolRegistry) -> "ToolCatalog":
        return cls(cls._from_schema(schema) for schema in registry.get_all_schemas())

    @staticmethod
    def _from_schema(schema: dict[str, Any]) -> ToolSpec:
        function = schema.get("function") or {}
        name = function.get("name")
        if not name:
            raise ValueError(f"工具 Schema 缺少 function.name: {schema!r}")

        parameters = function.get("parameters") or {}
        properties = parameters.get("properties") or {}
        required = set(parameters.get("required") or [])

        params: list[ToolParameter] = []
        for param_name, spec in properties.items():
            raw_type = spec.get("type", "any")
            if isinstance(raw_type, list):
                type_name = " | ".join(str(item) for item in raw_type)
            else:
                type_name = str(raw_type)

            params.append(
                ToolParameter(
                    name=param_name,
                    type=type_name,
                    required=param_name in required,
                    description=(spec.get("description") or "").strip(),
                    default=spec.get("default"),
                    has_default="default" in spec,
                )
            )

        return ToolSpec(
            name=name,
            description=(function.get("description") or "").strip(),
            parameters=tuple(params),
        )

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"工具目录中不存在 '{name}'")
        return self._tools[name]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def _select(self, names: Iterable[str] | None) -> list[ToolSpec]:
        if names is None:
            return list(self._tools.values())
        return [self.get(name) for name in names]

    def render(self, names: Iterable[str] | None = None) -> str:
        specs = self._select(names)
        if not specs:
            return "（无可用工具）"
        return "\n\n".join(spec.render() for spec in specs)

    def render_compact(self, names: Iterable[str] | None = None) -> str:
        specs = self._select(names)
        if not specs:
            return "（无可用工具）"
        return "\n".join(spec.render_compact() for spec in specs)
```

### 8.2 `agent/planner.py` 的改动

只改三处，协议和校验逻辑不动：

```python
from tools.catalog import ToolCatalog

# __init__ 里
self.catalog = ToolCatalog.from_registry(registry)

# 删除原来的 _format_tool()

def _system_prompt(self) -> str:
    return (
        PROTOCOL_RULES
        + "\n可用工具（由注册表自动生成；参数格式：名字: 类型，必填/可选）：\n"
        + self.catalog.render()
    )
```

原来的 `self.registry` 还要保留，`TaskConverter.validate_tools()` 仍在用它做工具名校验。

### 8.3 `agent/intent.py` 的改动

```python
from tools.catalog import ToolCatalog

# __init__ 里
self.catalog = ToolCatalog.from_registry(registry)

def _system_prompt(self) -> str:
    return (
        INTENT_PROMPT
        + self.catalog.render_compact()
        + "\n\n"
        + INTENT_RULES
    )
```

`INTENT_RULES` 里「只根据现有工具能力判断」的规则不要删，它和目录里的能力边界是配合关系。

### 8.4 `tools/builtin.py` 的 docstring

```python
async def get_weather(city: str) -> str:
    """获取指定城市的当前天气。

    只返回当前时刻的天气，不能查询未来预报或历史天气。
    目前使用模拟数据，不访问真实天气服务。

    Args:
        city: 城市名，使用中文或拼音，如 "成都" 或 "chengdu"。

    Returns:
        自然语言描述的天气信息，例如 "成都的天气是晴天，气温25℃"。

    Raises:
        ValueError: 当 city 为空字符串时抛出。
    """
    ...


async def brewing(weather: str, current_time: str) -> str:
    """根据天气和当前时间判断是否适合酿造葡萄酒。

    必须同时提供天气和时间；通常把 get_weather 与 get_current_time 的结果作为输入。
    目前为模拟逻辑，不会真的启动酿造流程。

    Args:
        weather: 天气描述，例如 "晴天" 或 "成都的天气是晴天，气温25℃"。
        current_time: 当前时间字符串，格式为 "YYYY-MM-DD HH:MM:SS"。

    Returns:
        综合天气与时间后的结论文本。
    """
    ...
```

### 8.5 `tools/__init__.py`

```python
from .catalog import ToolCatalog, ToolParameter, ToolSpec
from .registry import ToolRegistry
from .runner import ToolRunner
from .builtin import get_weather, get_current_time, brewing

__all__ = [
    "ToolCatalog",
    "ToolParameter",
    "ToolSpec",
    "ToolRegistry",
    "ToolRunner",
    "get_weather",
    "get_current_time",
    "brewing",
]
```

---

## 9. 验收清单（DoD）

- [x] `tools/catalog.py` 存在，且不包含任何手写工具名
- [x] `ToolCatalog.from_registry()` 的顺序与 `registry.list_tools()` 一致
- [x] 详细视图包含参数名、类型、必填/可选、默认值、参数说明
- [x] 紧凑视图包含签名和完整能力边界，不包含详细参数段
- [x] `get_weather` 的目录描述包含「不能查询未来预报」
- [x] `TaskPlanner` 不再有 `_format_tool()`
- [x] `IntentRouter` 不再自己遍历 `list_tools()`
- [x] 空目录、重复工具名、未知工具名都有明确行为
- [x] `pytest tests/unit/test_lesson08_tool_catalog.py -q` → 18 passed
- [x] 全量 `pytest -q` → 78 passed
- [x] `python -m scripts.lesson08_smoke` 不需要 API key
- [x] 第 5-7 课的现有测试没有被改坏

---

## 10. 常见坑

| 坑 | 现象 | 处理 |
|---|---|---|
| 目录维护成手写字典 | 新增工具后模型看不到 | 只从 `get_all_schemas()` 生成 |
| 目录里重新 `inspect` 函数 | 目录和 Schema 不一致 | Registry 已经解析过，目录只消费 Schema |
| 用 `default is None` 判断默认值 | 显式默认 `None` 被当成无默认 | 用 `has_default` |
| `Args:` 名字和签名不一致 | 参数说明为空 | 参数名逐字对齐 |
| docstring 没写能力边界 | 模型把「查当前」当「查预报」 | 在 long description 里写「不能做什么」 |
| 紧凑视图只取第一句 | 边界丢失，路由误判 | 紧凑视图保留完整 description |
| 空目录返回 `""` | prompt 出现空的「可用工具：」 | 返回 `（无可用工具）` |
| 运行时注册新工具 | 目录还是旧的 | 当前是启动快照；动态注册时重建 |
| 把 API key 写进 docstring | 目录会带进 prompt | docstring 不放密钥、内网地址等敏感信息 |
| 目录渲染顺序不稳定 | prompt 缓存命中率低 | 保持 Registry 插入顺序，不排序 |
| 目录里渲染 `Returns` 但 Schema 没有 | 模型以为有结构化返回值 | 本课只渲染 Schema 里有的字段 |

---

## 11. 扩展与思考题

1. **把 `Returns` 也纳入目录**：`docstring_parser` 能解析 `doc.returns`，但 JSON Schema 没有标准位置。你会放在 `ToolSpec.returns`，还是扩成自定义元数据？
2. **枚举值**：参数 `style: Literal["dry", "sweet"]` 会生成 `enum`，目录要不要把可选值列出来？（已实现，见 §13.1）
3. **按需注入**：工具增加到 30 个时，全量目录会撑爆 prompt。用关键词、向量检索还是让模型先选分类？
4. **目录哈希**：把 `catalog.render()` 的哈希放进日志，比较两次请求的 prompt 是否变化，为第 31 课追踪做准备。（已实现，见 §13.3）
5. **MCP 工具**：MCP Server 返回的 `inputSchema` 如何映射到 `ToolSpec`？和本地函数的 Schema 有什么差异？
6. **目录版本**：工具描述变更后，如何知道线上 prompt 用的是哪一版目录？

---

## 12. 与第 9 课的衔接

第 8 课解决「模型知道工具长什么样」，但模型仍可能传错参数：

```json
{"city": 123, "unit": "celsius"}
```

第 9 课会在**执行前**用 JSON Schema 做参数级校验：

- 必填参数缺失；
- 类型不对；
- 多余的参数；
- `from_task` 占位符要跳过校验，等上游结果回填后再校验。

分工可以记成一句话：

> **Catalog 负责「说清楚」，Validator 负责「挡错误」。**

本课先把「说清楚」做成单一事实来源，第 9 课才有稳定的 Schema 可校验。

---

## 13. 扩展实现记录（A/B/C）

> §8 的参考实现是基础版；这一节记录在基础版上额外落地的三个扩展。

### 13.1 A：`Literal` 生成 `enum`

`ToolRegistry._to_json_schema_type()` 新增对 `Literal[...]` 的识别：

```python
if origin is Literal:
    values = list(args)
    ...
    return {"type": "string", "enum": values}
```

`ToolParameter` 增加 `enum` 字段，详细视图渲染成：

```text
- style: string（必填，可选值: dry | sweet）
```

对应测试：

```text
test_registry_literal_emits_enum_and_catalog_renders_it
```

注意：这里只负责**描述**可选值，不负责校验；校验仍然留给第 9 课。

### 13.2 B：Planner 和 Router 共享同一个 Catalog

两个构造函数都增加可选参数：

```python
TaskPlanner(llm, registry, catalog=catalog)
IntentRouter(llm, registry, catalog=catalog)
```

`AgentRuntime` 只构建一次：

```python
catalog = ToolCatalog.from_registry(registry)
self.catalog = catalog
```

这样不再是「两份内容相同的快照」，而是「同一个对象」。对应测试：

```text
test_planner_and_router_can_share_one_catalog
test_runtime_shares_one_catalog_between_planner_and_router
```

### 13.3 C：目录指纹

`ToolCatalog.fingerprint()` 对详细视图和紧凑视图一起做 SHA-256，取前 12 位：

```python
payload = f"{self.render()}\n--\n{self.render_compact()}"
return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
```

`AgentRuntime` 启动时打日志：

```text
工具目录指纹: 55ca0e69d361
```

对应测试：

```text
test_catalog_fingerprint_is_stable
test_catalog_fingerprint_changes_with_contents
```

扩展后的测试总数：**18 passed**，全量 **78 passed**。
