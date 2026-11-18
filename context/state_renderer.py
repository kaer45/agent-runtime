"""把 ``SessionState`` 渲染成紧凑文本（v2）。

与旧 ``StateRenderer`` 的区别：

- 不输出 ``STATE_HEADER``，``<state>`` 标签由组件添加；
- 按 section 整体取舍，丢弃时记录 section 名；
- 不做 critical tier，超预算就截断而不是失败；
- 可用 ``measure`` 把候选文本映射成“最终消息成本”，把转义膨胀和
  counter 不可加性都算进去，组件因此不需要重试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from core.tokens import TokenCounter, estimate_tokens
from state.models import Goal, SessionState

# section 的固定顺序与中文标签。
SECTION_ORDER = (
    ("preferences", "偏好"),
    ("goals", "目标"),
    ("constraints", "约束"),
    ("progress", "进度"),
    ("facts", "事实"),
)

Measure = Callable[[str], int]


@dataclass(frozen=True)
class RenderedState:
    """状态渲染结果。

    ``tokens`` 是文本本身的计数；含包装的真实成本由组件用 ``measure`` 重算。
    """

    text: str = ""
    tokens: int = 0
    truncated: bool = False
    dropped_sections: tuple[str, ...] = ()


class StateRenderer:
    """按 section 顺序渲染会话状态。"""

    def __init__(self, counter: TokenCounter = estimate_tokens):
        self.counter = counter

    def is_empty(self, state: SessionState) -> bool:
        return not any(
            (
                state.preferences,
                state.goals,
                state.constraints,
                state.progress,
                state.facts,
            )
        )

    def render(
        self,
        state: SessionState,
        max_tokens: int,
        *,
        measure: Measure | None = None,
    ) -> RenderedState:
        if not isinstance(state, SessionState):
            raise TypeError("state 必须是 SessionState")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int):
            raise TypeError("max_tokens 必须是整数")
        if max_tokens < 0:
            raise ValueError("max_tokens 不能为负数")
        if measure is not None and not callable(measure):
            raise TypeError("measure 必须可调用")

        sections = self._sections(state)
        if not sections:
            return RenderedState()

        lines: list[str] = []
        dropped: list[str] = []

        for index, (name, label, body) in enumerate(sections):
            candidate = lines + [label] + body
            if self._fits(candidate, max_tokens, measure):
                lines = candidate
                continue

            # 放不下就截断：当前 section 及之后全部丢弃。
            dropped.extend(
                section_name for section_name, _, _ in sections[index:]
            )
            break

        if not lines:
            return RenderedState(
                text="",
                tokens=0,
                truncated=bool(dropped),
                dropped_sections=tuple(dropped),
            )

        text = "\n".join(lines)
        return RenderedState(
            text=text,
            tokens=self._count_text(text),
            truncated=bool(dropped),
            dropped_sections=tuple(dropped),
        )

    def _fits(
        self,
        lines: list[str],
        max_tokens: int,
        measure: Measure | None,
    ) -> bool:
        text = "\n".join(lines)
        cost = measure(text) if measure is not None else self._count_text(text)
        return cost <= max_tokens

    def _sections(
        self,
        state: SessionState,
    ) -> list[tuple[str, str, list[str]]]:
        sections: list[tuple[str, str, list[str]]] = []

        for name, label in SECTION_ORDER:
            value = getattr(state, name)
            if not value:
                continue

            if name == "preferences":
                body = [
                    f"- {key}: {self._format_value(value[key])}"
                    for key in sorted(value)
                ]
            elif name == "goals":
                body = [f"- {self._format_goal(goal)}" for goal in value]
            elif name == "constraints":
                body = [f"- {self._one_line(item)}" for item in value]
            elif name == "progress":
                body = [
                    f"- {key}: {self._one_line(value[key])}"
                    for key in sorted(value)
                ]
            else:  # facts
                body = [
                    f"- {key}: {self._format_value(value[key])}"
                    for key in sorted(value)
                ]

            sections.append((name, label, body))

        return sections

    def _format_goal(self, goal: Goal) -> str:
        return f"{goal.id} [{goal.status.value}] {self._one_line(goal.text)}"

    @staticmethod
    def _format_value(value) -> str:
        if isinstance(value, str):
            return " ".join(value.splitlines()).strip()
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _one_line(text: str) -> str:
        return " ".join(str(text).splitlines()).strip()

    def _count_text(self, text: str) -> int:
        return self.counter(text or "")
