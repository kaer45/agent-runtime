"""普通对话路径：直接回答，不规划、不调用工具。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator

from agent.llm import LLM
from events.models import EventType, RuntimeEvent

logger = logging.getLogger(__name__)

class ChatResponder:
    """直接调用 LLM 回答用户。"""

    def __init__(self, llm: LLM, system_prompt: str):
        self.llm=llm
        self.system_prompt=system_prompt

    async def run(self,user_input:str,run_id:str|None=None)->AsyncIterator[RuntimeEvent]:
        """回答用户并产出事件。"""
        run_id=run_id or str(uuid.uuid4())

        yield RuntimeEvent(type=EventType.RUN_STARTED, run_id=run_id)
        try:
            response = await self.llm.chat(
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_input},
                ]
            )
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
            data={"content": response.content or ""},
        )
        logger.info("聊天回答完成")
        yield RuntimeEvent(type=EventType.RUN_FINISHED, run_id=run_id)