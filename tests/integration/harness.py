"""M3d 集成测试的公共装配：真 SQLite + 真 engine/store/compactor，假模型。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from agent.turn_orchestrator import TurnOrchestrator
from context.engine import ContextEngine
from context.models import ContextLimits
from events.models import EventType
from history.compactor import HistoryCompactor
from history.store import HistoryStore
from history.summarizer import HistorySummarizer
from state.models import StateDelta
from state.registry import SessionStateRegistry
from state.service import StateService
from state.store import StateStore
from state.updater import TurnStateUpdater

SESSION = "session-1"
SYSTEM_PROMPT = "你是侍酒师。"


class FakeLLM:
    """按队列返回 chat / chat_with_meta；每次调用都让出一次事件循环。"""

    def __init__(self):
        self.chat_responses: list = []
        self.summary_responses: list = []
        self.chat_calls: list[list[dict]] = []
        self.summary_calls: list[list[dict]] = []

    def queue_chat(self, content: str) -> None:
        self.chat_responses.append(content)

    def queue_chat_error(self, exc: BaseException) -> None:
        self.chat_responses.append(exc)

    def queue_summary(self, text: str) -> None:
        self.summary_responses.append(text)

    def queue_summary_error(self, exc: BaseException) -> None:
        self.summary_responses.append(exc)

    async def chat(self, messages, **kwargs):
        self.chat_calls.append(list(messages))
        await asyncio.sleep(0)
        if not self.chat_responses:
            raise AssertionError("没有排队的 chat 响应")
        item = self.chat_responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(content=item, tool_calls=None)

    async def chat_with_meta(self, messages, **kwargs):
        self.summary_calls.append(list(messages))
        await asyncio.sleep(0)
        if not self.summary_responses:
            raise AssertionError("没有排队的摘要响应")
        item = self.summary_responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return SimpleNamespace(
            message=SimpleNamespace(content=item),
            finish_reason="stop",
            usage=None,
        )


class BlockingLLM(FakeLLM):
    """chat 挂起，用于测试取消。"""

    def __init__(self, answer: str = "回答"):
        super().__init__()
        self.answer = answer
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, messages, **kwargs):
        self.chat_calls.append(list(messages))
        self.started.set()
        await self.release.wait()
        return SimpleNamespace(content=self.answer, tool_calls=None)


class FakeExtractor:
    """按队列返回 StateDelta；队列空了返回空 delta。"""

    def __init__(self, deltas=None):
        self.deltas = list(deltas or [])
        self.calls: list[tuple] = []

    async def extract(self, state, messages):
        self.calls.append((state, list(messages)))
        if self.deltas:
            item = self.deltas.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return StateDelta()


class Harness:
    """一套完整链路；同一 db_path 上重复 make 即可模拟重启。"""

    def __init__(self, db_path, limits, llm, extractor):
        self.db_path = db_path
        self.limits = limits
        self.llm = llm
        self.extractor = extractor

        self.state_store = StateStore(db_path)
        self.state_store.initialize()
        self.history = HistoryStore(db_path)
        self.history.initialize()

        self.service = StateService(SessionStateRegistry(self.state_store))
        self.engine = ContextEngine(
            system_prompt=SYSTEM_PROMPT,
            limits=limits,
        )
        self.summarizer = HistorySummarizer(
            llm, available_tokens=limits.available_tokens
        )
        self.compactor = HistoryCompactor(
            store=self.history,
            summarizer=self.summarizer,
            limits=limits,
        )
        self.updater = TurnStateUpdater(extractor, self.service)
        self.orchestrator = TurnOrchestrator(
            state_service=self.service,
            store=self.history,
            engine=self.engine,
            compactor=self.compactor,
            updater=self.updater,
            llm=llm,
        )

    async def run_turn(self, user_input: str, *, session: str = SESSION, turn_id=None):
        context = await self.orchestrator.begin_turn(
            session, turn_id=turn_id
        )
        events = [
            event async for event in self.orchestrator.run_chat(
                context, user_input
            )
        ]
        return context, events


def make_harness(
    db_path,
    *,
    limits: ContextLimits | None = None,
    llm: FakeLLM | None = None,
    extractor: FakeExtractor | None = None,
) -> Harness:
    return Harness(
        db_path=db_path,
        limits=limits or ContextLimits(),
        llm=llm or FakeLLM(),
        extractor=extractor or FakeExtractor(),
    )


def seed_turn(history: HistoryStore, session: str, index: int, text: str) -> None:
    history.append_turn_once(
        session,
        f"seed-{index}",
        [
            {"role": "user", "content": text},
            {"role": "assistant", "content": ""},
        ],
    )


def final_data(events) -> dict:
    assert events[-1].type is EventType.RUN_FINISHED
    return events[-1].data


def answer_of(events) -> str:
    for event in events:
        if event.type is EventType.FINAL_ANSWER:
            return event.data["content"]
    raise AssertionError("没有 FINAL_ANSWER")


def contents_of(messages) -> list[str]:
    return [message.get("content", "") for message in messages]
