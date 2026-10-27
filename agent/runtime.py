import logging
import uuid

from agent.chat_responder import ChatResponder
from agent.intent import Intent, IntentRouter
from agent.llm import LLM
from agent.plan_runner import PlanRunner
from agent.planner import TaskPlanner
from tasks.plan_executor import TaskPlanExecutor
from tasks.scheduler import TaskScheduler
from tools.builtin import brewing, get_current_time, get_weather
from tools.registry import ToolRegistry
from tools.runner import ToolRunner
from events.models import EventType, RuntimeEvent

logger = logging.getLogger(__name__)

TASK_SYSTEM_PROMPT = (
    "你是私人酒庄的葡萄酒酿酒大师。\n\n"
    "你可以使用提供的工具，不要自己编造工具结果。"
)

CHAT_SYSTEM_PROMPT = (
    "你是私人酒庄的葡萄酒酿酒大师。\n\n"
    "请用简洁、自然的中文回答用户；可以基于常识回答，但不要编造工具执行结果。"
)


class AgentRuntime:
    """组装路由、任务执行链与聊天链路，对外提供事件流。"""

    def __init__(self, llm: LLM | None = None):
        # 1. 全局单例LLM
        self.llm = llm or LLM()

        # 2. 工具注册
        registry = ToolRegistry()
        registry.register(get_weather)
        registry.register(get_current_time)
        registry.register(brewing)

        # 3. ToolRunner->TaskScheduler->TaskPlanExecutor,TaskPlanner->TaskPlan
        tool_runner = ToolRunner(registry=registry)
        scheduler = TaskScheduler(tool_runner=tool_runner)
        executor = TaskPlanExecutor(scheduler=scheduler, registry=registry)
        planner = TaskPlanner(llm=self.llm, registry=registry)

        # 4. 意图路由器
        self.router = IntentRouter(llm=self.llm, registry=registry)

        # 5. 任务执行器 PlanRunner
        self.task_runner = PlanRunner(
            planner=planner,
            executor=executor,
            llm=self.llm,
            system_prompt=TASK_SYSTEM_PROMPT,
        )
        # 6. 对话执行器 ChatResponder
        self.chat_responder = ChatResponder(
            llm=self.llm,
            system_prompt=CHAT_SYSTEM_PROMPT,
        )

        logger.info("AgentRuntime 初始化完成，模型: %s", getattr(self.llm, "model", "unknown"))

    async def run(self, user_input: str):
        """根据意图选择聊天或任务路径。"""
        run_id = str(uuid.uuid4())
        decision = await self.router.decide(user_input)
        logger.info(
            "路由意图: %s (source=%s, rule=%s)",
            decision.intent.value,
            decision.source,
            decision.rule or "-",
        )
        logger.info("路由理由: %s", decision.reason)

        yield RuntimeEvent(
            type=EventType.INTENT_DETECTED,
            run_id=run_id,
            data={
                "intent": decision.intent.value,
                "source": decision.source,
                "rule": decision.rule,
                "reason": str(decision.reason or "")[:100],
            },
        )

        runner = self.task_runner if decision.intent == Intent.TASK else self.chat_responder
        async for event in runner.run(user_input, run_id=run_id):
            yield event