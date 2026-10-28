import asyncio

from tools.builtin import recommend_wine_pairing
from tools.catalog import ToolCatalog
from tools.registry import ToolRegistry
from tools.runner import ToolRunner


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(recommend_wine_pairing)
    return registry


def test_recommend_wine_pairing_schema_has_complex_types():
    schema = make_registry().get_tool_schema("recommend_wine_pairing")
    parameters = schema["function"]["parameters"]
    properties = parameters["properties"]

    assert parameters["required"] == ["occasion", "styles"]
    assert properties["occasion"]["enum"] == ["dinner", "party", "gift"]

    assert properties["styles"]["type"] == "array"
    assert properties["styles"]["items"]["enum"] == ["dry", "sweet", "sparkling"]

    assert properties["guest_count"]["type"] == "integer"
    assert properties["guest_count"]["default"] == 2
    assert properties["budget"]["type"] == "number"
    assert properties["include_notes"]["type"] == "boolean"


def test_recommend_wine_pairing_catalog_renders_complex_parameters():
    text = ToolCatalog.from_registry(make_registry()).render()

    assert "occasion: string（必填，可选值: dinner | party | gift）" in text
    assert "styles: array（必填）" in text
    assert "每个元素只能是 dry、sweet 或 sparkling" in text
    assert "guest_count: integer（可选，默认 2）" in text
    assert "budget: number（可选，默认 null）" in text
    assert "include_notes: boolean（可选，默认 false）" in text


def test_recommend_wine_pairing_runs_with_all_arguments():
    result = asyncio.run(
        ToolRunner(registry=make_registry()).run(
            "recommend_wine_pairing",
            {
                "occasion": "party",
                "styles": ["dry", "sparkling"],
                "guest_count": 20,
                "budget": 1000,
                "include_notes": True,
            },
        )
    )

    assert result["success"] is True
    assert "派对" in result["result"]
    assert "干型、起泡" in result["result"]
    assert "20 人" in result["result"]
    assert "1000 元" in result["result"]
    assert "配餐建议" in result["result"]


def test_recommend_wine_pairing_uses_optional_defaults():
    result = asyncio.run(
        ToolRunner(registry=make_registry()).run(
            "recommend_wine_pairing",
            {"occasion": "gift", "styles": ["sweet"]},
        )
    )

    assert result["success"] is True
    assert "送礼" in result["result"]
    assert "甜型" in result["result"]
    assert "共 2 人" in result["result"]
    assert "总预算" not in result["result"]
    assert "配餐建议" not in result["result"]
