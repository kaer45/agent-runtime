import logging
import os
import uuid
from pathlib import Path

from agent.chat_responder import ChatResponder
from agent.intent import Intent, IntentRouter
from agent.llm import LLM
from agent.plan_runner import PlanRunner
from agent.planner import TaskPlanner
from agent.turn_orchestrator import TurnOrchestrator
from context.budget import TokenBudget
from context.builder import ContextBuilder
from context.history import HistoryStore
from context.planner import ContextPlanner
from context.summarizer import HistorySummarizer
from state.extractor import StateExtractor
from state.service import StateService
from state.session import SessionRegistry
from state.store import StateStore
from state.updater import TurnStateUpdater
from tasks.plan_executor import TaskPlanExecutor
from tasks.scheduler import TaskScheduler
from tools.builtin import (
    brewing,
    get_current_time,
    get_weather,
    recommend_wine_pairing,
)
from tools.catalog import ToolCatalog
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

DEFAULT_DB_PATH = "data/agent.db"
CHAT_MAX_TOKENS = 2000
CHAT_RESERVE_OUTPUT = 400
CHAT_STATE_MAX_TOKENS = 600
CHAT_SUMMARY_MAX_TOKENS = 400
CHAT_SUMMARY_TEXT_MAX_TOKENS = 300
HISTORY_MAX_MESSAGES = 50


class AgentRuntime:
    """组装路由、任务执行链与聊天链路，对外提供事件流。"""

    def __init__(
        self,
        llm: LLM | None = None,
        *,
        db_path: str | Path | None = None,
        history_limit: int = HISTORY_MAX_MESSAGES,
    ):
        # 1. 全局单例LLM
        self.llm = llm or LLM()
        self.db_path = Path(
            db_path or os.getenv("AGENT_DB_PATH", DEFAULT_DB_PATH)
        )

        # 2. 工具注册
        registry = ToolRegistry()
        registry.register(get_weather)
        registry.register(get_current_time)
        registry.register(brewing)
        registry.register(recommend_wine_pairing)

        # 3. ToolRunner->TaskScheduler->TaskPlanExecutor,TaskPlanner->TaskPlan
        tool_runner = ToolRunner(registry=registry)
        scheduler = TaskScheduler(tool_runner=tool_runner)
        executor = TaskPlanExecutor(scheduler=scheduler, registry=registry)

        # 工具目录只构建一次，Planner 和 Router 共用同一份快照。
        catalog = ToolCatalog.from_registry(registry)
        self.catalog = catalog
        logger.info("工具目录指纹: %s", catalog.fingerprint())

        planner = TaskPlanner(llm=self.llm, registry=registry, catalog=catalog)

        # 4. 意图路由器
        self.router = IntentRouter(llm=self.llm, registry=registry, catalog=catalog)

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

        # 7. 状态与历史持久化：两个 Store 共库，但各自事务。
        self.state_store = StateStore(self.db_path)
        self.state_store.initialize()
        self.history_store = HistoryStore(
            self.db_path,
            max_messages_per_session=history_limit,
        )
        self.history_store.initialize()

        self.state_service = StateService(SessionRegistry(self.state_store))
        budget = TokenBudget(
            max_tokens=CHAT_MAX_TOKENS,
            reserve_output=CHAT_RESERVE_OUTPUT,
        )
        self.context_builder = ContextBuilder(
            system_prompt=CHAT_SYSTEM_PROMPT,
            budget=budget,
            state_max_tokens=CHAT_STATE_MAX_TOKENS,
        )
        self.orchestrator = TurnOrchestrator(
            state_service=self.state_service,
            history_store=self.history_store,
            planner=ContextPlanner(
                state_max_tokens=CHAT_STATE_MAX_TOKENS,
                summary_max_tokens=CHAT_SUMMARY_MAX_TOKENS,
            ),
            builder=self.context_builder,
            summarizer=HistorySummarizer(
                self.llm,
                max_summary_tokens=CHAT_SUMMARY_TEXT_MAX_TOKENS,
                counter=budget.counter,
            ),
            updater=TurnStateUpdater(
                StateExtractor(self.llm),
                self.state_service,
            ),
            chat_responder=self.chat_responder,
        )

        logger.info("AgentRuntime 初始化完成，模型: %s", getattr(self.llm, "model", "unknown"))

    async def run(self, user_input: str, session_id: str | None = None):
        """根据意图选择聊天或任务路径。"""
        run_id = str(uuid.uuid4())
        session_id = self._normalize_session_id(session_id, run_id)

        decision = await self.router.decide(user_input)
        logger.info(
            "路由意图: %s (source=%s, rule=%s, session=%s)",
            decision.intent.value,
            decision.source,
            decision.rule or "-",
            session_id,
        )
        logger.info("路由理由: %s", decision.reason)

        yield RuntimeEvent(
            type=EventType.INTENT_DETECTED,
            run_id=run_id,
            session_id=session_id,
            data={
                "intent": decision.intent.value,
                "source": decision.source,
                "rule": decision.rule,
                "reason": str(decision.reason or "")[:100],
            },
        )

        if decision.intent == Intent.TASK:
            async for event in self.task_runner.run(user_input, run_id=run_id):
                event.session_id = session_id
                yield event
            return

        try:
            context = await self.orchestrator.begin_turn(
                session_id,
                turn_id=run_id,
            )
        except Exception as exc:
            logger.exception("加载会话上下文失败 session=%s", session_id)
            yield RuntimeEvent(
                type=EventType.RUN_STARTED,
                run_id=run_id,
                session_id=session_id,
            )
            yield RuntimeEvent(
                type=EventType.RUN_FINISHED,
                run_id=run_id,
                session_id=session_id,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            return

        async for event in self.orchestrator.run_chat(
            context,
            user_input,
            run_id=run_id,
        ):
            event.session_id = session_id
            yield event

    @staticmethod
    def _normalize_session_id(
        session_id: str | None,
        run_id: str,
    ) -> str:
        if session_id is None:
            return f"s_{run_id}"
        if not isinstance(session_id, str):
            raise TypeError("session_id 必须是字符串或 None")
        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id 不能为空")
        return session_id