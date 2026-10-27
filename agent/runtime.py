import logging

from agent.llm import LLM
from agent.plan_runner import PlanRunner
from agent.planner import TaskPlanner
from tasks.plan_executor import TaskPlanExecutor
from tasks.scheduler import TaskScheduler
from tools.builtin import brewing, get_current_time, get_weather
from tools.registry import ToolRegistry
from tools.runner import ToolRunner

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是私人酒庄的葡萄酒酿酒大师。\n\n"
    "你可以使用提供的工具，不要自己编造工具结果。"
)


class AgentRuntime:
    """组装 LLM、工具、规划器与执行器，对外提供事件流。"""

    def __init__(self):
        # 1. 全局单例 LLM
        self.llm = LLM()

        # 2. 工具
        registry = ToolRegistry()
        registry.register(get_weather)
        registry.register(get_current_time)
        registry.register(brewing)

        # 3. 执行链：Runner → Scheduler → Executor
        tool_runner = ToolRunner(registry=registry)
        scheduler = TaskScheduler(tool_runner=tool_runner)
        executor = TaskPlanExecutor(scheduler=scheduler, registry=registry)

        # 4. 规划器 + 运行器
        planner = TaskPlanner(llm=self.llm, registry=registry)
        self.runner = PlanRunner(
            planner=planner,
            executor=executor,
            llm=self.llm,
            system_prompt=SYSTEM_PROMPT,
        )

        logger.info("AgentRuntime 初始化完成，模型: %s", self.llm.model)
        logger.info("已注册工具: %s", registry.list_tools())

    async def run(self, user_input: str):
        """接收用户输入，返回事件生成器。"""
        async for event in self.runner.run(user_input):
            yield event