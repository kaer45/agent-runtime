import logging

from agent.llm import LLM
from agent.model_tool_loop import ModelToolLoop

from tasks.scheduler import TaskScheduler

from tools.registry import ToolRegistry
from tools.runner import ToolRunner
from tools.builtin import get_weather, get_current_time, brewing

logger = logging.getLogger(__name__)

class AgentRuntime:
    def __init__(self):
        # 1. 实例化全局单例 LLM
        self.llm = LLM()

        # 2. 注册工具
        registry = ToolRegistry()
        registry.register(get_weather)
        registry.register(get_current_time)
        registry.register(brewing)

        # 3. 组装执行器与调度器
        tool_runner = ToolRunner(registry=registry)
        scheduler = TaskScheduler(tool_runner=tool_runner)

        # 4. 组装主循环（使用 self.llm 注入）
        self.loop = ModelToolLoop(
            llm=self.llm,
            tool_registry=registry,
            tool_runner=tool_runner,
            scheduler=scheduler,
        )
        
        logger.info("AgentRuntime 初始化完成，模型: %s", self.llm.model)
        logger.info("已注册工具: %s", registry.list_tools())

    async def run(self, user_input: str):
        """接收用户输入，返回异步事件生成器。"""
        messages = [
            {
                "role": "system",
                "content": (
                    "你是私人酒庄的葡萄酒酿酒大师。\n\n"
                    "你可以使用提供的工具，不要自己编造工具结果。"
                )
            },
            {
                "role": "user",
                "content": user_input
            }
        ]

        # 透传事件流
        async for event in self.loop.run(messages):
            yield event