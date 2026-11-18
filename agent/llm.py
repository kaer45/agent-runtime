import os
import logging
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMResponse:
    """一次模型调用的完整结果。

    ``message`` 是原始消息对象；``finish_reason`` 用于判断模型侧截断
    （``"length"``）；``usage`` 是厂商返回的用量，统一成 dict 便于日志。
    """

    message: Any
    finish_reason: str | None
    usage: dict | None


def _usage_to_dict(response: Any) -> dict | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None

    model_dump = getattr(usage, "model_dump", None)
    if callable(model_dump):
        return model_dump()

    try:
        return dict(usage)
    except (TypeError, ValueError):
        return None


class LLM:
    def __init__(self, model: str = "deepseek-flash", *, client: AsyncOpenAI | None = None):
        self.client = client if client is not None else AsyncOpenAI(
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL"),
        )
        self.model = os.getenv("LLM_MODEL_ID") or model
        logger.info("LLM客户端初始化成功，当前模型: %s", self.model)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        thinking: bool | None = None,
        temperature: float | None = None,
    ):
        """执行一次非流式对话请求并返回消息体（保留旧签名）。"""
        response = await self.chat_with_meta(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            thinking=thinking,
            temperature=temperature,
        )
        return response.message

    async def chat_with_meta(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        thinking: bool | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """执行一次请求，额外返回 ``finish_reason`` 和 ``usage``。"""
        logger.debug("请求LLM: 消息数=%d, 工具数=%d", len(messages), len(tools or []))

        kwargs: dict = {}
        if tools is not None:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if thinking is not None:
            kwargs["extra_body"] = {
                "thinking": {"type": "enabled" if thinking else "disabled"}
            }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            **kwargs,
        )
        choice = response.choices[0]

        return LLMResponse(
            message=choice.message,
            finish_reason=getattr(choice, "finish_reason", None),
            usage=_usage_to_dict(response),
        )
