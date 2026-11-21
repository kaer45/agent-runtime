"""真实 LLM 的端到端冒烟：多轮对话 + 状态提取 + 上下文装配。

走完整 v2 链路：

    TurnOrchestrator.begin_turn
      → ContextEngine.assemble
      → LLM
      → TurnStateUpdater（状态 delta）
      → HistoryStore.append_turn_once
      → HistoryCompactor.after_turn（摘要 / 归档）

数据库是临时 SQLite 文件，默认跑完删除。

用法：
    .venv\\Scripts\\python.exe -m scripts.real_llm_state_e2e

可选：
    --session real-e2e
    --keep-db
    --turns 3
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path

from agent.llm import LLM
from agent.runtime import CHAT_SYSTEM_PROMPT
from agent.turn_orchestrator import TurnOrchestrator
from context.engine import ContextEngine
from context.models import ContextLimits
from events.models import EventType
from history.compactor import HistoryCompactor
from history.store import HistoryStore
from history.summarizer import HistorySummarizer
from state.extractor import StateExtractor
from state.registry import SessionStateRegistry
from state.service import StateService
from state.store import StateStore
from state.updater import TurnStateUpdater

TURNS = [
    "我喜欢干型酒，预算 800 元，想买两瓶波尔多产区的酒。",
    "那帮我推荐一款吧，最好适合配牛排。",
    "预算改成 1000 元，其他不变。",
]


async def main() -> None:
    args = _parse_args()
    _require_env()

    db_path = _make_db_path()

    llm = LLM()
    state_store = StateStore(db_path)
    state_store.initialize()
    history_store = HistoryStore(db_path)
    history_store.initialize()

    service = StateService(SessionStateRegistry(state_store))
    limits = ContextLimits()
    engine = ContextEngine(system_prompt=CHAT_SYSTEM_PROMPT, limits=limits)
    summarizer = HistorySummarizer(
        llm, available_tokens=limits.available_tokens
    )
    compactor = HistoryCompactor(
        store=history_store,
        summarizer=summarizer,
        limits=limits,
    )
    updater = TurnStateUpdater(StateExtractor(llm), service)
    orchestrator = TurnOrchestrator(
        state_service=service,
        store=history_store,
        engine=engine,
        compactor=compactor,
        updater=updater,
        llm=llm,
    )

    print(f"model: {getattr(llm, 'model', 'unknown')}")
    print(f"session: {args.session}")
    print(f"db: {db_path}")
    print(
        f"limits: window={limits.window_tokens:,}"
        f" reserve={limits.reserve_output:,}"
        f" history={limits.history_ceiling:,}"
        f" capacity={limits.configured_capacity:,}"
    )
    print("=" * 70)

    try:
        for index, user_input in enumerate(TURNS[: args.turns], start=1):
            print(f"\n[turn {index}] user: {user_input}")

            context = await orchestrator.begin_turn(
                args.session, turn_id=f"turn-{index}"
            )

            answer = ""
            report = None
            compaction = None
            warnings: list[str] = []
            error = None
            error_code = None

            async for event in orchestrator.run_chat(context, user_input):
                if event.type is EventType.FINAL_ANSWER:
                    answer = event.data.get("content", "")
                elif event.type is EventType.RUN_FINISHED:
                    error = event.data.get("error")
                    error_code = event.data.get("error_code")
                    report = event.data.get("context")
                    compaction = event.data.get("compaction")
                    warnings = list(event.data.get("warnings") or ())

            if error is not None:
                print(f"[turn {index}] failed ({error_code or 'error'}): {error}")
                break

            print(f"[turn {index}] assistant: {answer}")

            if report is not None:
                print(
                    "context:"
                    f" total={report['total_tokens']}"
                    f" window={report['window_utilization']:.1%}"
                    f" history={report['history_tokens']}"
                    f" active_turns={report['active_turns']}"
                    f" capacity={report['configured_capacity']:,}"
                )

            if compaction is not None:
                print(
                    "compaction:"
                    f" decision={compaction['decision']}"
                    f" compacted={compaction['compacted']}"
                    f" covered={compaction['covered_through_seq']}"
                )

            for warning in warnings:
                print(f"[warning] {warning}")

            snapshot = await service.get(args.session)
            print(f"[turn {index}] state version: {snapshot.version}")
            print(snapshot.state.model_dump_json(indent=2))
            print("=" * 70)

        final = await service.get(args.session)
        print("\n[final]")
        print(f"version: {final.version}")
        print(final.state.model_dump_json(indent=2))
        print(f"turns: {history_store.count_turns(args.session)}")

    finally:
        await updater.drain()
        if args.keep_db:
            print(f"\n保留数据库：{db_path}")
        else:
            db_path.unlink(missing_ok=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="真实 LLM 的多轮上下文/状态端到端冒烟（v2 链路）"
    )
    parser.add_argument("--session", default="real-e2e")
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="保留临时 SQLite 文件，便于检查最终状态",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=len(TURNS),
        help=f"运行前几轮，默认 {len(TURNS)}",
    )
    return parser.parse_args()


def _require_env() -> None:
    missing = [
        name
        for name in ("LLM_API_KEY", "LLM_BASE_URL")
        if not os.getenv(name)
    ]
    if missing:
        raise SystemExit(
            "缺少环境变量："
            + ", ".join(missing)
            + "。请先在 .env 中配置 LLM_API_KEY / LLM_BASE_URL。"
        )


def _make_db_path() -> Path:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="real-llm-e2e-",
        suffix=".db",
        delete=False,
    ) as handle:
        return Path(handle.name)


if __name__ == "__main__":
    asyncio.run(main())
