import asyncio
from typing import Literal

import pytest

from agent.intent import IntentRouter
from agent.planner import TaskPlanner
from agent.runtime import AgentRuntime
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


def pour_wine(style: Literal["dry", "sweet"], bottles: int = 1) -> str:
    """按指定风格倒酒。

    Args:
        style: 酒款风格，只能是 dry 或 sweet。
        bottles: 倒几瓶，默认 1。

    Returns:
        模拟的倒酒结果。
    """
    return f"{style} x {bottles}"


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


def test_registry_literal_emits_enum_and_catalog_renders_it():
    registry = ToolRegistry()
    registry.register(pour_wine)

    schema = registry.get_tool_schema("pour_wine")["function"]["parameters"]["properties"]
    assert schema["style"]["type"] == "string"
    assert schema["style"]["enum"] == ["dry", "sweet"]

    catalog = ToolCatalog.from_registry(registry)
    style = catalog.get("pour_wine").parameters[0]
    assert style.enum == ("dry", "sweet")

    text = catalog.render()
    assert "- style: string（必填，可选值: dry | sweet）" in text


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


def test_planner_and_router_can_share_one_catalog(registry):
    catalog = ToolCatalog.from_registry(registry)

    planner = TaskPlanner(FakeLLM([]), registry, catalog=catalog)
    router = IntentRouter(FakeLLM([]), registry, catalog=catalog)

    assert planner.catalog is catalog
    assert router.catalog is catalog


def test_runtime_shares_one_catalog_between_planner_and_router():
    runtime = AgentRuntime(llm=FakeLLM([]))

    assert runtime.task_runner.planner.catalog is runtime.catalog
    assert runtime.router.catalog is runtime.catalog


def test_catalog_fingerprint_is_stable(catalog):
    fingerprint = catalog.fingerprint()

    assert fingerprint == catalog.fingerprint()
    assert len(fingerprint) == 12
    assert all(ch in "0123456789abcdef" for ch in fingerprint)


def test_catalog_fingerprint_changes_with_contents(catalog):
    other = ToolCatalog([ToolSpec("other", "另一个工具")])

    assert catalog.fingerprint() != other.fingerprint()
