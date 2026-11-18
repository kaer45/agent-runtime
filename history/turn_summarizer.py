"""滚动摘要生成（v2）：档位重试 + 输入保护。

与旧 ``history.summarizer`` 的区别：

- 档位重试：``local_max_tokens`` 和 ``model_max_tokens`` 分开升级
  （local 截断只升 local，模型截断两者同时升）；
- 输入保护：``fit_turns`` 只取能放下的最老连续前缀，剩下的下一轮再压；
- 存储摘要始终完整：本地不再截断，截断只发生在渲染层。

本模块不读 DB、不做 CAS；保存与水位推进由 ``HistoryCompactor`` 负责。
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from core.tokens import TokenCounter, count_messages, estimate_tokens
from history.errors import SummaryError
from history.prompts import SUMMARY_SYSTEM_PROMPT
from history.turns import RollingSummary, TurnRecord

logger = logging.getLogger(__name__)

# 本地档位；model 档位是它的 0.7 倍。
LOCAL_TOKEN_LADDER = (2024, 4096, 8192)
MODEL_TOKEN_RATIO = 0.7

MAX_ATTEMPTS = 3

# 摘要输入的独立策略上限（strategy §5.5）。
SUMMARY_INPUT_CEILING = 100_000


def model_token_ladder(
    local_ladder: Sequence[int] = LOCAL_TOKEN_LADDER,
) -> tuple[int, ...]:
    return tuple(int(value * MODEL_TOKEN_RATIO) for value in local_ladder)


class HistorySummarizer:
    """用模型把旧摘要和新 turn 合并成新摘要。"""

    def __init__(
        self,
        llm: Any,
        *,
        counter: TokenCounter = estimate_tokens,
        available_tokens: int = 248_000,
        input_ceiling: int = SUMMARY_INPUT_CEILING,
        max_attempts: int = MAX_ATTEMPTS,
    ):
        if isinstance(available_tokens, bool) or not isinstance(available_tokens, int):
            raise TypeError("available_tokens 必须是整数")
        if available_tokens <= 0:
            raise ValueError("available_tokens 必须大于 0")

        if isinstance(input_ceiling, bool) or not isinstance(input_ceiling, int):
            raise TypeError("input_ceiling 必须是整数")
        if input_ceiling <= 0:
            raise ValueError("input_ceiling 必须大于 0")

        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("max_attempts 必须是整数")
        if max_attempts <= 0:
            raise ValueError("max_attempts 必须大于 0")

        self.llm = llm
        self.counter = counter
        self.available_tokens = available_tokens
        self.max_attempts = max_attempts

        self.local_ladder = tuple(LOCAL_TOKEN_LADDER)
        self.model_ladder = model_token_ladder(self.local_ladder)

        # 按最大输出档位预留，再取策略上限。
        self.input_ceiling = min(
            input_ceiling,
            available_tokens - self.model_ladder[-1],
        )

    # ------------------------------------------------------------------ 输入保护

    def fit_turns(
        self,
        previous: RollingSummary,
        turns: Sequence[TurnRecord],
        input_ceiling: int,
    ) -> tuple[TurnRecord, ...]:
        """最老的连续前缀，保证这次的 prompt 放得进 ``input_ceiling``。"""
        if not isinstance(previous, RollingSummary):
            raise TypeError("previous 必须是 RollingSummary")
        if isinstance(input_ceiling, bool) or not isinstance(input_ceiling, int):
            raise TypeError("input_ceiling 必须是整数")
        if input_ceiling <= 0:
            raise ValueError("input_ceiling 必须大于 0")

        turns = tuple(turns)
        if not all(isinstance(turn, TurnRecord) for turn in turns):
            raise TypeError("turns 的元素必须是 TurnRecord")
        if not turns:
            return ()

        used = self._prompt_tokens(previous, ())
        selected: list[TurnRecord] = []

        for turn in turns:
            cost = self._count_text(self._render_turns((turn,))) + 1
            if used + cost > input_ceiling:
                break
            selected.append(turn)
            used += cost

        return tuple(selected)

    # ------------------------------------------------------------------ 生成

    async def update(
        self,
        previous: RollingSummary,
        turns: Sequence[TurnRecord],
    ) -> RollingSummary:
        """合并摘要；失败抛 ``SummaryError``，调用方保留旧摘要与旧水位。"""
        if not isinstance(previous, RollingSummary):
            raise TypeError("previous 必须是 RollingSummary")

        turns = tuple(turns)
        if not all(isinstance(turn, TurnRecord) for turn in turns):
            raise TypeError("turns 的元素必须是 TurnRecord")
        if not turns:
            return previous

        messages = self._build_messages(previous, turns)
        prompt_tokens = count_messages(self.counter, messages)

        local_index = 0
        model_index = 0
        last_reason = ""

        for attempt in range(1, self.max_attempts + 1):
            local_max = self.local_ladder[local_index]
            model_max = self.model_ladder[model_index]

            budget = self.available_tokens - model_max
            if prompt_tokens > budget:
                raise SummaryError(
                    "摘要输入超窗："
                    f"{prompt_tokens} > {budget}"
                    f"（available={self.available_tokens}, model_max={model_max}）"
                )

            try:
                response = await self.llm.chat_with_meta(
                    messages=messages,
                    temperature=0,
                    thinking=False,
                    max_tokens=model_max,
                )
            except Exception as exc:  # noqa: BLE001 - 统一转成摘要失败
                last_reason = f"调用异常：{exc}"
                logger.warning("摘要第 %d 次尝试失败：%s", attempt, last_reason)
                continue

            content = self._content_of(response)
            finish_reason = getattr(response, "finish_reason", None)
            usage = getattr(response, "usage", None)

            if not content:
                last_reason = "模型返回了空摘要"
                logger.warning("摘要第 %d 次尝试失败：%s", attempt, last_reason)
                continue

            local_truncated = self._count_text(content) > local_max
            model_truncated = finish_reason == "length"

            logger.info(
                "摘要尝试 attempt=%d local_max=%d model_max=%d "
                "local_truncated=%s model_truncated=%s finish_reason=%s usage=%s",
                attempt,
                local_max,
                model_max,
                local_truncated,
                model_truncated,
                finish_reason,
                usage,
            )

            if not local_truncated and not model_truncated:
                return RollingSummary(text=content)

            if (
                local_index >= len(self.local_ladder) - 1
                and model_index >= len(self.model_ladder) - 1
            ):
                last_reason = (
                    "摘要被截断且档位已用尽："
                    f"local={local_max}, model={model_max}"
                )
                break

            if model_truncated:
                local_index = min(local_index + 1, len(self.local_ladder) - 1)
                model_index = min(model_index + 1, len(self.model_ladder) - 1)
            else:
                local_index = min(local_index + 1, len(self.local_ladder) - 1)

        raise SummaryError(f"摘要生成失败（{self.max_attempts} 次尝试）：{last_reason}")

    # ------------------------------------------------------------------ 内部

    def _content_of(self, response: Any) -> str:
        message = getattr(response, "message", None)
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            return ""
        return content.strip()

    def _build_messages(
        self,
        previous: RollingSummary,
        turns: Sequence[TurnRecord],
    ) -> list[dict]:
        return [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": self._build_user_content(previous, turns)},
        ]

    def _build_user_content(
        self,
        previous: RollingSummary,
        turns: Sequence[TurnRecord],
    ) -> str:
        previous_text = previous.text or "（无）"
        return (
            f"已有摘要：\n{previous_text}\n\n"
            f"新增消息：\n{self._render_turns(turns)}\n\n"
            "请输出更新后的摘要。"
        )

    def _prompt_tokens(
        self,
        previous: RollingSummary,
        turns: Sequence[TurnRecord],
    ) -> int:
        return count_messages(self.counter, self._build_messages(previous, turns))

    @staticmethod
    def _render_turns(turns: Sequence[TurnRecord]) -> str:
        lines: list[str] = []

        for turn in turns:
            for message in turn.messages:
                role = message.get("role", "unknown")
                content = message.get("content")
                if content is not None:
                    lines.append(f"{role}: {content}")

                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    lines.append(
                        f"{role} tool_call: {function.get('name', '')}"
                        f"({function.get('arguments', '')})"
                    )

        return "\n".join(lines) or "（无）"

    def _count_text(self, text: str) -> int:
        return self.counter(text or "")
