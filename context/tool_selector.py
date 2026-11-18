"""动态工具选择：白名单过滤 → 关键词打分 → 预算裁剪。

打分只用于排序，不作为排除条件：预算和 ``max_tools`` 都够时，所有
allowed 工具都会进入上下文。``score=0`` 的候选在还有空间时同样入选。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from context.errors import ContextError
from context.models import ToolSelection
from core.issues import ContextIssueCode
from core.tokens import TokenCounter, count_text, estimate_tokens
from state.models import SessionState

# required 的固定最高分，保证排在最前。
REQUIRED_NAME_BONUS = 1000.0

# 关键词命中的权重：name 比 description 更具体，用户输入比 state 更直接。
USER_NAME_HIT = 10.0
USER_DESCRIPTION_HIT = 4.0
STATE_NAME_HIT = 6.0
STATE_DESCRIPTION_HIT = 3.0


def schema_text(schema: dict) -> str:
    """工具 schema 参与计数的规范文本。"""
    return json.dumps(schema, ensure_ascii=False, sort_keys=True)


def count_schema(counter: TokenCounter, schema: dict) -> int:
    return count_text(counter, schema_text(schema))


@dataclass(frozen=True)
class _Candidate:
    index: int
    name: str
    description: str
    schema: dict


class ToolSelector:
    """纯函数式工具选择器：不读 DB、不调 LLM。"""

    def __init__(
        self,
        *,
        allowed_tools: frozenset[str] | None = None,
        max_tools: int = 8,
        counter: TokenCounter = estimate_tokens,
    ):
        if allowed_tools is not None and not isinstance(
            allowed_tools, (frozenset, set)
        ):
            raise TypeError("allowed_tools 必须是 frozenset[str] 或 None")
        if allowed_tools is not None and not all(
            isinstance(name, str) for name in allowed_tools
        ):
            raise TypeError("allowed_tools 的元素必须是字符串")

        if isinstance(max_tools, bool) or not isinstance(max_tools, int):
            raise TypeError("max_tools 必须是整数")
        if max_tools < 0:
            raise ValueError("max_tools 不能为负数")

        self.allowed_tools = (
            None if allowed_tools is None else frozenset(allowed_tools)
        )
        self.max_tools = max_tools
        self.counter = counter

    def select(
        self,
        *,
        user_input: str,
        state: SessionState,
        tools: Sequence[dict],
        required_tools: frozenset[str],
        ceiling: int,
    ) -> ToolSelection:
        if not isinstance(user_input, str):
            raise TypeError("user_input 必须是字符串")
        if not isinstance(state, SessionState):
            raise TypeError("state 必须是 SessionState")
        if isinstance(ceiling, bool) or not isinstance(ceiling, int):
            raise TypeError("ceiling 必须是整数")
        if ceiling < 0:
            raise ValueError("ceiling 不能为负数")

        candidates = self._candidates(tools)
        allowed = [item for item in candidates if self._is_allowed(item.name)]
        allowed_names = {item.name for item in allowed}

        missing = sorted(set(required_tools) - allowed_names)
        if missing:
            raise ContextError.from_code(
                ContextIssueCode.TOOLS_REQUIRED_MISSING,
                f"required 工具不可用：{missing}",
                component="tools",
                details={"missing": missing},
            )

        user_lower = user_input.lower()
        state_lower = self._state_text(state).lower()

        scores = {
            item.name: self._score(
                item,
                user_lower=user_lower,
                state_lower=state_lower,
                required=required_tools,
            )
            for item in allowed
        }

        ordered = self._order(allowed, scores, required_tools)

        selected: list[_Candidate] = []
        dropped: list[str] = []
        used = 0

        for item in ordered:
            cost = count_schema(self.counter, item.schema)
            if len(selected) >= self.max_tools or used + cost > ceiling:
                if item.name in required_tools:
                    raise ContextError.from_code(
                        ContextIssueCode.TOOLS_REQUIRED_MISSING,
                        f"required 工具 {item.name} 放不进预算",
                        component="tools",
                        details={
                            "tool": item.name,
                            "used": used,
                            "cost": cost,
                            "ceiling": ceiling,
                            "max_tools": self.max_tools,
                        },
                    )
                # 跳过放不下的 optional，继续尝试更小的候选（C5 提议）。
                dropped.append(item.name)
                continue

            selected.append(item)
            used += cost

        whitelist_dropped = [
            item.name for item in candidates if not self._is_allowed(item.name)
        ]

        return ToolSelection(
            tools=tuple(item.schema for item in selected),
            selected_names=tuple(item.name for item in selected),
            dropped_names=tuple(whitelist_dropped) + tuple(dropped),
            scores=scores,
            reason=self._reason(
                candidates=candidates,
                allowed=allowed,
                selected=selected,
                dropped=dropped,
                required=required_tools,
            ),
            tokens=used,
        )

    # ------------------------------------------------------------------ 内部

    def _is_allowed(self, name: str) -> bool:
        return self.allowed_tools is None or name in self.allowed_tools

    @staticmethod
    def _candidates(tools: Sequence[dict]) -> list[_Candidate]:
        candidates: list[_Candidate] = []

        for index, schema in enumerate(tools):
            if not isinstance(schema, dict):
                raise TypeError("tools 的元素必须是 dict")

            function = schema.get("function") or {}
            name = function.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"工具 schema 缺少 function.name：index={index}")

            description = function.get("description") or ""
            if not isinstance(description, str):
                raise TypeError(f"工具 description 必须是字符串：{name}")

            candidates.append(
                _Candidate(
                    index=index,
                    name=name,
                    description=description,
                    schema=schema,
                )
            )

        return candidates

    @staticmethod
    def _state_text(state: SessionState) -> str:
        parts = [goal.text for goal in state.goals]
        parts.extend(state.constraints)
        parts.extend(str(value) for _, value in sorted(state.preferences.items()))
        return "\n".join(parts)

    @staticmethod
    def _score(
        item: _Candidate,
        *,
        user_lower: str,
        state_lower: str,
        required: frozenset[str],
    ) -> float:
        score = REQUIRED_NAME_BONUS if item.name in required else 0.0

        name = item.name.lower()
        description = item.description.lower()

        if name and name in user_lower:
            score += USER_NAME_HIT
        if description and description in user_lower:
            score += USER_DESCRIPTION_HIT
        if name and name in state_lower:
            score += STATE_NAME_HIT
        if description and description in state_lower:
            score += STATE_DESCRIPTION_HIT

        return score

    @staticmethod
    def _order(
        allowed: list[_Candidate],
        scores: dict[str, float],
        required: frozenset[str],
    ) -> list[_Candidate]:
        required_items = [
            item for item in allowed if item.name in required
        ]
        optional_items = sorted(
            (item for item in allowed if item.name not in required),
            key=lambda item: (-scores[item.name], item.index),
        )
        return required_items + optional_items

    @staticmethod
    def _reason(
        *,
        candidates: list[_Candidate],
        allowed: list[_Candidate],
        selected: list[_Candidate],
        dropped: list[str],
        required: frozenset[str],
    ) -> str:
        if not candidates or not selected:
            return "empty"

        optional = [item for item in allowed if item.name not in required]
        if not optional:
            return "required_only"

        return "budget_trim" if dropped else "ranked"
