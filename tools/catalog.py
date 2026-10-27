"""工具能力目录（Tool Catalog）。

职责：
1. 从 ``ToolRegistry`` 已生成的 JSON Schema 中提取工具元数据；
2. 转成与具体模型、提示词无关的 ``ToolSpec``；
3. 按使用场景渲染成 Planner 详细视图或 Router 紧凑视图。

设计约束：
- 不在本模块维护工具名、参数名或说明，避免与函数签名 / docstring 漂移；
- 只读 Schema，不执行工具，不产生副作用；
- 目录是创建时的快照，Registry 后续变更不会自动反映，需要重新调用
  ``ToolCatalog.from_registry()``。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from tools.registry import ToolRegistry


def _json_default(value: Any) -> str:
    """把默认值渲染成稳定的 JSON 文本。

    ``ensure_ascii=False`` 保留中文；``sort_keys=True`` 让
    ``{"b": 2, "a": 1}`` 始终输出成相同顺序，避免相同默认值在 prompt
    中时好时坏，影响缓存和测试。
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _one_line(text: str) -> str:
    """把多行 docstring 压成单行，避免破坏目录的列表结构。"""
    return " ".join(text.split())


@dataclass(frozen=True)
class ToolParameter:
    """一个工具参数的渲染模型。

    Attributes:
        name: 参数名，必须与函数签名一致。
        type: JSON Schema 类型名，如 string / integer；联合类型渲染为
            ``"string | null"``。
        required: 是否必填。
        description: 来自 docstring ``Args`` 的参数说明。
        default: 默认值；仅当 ``has_default`` 为 True 时有意义。
        has_default: 是否真的声明了默认值。不能只靠 ``default is None``
            判断，因为 ``None`` 本身也可能是合法默认值。
        enum: 可选值列表；来自 JSON Schema 的 ``enum``，为空表示不限制。
    """

    name: str
    type: str
    required: bool
    description: str = ""
    default: Any = None
    has_default: bool = False
    enum: tuple[Any, ...] = ()

    def render(self) -> str:
        """渲染成一行参数说明。

        示例：``city: string（必填） - 城市名，使用中文或拼音``。
        """
        marker = "必填" if self.required else "可选"
        text = f"{self.name}: {self.type}（{marker}"
        extras: list[str] = []
        if self.has_default:
            extras.append(f"默认 {_json_default(self.default)}")
        if self.enum:
            extras.append("可选值: " + " | ".join(str(value) for value in self.enum))
        if extras:
            text += "，" + "，".join(extras)
        text += "）"
        if self.description:
            text += f" - {_one_line(self.description)}"
        return text


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的能力说明。

    它是 Registry Schema 与 prompt 文本之间的中间层：先把 JSON 解析成
    结构化的参数，再按 Planner / Router 的不同需要渲染。
    """

    name: str
    description: str
    # 用 tuple 而不是 list，保证 frozen dataclass 真正不可变；
    # 末尾的 ... 表示“任意数量”，不是“每个元素都是 str”。
    parameters: tuple[ToolParameter, ...] = ()

    @property
    def signature(self) -> str:
        """紧凑签名，如 ``get_weather(city: string)``；可选参数带 ``?``。"""
        return ", ".join(
            f"{p.name}: {p.type}{'' if p.required else '?'}"
            for p in self.parameters
        )

    def render(self) -> str:
        """渲染详细视图，供 ``TaskPlanner`` 使用。

        包含完整描述、参数类型、必填 / 可选、默认值和参数说明。
        无参数时显式写「参数：无」，避免模型误以为目录被截断。
        """
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
        """渲染紧凑视图，供 ``IntentRouter`` 使用。

        参数只保留名字和类型，但 ``description`` 保留完整内容：路由需要
        知道工具的能力边界（例如「不能查预报」），不能为了省 token
        把边界截掉。
        """
        description = _one_line(self.description) or "（无描述）"
        return f"- {self.name}({self.signature}): {description}"


class ToolCatalog:
    """工具目录：按注册顺序保存 ``ToolSpec``，并负责选择与渲染。

    目录是创建时的快照；它不持有 Registry，也不会在 Registry 变更后
    自动刷新。
    """

    def __init__(self, tools: Iterable[ToolSpec]):
        """用一组工具规格构建目录。

        Args:
            tools: 工具规格；顺序会被保留，用于生成稳定的 prompt。

        Raises:
            ValueError: 出现同名工具时。Registry 本身不允许重名，这里再挡
                一层，避免手工构造 ``ToolSpec`` 时静默覆盖。
        """
        self._tools: dict[str, ToolSpec] = {}
        for spec in tools:
            if spec.name in self._tools:
                raise ValueError(f"重复的工具名: {spec.name}")
            self._tools[spec.name] = spec

    @classmethod
    def from_registry(cls, registry: ToolRegistry) -> "ToolCatalog":
        """从 ``ToolRegistry`` 构建目录。

        这是唯一推荐的入口：Schema 已经由 Registry 统一生成，本方法只做
        JSON -> ``ToolSpec`` 的适配，不重新 inspect 函数。

        Args:
            registry: 已注册工具的 Registry。

        Returns:
            按 Registry 注册顺序排列的 ``ToolCatalog``。
        """
        return cls(cls._from_schema(schema) for schema in registry.get_all_schemas())

    @staticmethod
    def _from_schema(schema: dict[str, Any]) -> ToolSpec:
        """把单个 function schema 转成 ``ToolSpec``。

        Args:
            schema: OpenAI function calling 格式的单个工具 Schema。

        Returns:
            结构化的工具规格。

        Raises:
            ValueError: Schema 缺少 ``function.name``。
        """
        function = schema.get("function") or {}
        name = function.get("name")
        if not name:
            raise ValueError(f"工具 Schema 缺少 function.name: {schema!r}")

        parameters = function.get("parameters") or {}
        properties = parameters.get("properties") or {}
        # JSON Schema 的 required 是数组，转成 set 便于逐个参数判断。
        required = set(parameters.get("required") or [])

        params: list[ToolParameter] = []
        for param_name, spec in properties.items():
            # type 可能是字符串，也可能是 ["string", "null"] 这样的联合类型。
            raw_type = spec.get("type", "any")
            if isinstance(raw_type, list):
                type_name = " | ".join(str(item) for item in raw_type)
            else:
                type_name = str(raw_type)

            # enum 可能不存在，也可能是空列表；统一转成 tuple 便于不可变比较。
            raw_enum = spec.get("enum")
            enum_values = (
                tuple(raw_enum)
                if isinstance(raw_enum, (list, tuple))
                else ()
            )

            params.append(
                ToolParameter(
                    name=param_name,
                    type=type_name,
                    required=param_name in required,
                    description=(spec.get("description") or "").strip(),
                    default=spec.get("default"),
                    # 必须看 key 是否存在，不能写 default is not None：
                    # 显式默认 None 也是一个默认值。
                    has_default="default" in spec,
                    enum=enum_values,
                )
            )

        return ToolSpec(
            name=name,
            description=(function.get("description") or "").strip(),
            parameters=tuple(params),
        )

    def names(self) -> list[str]:
        """返回工具名列表，顺序与注册顺序一致。"""
        return list(self._tools)

    def get(self, name: str) -> ToolSpec:
        """按名字取工具规格。

        Raises:
            KeyError: 名字不在目录中。
        """
        if name not in self._tools:
            raise KeyError(f"工具目录中不存在 '{name}'")
        return self._tools[name]

    def __len__(self) -> int:
        """目录中的工具数量。"""
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        """支持 ``"get_weather" in catalog``。"""
        return name in self._tools

    def _select(self, names: Iterable[str] | None) -> list[ToolSpec]:
        """把可选的 names 过滤成 ``ToolSpec`` 列表。

        names 为 None 表示全量；传入名字时保持调用方给的顺序。未知名字
        由 ``get()`` 抛 ``KeyError``，不静默跳过。
        """
        if names is None:
            return list(self._tools.values())
        return [self.get(name) for name in names]

    def render(self, names: Iterable[str] | None = None) -> str:
        """渲染详细目录，供 ``TaskPlanner`` 使用。

        Args:
            names: 只渲染指定工具；None 表示全部。

        Returns:
            多个工具之间用空行分隔；空目录返回固定占位文本。
        """
        specs = self._select(names)
        if not specs:
            return "（无可用工具）"
        return "\n\n".join(spec.render() for spec in specs)

    def render_compact(self, names: Iterable[str] | None = None) -> str:
        """渲染紧凑目录，供 ``IntentRouter`` 使用。

        每个工具一行；names 语义与 ``render()`` 相同。
        """
        specs = self._select(names)
        if not specs:
            return "（无可用工具）"
        return "\n".join(spec.render_compact() for spec in specs)

    def fingerprint(self) -> str:
        """返回目录内容的稳定短指纹，用于日志和 prompt 排查。

        同时覆盖详细视图和紧凑视图：任一视图的描述发生变化，指纹都会变。
        只取前 12 位十六进制字符，足够在日志里区分版本，不用于安全场景。
        """
        payload = f"{self.render()}\n--\n{self.render_compact()}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
