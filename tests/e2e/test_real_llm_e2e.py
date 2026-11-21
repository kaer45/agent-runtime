"""真实大模型的端到端测试。

默认跳过：需要设置 ``LLM_API_KEY`` 和 ``LLM_BASE_URL``。

    $env:LLM_API_KEY = "..."
    $env:LLM_BASE_URL = "https://api.deepseek.com"
    .venv\\Scripts\\python.exe -m pytest tests/e2e -q

只有两个用例，成本约 10~12 次真实调用；CI 不会执行。
每个用例的所有轮次都跑在**同一个事件循环**里：真实 ``LLM`` 的 httpx
客户端绑定在创建它的事件循环上，跨 ``asyncio.run`` 复用会报
“Event loop is closed”。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from agent.llm import LLM
from context.models import ContextLimits
from state.extractor import StateExtractor
from tests.integration.harness import (
    SESSION,
    answer_of,
    contents_of,
    final_data,
    make_harness,
    seed_turn,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("LLM_API_KEY") or not os.getenv("LLM_BASE_URL"),
    reason="真实 LLM 测试：需要 LLM_API_KEY / LLM_BASE_URL",
)


class RecordingLLM:
    """包一层真实 LLM，记录每次调用的 messages 和 tools。"""

    def __init__(self, inner):
        self.inner = inner
        self.chat_calls: list[dict] = []
        self.summary_calls: list[list[dict]] = []

    async def chat(self, messages, tools=None, **kwargs):
        self.chat_calls.append({"messages": list(messages), "tools": tools})
        return await self.inner.chat(messages, tools=tools, **kwargs)

    async def chat_with_meta(self, messages, **kwargs):
        self.summary_calls.append(list(messages))
        return await self.inner.chat_with_meta(messages, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def answer_messages(harness) -> list[dict]:
    """只取“回答”调用；状态提取也走 llm.chat，但会带 tools。"""
    calls = [
        call["messages"]
        for call in harness.llm.chat_calls
        if call["tools"] is None
    ]
    assert calls, "没有找到无 tools 的回答调用"
    return calls[-1]


def make_real_harness(db_path, *, limits: ContextLimits | None = None):
    llm = RecordingLLM(LLM())
    return make_harness(
        db_path,
        limits=limits,
        llm=llm,
        extractor=StateExtractor(llm),
    )


def test_real_llm_three_turn_conversation(tmp_path):
    harness = make_real_harness(tmp_path / "real.db")

    async def scenario():
        _, first = await harness.run_turn(
            "我喜欢干型酒，预算 800 元，想买两瓶波尔多产区的酒。",
            turn_id="real-1",
        )
        first_data = final_data(first)
        assert "error" not in first_data
        assert (
            first_data["context"]["total_tokens"]
            <= harness.limits.available_tokens
        )
        assert answer_of(first).strip()

        _, second = await harness.run_turn(
            "那帮我推荐一款吧，最好适合配牛排。", turn_id="real-2"
        )
        assert "error" not in final_data(second)

        _, third = await harness.run_turn(
            "预算改成 1000 元，其他不变。", turn_id="real-3"
        )
        assert "error" not in final_data(third)
        assert (
            third[-1].data["context"]["total_tokens"]
            <= harness.limits.available_tokens
        )

        assert harness.history.count_turns(SESSION) == 3

        messages = answer_messages(harness)
        contents = contents_of(messages)
        assert "我喜欢干型酒，预算 800 元，想买两瓶波尔多产区的酒。" in contents
        assert messages[-1] == {
            "role": "user",
            "content": "预算改成 1000 元，其他不变。",
        }

        state = await harness.service.get(SESSION)
        # 真实模型可能不产生 delta；只要产生了，state 就必须出现在上下文里。
        if state.version > 0:
            assert any(content.startswith("<state>") for content in contents)

    asyncio.run(scenario())


def test_real_llm_summary_is_rendered_as_user_message(tmp_path):
    # 8 条 seed 各 100 token；ceiling 1000 让 last6 放得下、总量必然超过 60%。
    limits = ContextLimits(history_ceiling=1_000)
    harness = make_real_harness(tmp_path / "real-summary.db", limits=limits)
    for index in range(1, 9):
        seed_turn(harness.history, SESSION, index, "酒" * 92)

    async def scenario():
        _, first = await harness.run_turn(
            "请继续推荐一款。", turn_id="real-s1"
        )
        data = final_data(first)
        assert "error" not in data
        assert data["compaction"]["decision"] == "compact"
        assert data["compaction"]["compacted"] is True

        snapshot = harness.history.load(SESSION)
        assert snapshot.summary.text.strip()
        assert snapshot.covered_through_seq >= 1

        _, second = await harness.run_turn(
            "再推荐一款。", turn_id="real-s2"
        )
        assert "error" not in final_data(second)

        summary_messages = [
            content
            for content in contents_of(answer_messages(harness))
            if content.startswith("<summary>")
        ]
        assert summary_messages
        assert "&lt;/summary&gt;" not in summary_messages[0]

    asyncio.run(scenario())
