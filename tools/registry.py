import inspect
import json
import logging
from typing import Any, Callable, Dict, Literal, Union, get_args, get_origin

from docstring_parser import parse as parse_docstring

logger = logging.getLogger(__name__)

_TYPE_MAP: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    type(None): "null",
}

def _to_json_schema_type(tp: Any) -> dict[str, Any]:
    """把 Python 类型注解递归转换成 JSON Schema 的类型描述。"""
    if tp is inspect.Parameter.empty:
        return {"type": "string"}

    if tp in _TYPE_MAP:
        return {"type": _TYPE_MAP[tp]}

    origin = get_origin(tp)
    args = get_args(tp)

    # Literal["dry", "sweet"] 既约束类型，也约束可选值。
    # 只生成 type 会丢掉 enum，模型就不知道只能填这两个值。
    if origin is Literal:
        values = list(args)
        if not values:
            return {"type": "string"}

        value_types = {
            _TYPE_MAP.get(type(value), "string")
            for value in values
        }
        json_type = (
            next(iter(value_types))
            if len(value_types) == 1
            else sorted(value_types)
        )
        schema: dict[str, Any] = {"type": json_type}
        schema["enum"] = values
        return schema

    if origin is list:
        item_tp = args[0] if args else str
        return {"type": "array", "items": _to_json_schema_type(item_tp)}

    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _to_json_schema_type(non_none[0])
        return {
            "type": [
                _to_json_schema_type(a).get("type", "string")
                for a in non_none
            ]
        }

    if origin is dict or tp is dict:
        schema: dict[str, Any] = {"type": "object"}
        if len(args) == 2:
            schema["additionalProperties"] = _to_json_schema_type(args[1])
        return schema

    return {"type": "string"}

def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False

def _parse_doc(func: Callable) -> tuple[str, dict[str, str]]:
    """解析 docstring，提取 (工具描述, {参数名: 参数描述})。"""
    raw = inspect.getdoc(func) or ""
    if not raw:
        return "", {}

    doc = parse_docstring(raw)
    parts = [p for p in (doc.short_description, doc.long_description) if p]
    tool_desc = " ".join(parts).strip()

    param_docs = {
        p.arg_name: (p.description or "").strip()
        for p in doc.params
        if p.arg_name
    }
    return tool_desc, param_docs


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Callable] = {}

    def register(self, func: Callable, *, replace: bool = False) -> None:
        """注册一个工具函数。"""
        if not callable(func):
            raise TypeError(f"Expected callable, got {type(func).__name__}")
        
        tool_name = func.__name__
        if tool_name in self._tools and not replace:
            raise ValueError(
                f"Tool '{tool_name}' is already registered. "
                f"Pass replace=True to override."
            )
        self._tools[tool_name] = func

    def get(self, name: str) -> Callable:
        """根据工具名获取工具。"""
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' not found.")
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def get_tool_schema(self, name: str) -> dict:
        """生成单个工具的 JSON Schema。"""
        func = self.get(name)
        signature = inspect.signature(func)
        tool_desc, param_docs = _parse_doc(func)

        if not tool_desc:
            logger.warning("Tool '%s' has no docstring; LLM may mis-select it", name)

        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []

        for param_name, param in signature.parameters.items():
            if param_name == "self" or param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue

            prop = _to_json_schema_type(param.annotation)

            desc = param_docs.get(param_name)
            if desc:
                prop["description"] = desc

            if param.default is inspect.Parameter.empty:
                required.append(param_name)
            elif _json_safe(param.default):
                prop["default"] = param.default
            else:
                logger.debug("Tool '%s' param '%s' default is not JSON-serializable", name, param_name)

            properties[param_name] = prop

        return {
            "type": "function",
            "function": {
                "name": name,
                "description": tool_desc,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def get_all_schemas(self) -> list[dict]:
        return [self.get_tool_schema(name) for name in self._tools]