import logging
import os
import uuid
from pathlib import Path

from agent.intent import Intent, IntentRouter
from agent.llm import LLM
from agent.plan_runner import PlanRunner
from agent.planner import TaskPlanner
from agent.turn_orchestrator import TurnOrchestrator
from context.engine import ContextEngine
from context.errors import ContextError
from context.models import ContextLimits
from events.models import EventType, RuntimeEvent
from history.compactor import HistoryCompactor
from history.store import HistoryStore
from history.summarizer import HistorySummarizer
from state.extractor import StateExtractor
from state.registry import SessionStateRegistry
from state.service import StateService
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


class AgentRuntime:
    """组装路由、任务执行链与聊天链路，对外提供事件流。"""

    def __init__(
        self,
        llm: LLM | None = None,
        *,
        db_path: str | Path | None = None,
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

        # 5. 任务执行器 PlanRunner（执行模式阶段再统一到 TurnOrchestrator）
        self.task_runner = PlanRunner(
            planner=planner,
            executor=executor,
            llm=self.llm,
            system_prompt=TASK_SYSTEM_PROMPT,
        )

        # 6. 状态与历史持久化：两个 Store 共库，但各自事务。
        # 历史 schema 与旧版不兼容，initialize() 遇到旧表会 fail fast。
        self.state_store = StateStore(self.db_path)
        self.state_store.initialize()
        self.history_store = HistoryStore(self.db_path)
        self.history_store.initialize()

        self.state_service = StateService(SessionStateRegistry(self.state_store))

        # 7. 上下文装配与历史维护（两者都必须是长生命周期单例：
        #    engine 启动时校验 system 预算，compactor 内部持有 per-session 锁）。
        self.limits = ContextLimits()
        self.context_engine = ContextEngine(
            system_prompt=CHAT_SYSTEM_PROMPT,
            limits=self.limits,
        )
        self.history_summarizer = HistorySummarizer(
            self.llm,
            available_tokens=self.limits.available_tokens,
        )
        self.compactor = HistoryCompactor(
            store=self.history_store,
            summarizer=self.history_summarizer,
            limits=self.limits,
        )
        self.updater = TurnStateUpdater(
            StateExtractor(self.llm),
            self.state_service,
        )
        self.orchestrator = TurnOrchestrator(
            state_service=self.state_service,
            store=self.history_store,
            engine=self.context_engine,
            compactor=self.compactor,
            updater=self.updater,
            llm=self.llm,
        )

        logger.info(
            "AgentRuntime 初始化完成，模型: %s",
            getattr(self.llm, "model", "unknown"),
        )

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
        except ContextError as exc:
            for event in self._begin_failed(
                run_id,
                session_id,
                exc,
                error_code=exc.code.value,
                level=exc.issue.level.value,
            ):
                yield event
            return
        except Exception as exc:
            logger.exception("加载会话上下文失败 session=%s", session_id)
            for event in self._begin_failed(run_id, session_id, exc):
                yield event
            return

        async for event in self.orchestrator.run_chat(
            context,
            user_input,
            run_id=run_id,
        ):
            event.session_id = session_id
            yield event

    @staticmethod
    def _begin_failed(
        run_id: str,
        session_id: str,
        exc: Exception,
        *,
        error_code: str | None = None,
        level: str | None = None,
    ) -> tuple[RuntimeEvent, RuntimeEvent]:
        data: dict = {"error": str(exc), "error_type": type(exc).__name__}
        if error_code is not None:
            data["error_code"] = error_code
            data["level"] = level

        return (
            RuntimeEvent(
                type=EventType.RUN_STARTED,
                run_id=run_id,
                session_id=session_id,
            ),
            RuntimeEvent(
                type=EventType.RUN_FINISHED,
                run_id=run_id,
                session_id=session_id,
                data=data,
            ),
        )

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
