"""真实 LLM 的端到端冒烟：多轮对话 + 状态提取 + 上下文组装。

默认直接使用 StateService 应用 delta；等第 19 课的 TurnStateUpdater
实现后，可以加 --updater 走后台更新/幂等路径。

用法：
    .venv\\Scripts\\python.exe -m scripts.real_llm_state_e2e

可选：
    --session real-e2e
    --updater
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
from context.budget import TokenBudget
from context.builder import ContextBuilder
from state.extractor import StateExtractor
from state.service import StateService
from state.session import SessionRegistry
from state.store import StateStore


TURNS = [
    "我喜欢干型酒，预算 800 元，想买两瓶波尔多产区的酒。",
    "那帮我推荐一款吧，最好适合配牛排。",
    "预算改成 1000 元，其他不变。",
]


async def main() -> None:
    args = _parse_args()

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

    db_path = _make_db_path()
    store = StateStore(db_path)
    store.initialize()

    llm = LLM()
    service = StateService(SessionRegistry(store))
    extractor = StateExtractor(llm)
    builder = ContextBuilder(
        system_prompt=CHAT_SYSTEM_PROMPT,
        budget=TokenBudget(max_tokens=2000, reserve_output=400),
        state_max_tokens=600,
    )

    updater = None
    if args.updater:
        try:
            from state.updater import TurnStateUpdater
        except ImportError as exc:
            raise SystemExit(
                "state/updater.py 尚未实现，去掉 --updater 可先走直接应用模式。"
            ) from exc
        updater = TurnStateUpdater(extractor, service)

    history: list[dict] = []
    session_id = args.session

    print(f"model: {llm.model}")
    print(f"session: {session_id}")
    print(f"db: {db_path}")
    print("=" * 70)

    try:
        for index, user_input in enumerate(TURNS[: args.turns], start=1):
            print(f"\n[turn {index}] user: {user_input}")

            snapshot = await service.get(session_id)
            built = builder.build(
                user_input,
                state=snapshot.state,
                history=history,
            )

            print(
                "context:"
                f" state_tokens={built.state_tokens}"
                f" summary_tokens={built.summary_tokens}"
                f" original={built.original_tokens}"
                f" final={built.final_tokens}"
                f" dropped={built.dropped_indices}"
            )

            response = await llm.chat(
                messages=built.messages,
                temperature=0.3,
            )
            answer = response.content or ""
            print(f"[turn {index}] assistant: {answer}")

            turn_messages = [
                {"role": "user", "content": user_input},
                {"role": "assistant", "content": answer},
            ]

            if updater is not None:
                result = await updater.update(
                    session_id,
                    f"turn-{index}",
                    turn_messages,
                    expected_version=snapshot.version,
                )
                if result.failed:
                    raise RuntimeError(f"状态更新失败: {result.error}")
                if result.snapshot is None:
                    raise RuntimeError("状态更新没有返回 snapshot")

                # updater 自己负责提取，这里只打印它真正应用的 delta。
                # 不要再单独调用 extractor，否则会多调一次 LLM，
                # 第二次提取结果可能和第一次不同。
                delta = result.delta
                updated = result.snapshot
                print("[turn] delta (from updater):")
                print(delta.model_dump_json(indent=2))
                if not result.applied:
                    print("[turn] no state change")
            else:
                delta = await extractor.extract(snapshot.state, turn_messages)
                print("[turn] delta:")
                print(delta.model_dump_json(indent=2))

                if delta.is_empty():
                    print("[turn] no state change")
                    updated = snapshot
                else:
                    updated = await service.apply(
                        session_id,
                        delta,
                        expected_version=snapshot.version,
                    )

            print(
                f"[turn {index}] state version:"
                f" {snapshot.version} -> {updated.version}"
            )
            print(updated.state.model_dump_json(indent=2))

            history.extend(turn_messages)
            print("=" * 70)

        if updater is not None:
            await updater.drain()

        final = await service.get(session_id)
        print("\n[final]")
        print(f"version: {final.version}")
        print(final.state.model_dump_json(indent=2))

    finally:
        if args.keep_db:
            print(f"\n保留数据库：{db_path}")
        else:
            db_path.unlink(missing_ok=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="真实 LLM 的多轮上下文/状态端到端冒烟"
    )
    parser.add_argument("--session", default="real-e2e")
    parser.add_argument(
        "--updater",
        action="store_true",
        help="使用第 19 课的 TurnStateUpdater（需要先实现 state/updater.py）",
    )
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
