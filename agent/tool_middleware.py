"""执行侧 middleware：把模型的 ``tool_calls`` 变成可回填的 tool 消息。

校验顺序（protocol §4.9）：

```text
registry → exposed_tools → 白名单 → 参数 schema → 执行
```

前四步失败都**合成失败 tool 消息继续**，不抛异常；错误码写在消息的
``error`` 字段里：

```text
TOOL_NOT_FOUND       registry 里没有
TOOL_NOT_EXPOSED     本轮 context 没暴露
TOOL_NOT_ALLOWED     白名单不允许
INVALID_ARGUMENTS    参数校验失败
```

只执行本轮暴露的 ``exposed_tools``：在白名单里、但被 Context 因预算 /
``max_tools`` 丢弃的工具不允许执行，统一返回 ``TOOL_NOT_EXPOSED``。

执行本身的失败（超时、异常、工具自己返回 ``success: false``）原样透传
runner 的结果，同样补一条失败 tool 消息。

并发：所有 tool_call 并发执行，结果按**输入顺序**写回（``asyncio.gather``
保序），每条消息都带对应的 ``tool_call_id``。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Sequence

from tasks.validator import ArgumentValidationError, ToolArgumentValidator
from tools.registry import ToolRegistry

TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
TOOL_NOT_EXPOSED = "TOOL_NOT_EXPOSED"
TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
INVALID_ARGUMENTS = "INVALID_ARGUMENTS"

_MISSING = object()


@dataclass(frozen=True)
class _Call:
    tool_call_id: str
    name: str
    arguments: Any


class ToolMiddleware:
    """按 exposed / 白名单 / schema 三层校验后执行工具调用。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        runner: Any,
        allowed_tools: frozenset[str] | set[str] | None = None,
        validator: ToolArgumentValidator | None = None,
        timeout: float | None = None,
    ):
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry 必须是 ToolRegistry")
        if not callable(getattr(runner, "run", None)):
            raise TypeError("runner 必须提供异步 run(name, arguments)")

        if allowed_tools is not None:
            if not isinstance(allowed_tools, (frozenset, set)):
                raise TypeError("allowed_tools 必须是 frozenset[str] 或 None")
            if not all(
                isinstance(name, str) and name for name in allowed_tools
            ):
                raise TypeError("allowed_tools 的元素必须是非空字符串")

        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(
                timeout, (int, float)
            ):
                raise TypeError("timeout 必须是正数或 None")
            if timeout <= 0:
                raise ValueError("timeout 必须大于 0")

        self.registry = registry
        self.runner = runner
        self.allowed_tools = (
            None if allowed_tools is None else frozenset(allowed_tools)
        )
        self.validator = (
            ToolArgumentValidator(registry) if validator is None else validator
        )
        self.timeout = timeout

    # ------------------------------------------------------------------ 入口

    async def execute(
        self,
        tool_calls: Sequence[Any],
        *,
        exposed_tools: Sequence[str | dict],
    ) -> list[dict]:
        """执行本轮全部 tool_call，返回与输入顺序一致的 tool 消息。

        ``exposed_tools`` 来自 ``ContextResult.tools``：可以传工具名，也可以
        直接传 schema（取 ``function.name``）。
        """
        calls = _parse_calls(tool_calls)
        exposed = _exposed_names(exposed_tools)

        if not calls:
            return []

        messages = await asyncio.gather(
            *(self._run_one(call, exposed) for call in calls)
        )
        return list(messages)

    # ------------------------------------------------------------------ 单个调用

    async def _run_one(self, call: _Call, exposed: frozenset[str]) -> dict:
        if not self.registry.has(call.name):
            return _rejected(
                call, TOOL_NOT_FOUND, f"工具 {call.name} 未注册"
            )

        if call.name not in exposed:
            return _rejected(
                call,
                TOOL_NOT_EXPOSED,
                f"工具 {call.name} 本轮未暴露，不允许执行",
            )

        if self.allowed_tools is not None and call.name not in self.allowed_tools:
            return _rejected(
                call, TOOL_NOT_ALLOWED, f"工具 {call.name} 不在白名单内"
            )

        arguments, error = _parse_arguments(call.arguments)
        if error is not None:
            return _rejected(call, INVALID_ARGUMENTS, error)

        try:
            self.validator.validate_arguments(call.name, arguments)
        except ArgumentValidationError as exc:
            return _rejected(call, INVALID_ARGUMENTS, str(exc))

        payload = await self._invoke(call, arguments)
        return _payload(call, payload)

    async def _invoke(self, call: _Call, arguments: dict) -> dict:
        try:
            pending = self.runner.run(call.name, arguments)
            if self.timeout is None:
                result = await pending
            else:
                result = await asyncio.wait_for(pending, self.timeout)
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"工具 {call.name} 执行超时（{self.timeout}s）",
            }
        except Exception as exc:  # noqa: BLE001 - 执行失败必须回填消息
            return {
                "success": False,
                "error": f"工具 {call.name} 执行异常：{exc}",
            }

        if isinstance(result, dict):
            return result
        return {"success": True, "result": result}


# ---------------------------------------------------------------------- 解析


def _parse_calls(tool_calls: Sequence[Any]) -> list[_Call]:
    if isinstance(tool_calls, (str, bytes)):
        raise TypeError("tool_calls 必须是序列，不能是字符串")

    try:
        items = list(tool_calls)
    except TypeError as exc:
        raise TypeError("tool_calls 必须是序列") from exc

    return [_parse_call(index, item) for index, item in enumerate(items)]


def _parse_call(index: int, call: Any) -> _Call:
    call_id = _field(call, "id")
    if not isinstance(call_id, str) or not call_id.strip():
        raise ValueError(f"tool_calls[{index}] 缺少非空 id")

    function = _field(call, "function")
    if function is _MISSING or function is None:
        raise ValueError(f"tool_calls[{index}] 缺少 function")

    name = _field(function, "name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"tool_calls[{index}] 缺少非空 function.name")

    return _Call(
        tool_call_id=call_id,
        name=name,
        arguments=_field(function, "arguments", ""),
    )


def _field(obj: Any, name: str, default: Any = _MISSING) -> Any:
    """兼容 SDK 对象和消息里的 dict 两种 tool_call 形态。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _parse_arguments(value: Any) -> tuple[dict, str | None]:
    """把 arguments 解析成 dict；失败返回 ``(空 dict, 错误信息)``。"""
    if value is None:
        return {}, None

    if isinstance(value, dict):
        return dict(value), None

    if not isinstance(value, str):
        return {}, "参数必须是 JSON 对象"

    text = value.strip()
    if not text:
        return {}, None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"参数不是合法 JSON：{exc}"

    if not isinstance(parsed, dict):
        return {}, "参数必须是 JSON 对象"

    return parsed, None


def _exposed_names(exposed_tools: Sequence[str | dict]) -> frozenset[str]:
    if isinstance(exposed_tools, (str, bytes)):
        raise TypeError("exposed_tools 必须是序列，不能是字符串")

    names: set[str] = set()

    try:
        items = list(exposed_tools)
    except TypeError as exc:
        raise TypeError("exposed_tools 必须是序列") from exc

    for index, item in enumerate(items):
        if isinstance(item, str):
            if not item.strip():
                raise ValueError(f"exposed_tools[{index}] 不能是空字符串")
            names.add(item)
            continue

        if isinstance(item, dict):
            function = item.get("function") or {}
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(
                    f"exposed_tools[{index}] 缺少 function.name"
                )
            names.add(name)
            continue

        raise TypeError(
            f"exposed_tools[{index}] 必须是工具名或 schema"
        )

    return frozenset(names)


# ---------------------------------------------------------------------- 消息


def _rejected(call: _Call, code: str, message: str) -> dict:
    return _tool_message(
        call,
        {"success": False, "error": code, "message": message},
    )


def _payload(call: _Call, payload: dict) -> dict:
    return _tool_message(call, payload)


def _tool_message(call: _Call, payload: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call.tool_call_id,
        "content": json.dumps(payload, ensure_ascii=False, default=str),
    }
