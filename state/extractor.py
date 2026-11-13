"""从对话增量中提取会话状态变更。

本模块只生成 ``StateDelta``，不负责把变更应用到 ``SessionState``；
应用、版本和冲突处理留给第 16 课的 ``StateService``。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from pydantic import ValidationError

from state.models import SessionState, StateDelta
from state.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    STATE_DELTA_TOOL,
    STATE_TOOL_NAME,
)


class StateExtractionError(RuntimeError):
    """状态增量提取失败，或模型返回的增量不合法。"""


class StateExtractor:
    """用模型把一轮对话转换成 ``StateDelta``。

    Example:
        extractor = StateExtractor(llm)
        delta = await extractor.extract(state, latest_messages)
    """

    def __init__(self, llm: Any):
        self.llm = llm

    async def extract(
        self,
        state: SessionState,
        messages: Sequence[dict],
    ) -> StateDelta:
        """提取本轮相对 ``state`` 的增量。

        Args:
            state: 当前会话状态，用来判断哪些内容已经存在。
            messages: 本轮新增消息；不要传完整历史，否则旧事件会被重复提取。

        Returns:
            StateDelta；没有变化时返回空增量。

        Raises:
            StateExtractionError: 模型调用失败，或返回的增量不合法。
        """
        if not messages:
            return StateDelta()

        try:
            response = await self.llm.chat(
                messages=self._build_messages(state, messages),
                tools=[STATE_DELTA_TOOL],
                tool_choice={
                    "type": "function",
                    "function": {"name": STATE_TOOL_NAME},
                },
                thinking=False,
                temperature=0,
            )
        except Exception as exc:
            raise StateExtractionError(f"状态提取失败：{exc}") from exc

        call = self._find_delta_call(response)
        try:
            if call is not None:
                payload = json.loads(call.function.arguments)
            else:
                payload = json.loads(response.content or "")
            return StateDelta.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise StateExtractionError(
                f"状态增量不合法：{self._format_error(exc)}"
            ) from exc

    def _build_messages(
        self,
        state: SessionState,
        messages: Sequence[dict],
    ) -> list[dict]:
        state_json = json.dumps(
            state.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        return [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"当前状态：\n{state_json}\n\n"
                    f"本轮新增消息：\n{self._render_messages(messages)}\n\n"
                    "请提交本轮相对当前状态的增量。"
                ),
            },
        ]

    @staticmethod
    def _render_messages(messages: Sequence[dict]) -> str:
        lines: list[str] = []
        for message in messages:
            role = message.get("role", "unknown")
            content = message.get("content")
            if content:
                lines.append(f"{role}: {content}")

            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                lines.append(
                    f"{role} tool_call: {function.get('name', '')}"
                    f"({function.get('arguments', '')})"
                )

        return "\n".join(lines) or "（无）"

    @staticmethod
    def _find_delta_call(response):
        for call in response.tool_calls or []:
            if call.function.name == STATE_TOOL_NAME:
                return call
        return None

    @staticmethod
    def _format_error(exc: Exception) -> str:
        if isinstance(exc, ValidationError):
            details = []
            for error in exc.errors():
                location = ".".join(str(part) for part in error["loc"]) or "(delta)"
                details.append(f"{location}: {error['msg']}")
            return "；".join(details)
        return str(exc)