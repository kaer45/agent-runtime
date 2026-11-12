"""第 20.6 课冒烟：两轮记忆 + 重启恢复 + session_id。

用法：
    .venv\\Scripts\\python.exe -m scripts.lesson20_6_smoke
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent.runtime import AgentRuntime


class FakeToolCall:
    def __init__(self, name: str, arguments: dict, call_id: str):
        self.id = call_id
        self.function = SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        )


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(
        self,
        messages,
        tools=None,
        tool_choice=None,
        thinking=None,
        temperature=None,
    ):
        self.calls.append(list(messages))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def classify_chat() -> FakeToolCall:
    return FakeToolCall(
        "classify_intent",
        {"intent": "chat", "reason": "smoke"},
        "intent-1",
    )


def state_delta(**payload) -> FakeToolCall:
    return FakeToolCall("submit_state_delta", payload, "state-1")


def final_answer(events) -> str:
    for event in events:
        if event.type.value == "final_answer":
            return event.data.get("content", "")
    return ""


async def run_turn(runtime, user_input: str) -> str:
    events = [
        event
        async for event in runtime.run(user_input, session_id="smoke-session")
    ]
    print("events:", [event.type.value for event in events])
    return final_answer(events)


async def main() -> None:
    with tempfile.NamedTemporaryFile(
        dir=Path.cwd(),
        prefix="lesson20-6-smoke-",
        suffix=".db",
        delete=False,
    ) as handle:
        db_path = Path(handle.name)

    try:
        llm = FakeLLM([
            FakeMessage(tool_calls=[classify_chat()]),
            FakeMessage(content="推荐波尔多。"),
            FakeMessage(tool_calls=[state_delta(preferences_set={"style": "dry"})]),
            FakeMessage(tool_calls=[classify_chat()]),
            FakeMessage(content="可以看波尔多。"),
            FakeMessage(tool_calls=[state_delta()]),
        ])
        runtime = AgentRuntime(llm=llm, db_path=db_path)

        print("turn 1:", await run_turn(runtime, "我喜欢干型酒。"))
        print("history:", runtime.history_store.count("smoke-session"))
        state = await runtime.state_service.get("smoke-session")
        print("state:", state.state.model_dump_json())
        print("state version:", state.version)

        print("turn 2:", await run_turn(runtime, "推荐一款。"))
        second_prompt = llm.calls[4]
        print(
            "turn 2 prompt has previous user:",
            any(m.get("content") == "我喜欢干型酒。" for m in second_prompt),
        )
        print("history:", runtime.history_store.count("smoke-session"))

        restarted_llm = FakeLLM([
            FakeMessage(tool_calls=[classify_chat()]),
            FakeMessage(content="预算 800 的波尔多。"),
            FakeMessage(tool_calls=[state_delta()]),
        ])
        restarted = AgentRuntime(llm=restarted_llm, db_path=db_path)
        print("turn 3 after restart:", await run_turn(restarted, "那预算 800 呢？"))
        third_prompt = restarted_llm.calls[1]
        print(
            "restart prompt has turn 1:",
            any(m.get("content") == "我喜欢干型酒。" for m in third_prompt),
        )
        print("history after restart:", restarted.history_store.count("smoke-session"))
        final_state = await restarted.state_service.get("smoke-session")
        print("final state:", final_state.state.model_dump_json())
    finally:
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
