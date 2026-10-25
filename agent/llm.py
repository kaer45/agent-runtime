import os
import logging
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

logger = logging.getLogger(__name__)

class LLM:
    def __init__(self, model: str = "deepseek-flash"):
        self.client = AsyncOpenAI(
            api_key=os.getenv("LLM_API_KEY"),
            base_url=os.getenv("LLM_BASE_URL")
        )
        self.model = os.getenv("LLM_MODEL_ID") or model
        logger.info("LLM客户端初始化成功，当前模型: %s", self.model)

    async def chat(self, messages: list[dict], tools: list[dict] | None = None):
        """执行一次非流式对话请求并返回消息体。"""
        logger.debug("请求LLM: 消息数=%d, 工具数=%d", len(messages), len(tools or []))
        
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
        )
        return response.choices[0].message