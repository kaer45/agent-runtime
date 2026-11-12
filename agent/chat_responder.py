"""普通对话路径：直接回答，不规划、不调用工具。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Sequence

from agent.llm import LLM
from events.models import EventType, RuntimeEvent

logger = logging.getLogger(__name__)

class ChatResponder:
    """直接调用 LLM 回答用户。"""

    def __init__(self, llm: LLM, system_prompt: str):
        self.llm=llm
        self.system_prompt=system_prompt

    async def answer(self, messages: Sequence[dict]) -> str:
        """用已组装好的 messages 调一次模型，返回回答正文。"""
        response = await self.llm.chat(messages=list(messages))
        return response.content or ""

    async def run(
        self,
        user_input: str | Sequence[dict],
        run_id: str | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        """回答用户并产出事件。

        ``user_input`` 既可以是原始文本（兼容旧调用），也可以是
        ``ContextBuilder`` 组装好的 messages。
        """
        run_id = run_id or str(uuid.uuid4())

        if isinstance(user_input, str):
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_input},
            ]
        else:
            messages = list(user_input)

        yield RuntimeEvent(type=EventType.RUN_STARTED, run_id=run_id)
        try:
            answer = await self.answer(messages)
        except Exception as exc:
            logger.exception("聊天回答失败")
            yield RuntimeEvent(
                type=EventType.RUN_FINISHED,
                run_id=run_id,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        yield RuntimeEvent(
            type=EventType.FINAL_ANSWER,
            run_id=run_id,
            data={"content": answer},
        )
        logger.info("聊天回答完成")
        yield RuntimeEvent(type=EventType.RUN_FINISHED, run_id=run_id)