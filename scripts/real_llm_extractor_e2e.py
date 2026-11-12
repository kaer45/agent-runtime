"""真实 LLM 的 StateExtractor 冒烟：观察 4 个复杂样例的提取结果。

默认会依次运行 4 个 case：
1. prompt 示例 1：偏好 / 约束 / 目标
2. prompt 示例 2：出差事实 / 购买目标 / 偏好
3. 已有状态更新：改预算、放弃目标、新增过敏约束
4. 复杂结婚采购：多偏好、多约束、事实与目标混合

用法：
    .venv\\Scripts\\python.exe -m scripts.real_llm_extractor_e2e

只看某一个：
    .venv\\Scripts\\python.exe -m scripts.real_llm_extractor_e2e --case 3

打印完整 system prompt：
    .venv\\Scripts\\python.exe -m scripts.real_llm_extractor_e2e --show-prompt
"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass, field

from agent.llm import LLM
from state.extractor import (
    EXTRACTION_SYSTEM_PROMPT,
    StateExtractionError,
    StateExtractor,
)
from state.models import Goal, SessionState
from state.service import StateApplyError, apply_delta


@dataclass(frozen=True)
class Case:
    name: str
    focus: str
    state: SessionState = field(default_factory=SessionState)
    messages: tuple[dict, ...] = ()


def build_cases() -> list[Case]:
    return [
        Case(
            name="prompt-1：偏好/约束/目标",
            focus=(
                "预期重点：wine_style=preference；预算=constraints；"
                "“两瓶”留在 goal，不进 constraints；波尔多不要进 facts。"
            ),
            messages=(
                {
                    "role": "user",
                    "content": "我喜欢干型酒，预算不超过 800 元，想买两瓶波尔多的酒。",
                },
            ),
        ),
        Case(
            name="prompt-2：出差事实/购买目标/偏好",
            focus=(
                "预期重点：出差时间/城市=fact；酒风格和有机=preference；"
                "2019 年份留在 goal，不放进 facts；预算进 constraints。"
            ),
            messages=(
                {
                    "role": "user",
                    "content": (
                        "我下周三要去波尔多出差，想买两瓶 2019 年份的干型红酒，"
                        "预算不超过 800 元，最好是有机的。"
                    ),
                },
            ),
        ),
        Case(
            name="已有状态更新：改预算/放弃目标/过敏",
            focus=(
                "预期重点：旧预算先 remove 再 add 新预算；"
                "goals_abandon 使用当前 goals[].id；"
                "亚硫酸盐过敏进 constraints，不要进 preferences。"
            ),
            state=SessionState(
                preferences={"wine_style": "dry"},
                constraints=["预算不超过 800 元"],
                goals=[
                    Goal(
                        id="g_111111111111",
                        text="买两瓶干型红酒",
                    )
                ],
                progress={"wine_selection": "in_progress"},
            ),
            messages=(
                {
                    "role": "user",
                    "content": (
                        "预算改成 1000 元，还是干型；之前那个买酒的目标先不做了。"
                        "另外我对亚硫酸盐过敏，以后别推荐含亚硫酸盐的酒。"
                    ),
                },
                {
                    "role": "assistant",
                    "content": "好的，预算调整到 1000 元，并记录亚硫酸盐过敏。",
                },
            ),
        ),
        Case(
            name="复杂结婚采购：多偏好/多约束/事实",
            focus=(
                "预期重点：酸度低、酒体饱满=preference；"
                "结婚/下个月=fact；订 20 瓶=goal；"
                "每瓶预算、总预算、橡木桶敏感=constraints 或偏好，观察模型如何拆分。"
            ),
            messages=(
                {
                    "role": "user",
                    "content": (
                        "我其实不太喜欢太酸的，最好酒体饱满一点；"
                        "下个月准备结婚，想订 20 瓶红酒，"
                        "预算每瓶 300 左右，总预算 6000，别超过。"
                        "我对橡木桶味比较敏感，太重的不行。"
                    ),
                },
                {
                    "role": "assistant",
                    "content": "明白了，我按这些条件帮你筛选。",
                },
            ),
        ),
    ]


async def run_case(
    extractor: StateExtractor,
    case: Case,
    index: int,
) -> None:
    print("=" * 78)
    print(f"[case {index}] {case.name}")
    print(f"focus: {case.focus}")
    print("-" * 78)
    print("current state:")
    print(case.state.model_dump_json(indent=2))
    print("messages:")
    for message in case.messages:
        print(f"  {message['role']}: {message['content']}")
    print("-" * 78)

    try:
        delta = await extractor.extract(case.state, list(case.messages))
    except StateExtractionError as exc:
        print(f"extract failed: {exc}")
        return

    print("raw delta:")
    print(delta.model_dump_json(indent=2))

    try:
        updated = apply_delta(case.state, delta)
    except (StateApplyError, ValueError) as exc:
        print(f"apply_delta failed: {exc}")
        return

    print("state after apply_delta:")
    print(updated.model_dump_json(indent=2))

    changed = _changed_fields(case.state, updated)
    print("changed fields:", ", ".join(changed) if changed else "(none)")


def _changed_fields(before: SessionState, after: SessionState) -> list[str]:
    fields = ("preferences", "goals", "constraints", "progress", "facts")
    return [
        name
        for name in fields
        if getattr(before, name) != getattr(after, name)
    ]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="真实 LLM 的 StateExtractor 冒烟"
    )
    parser.add_argument(
        "--case",
        type=int,
        choices=range(1, 5),
        help="只运行第 N 个 case（1-4）",
    )
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="先打印完整的 EXTRACTION_SYSTEM_PROMPT",
    )
    return parser.parse_args()


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

    if args.show_prompt:
        print("=" * 78)
        print("EXTRACTION_SYSTEM_PROMPT")
        print("=" * 78)
        print(EXTRACTION_SYSTEM_PROMPT)

    llm = LLM()
    extractor = StateExtractor(llm)
    cases = list(enumerate(build_cases(), start=1))
    if args.case is not None:
        cases = [cases[args.case - 1]]

    print(f"model: {llm.model}")
    print(f"cases: {len(cases)}")

    for index, case in cases:
        await run_case(extractor, case, index)


if __name__ == "__main__":
    asyncio.run(main())
