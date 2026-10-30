import asyncio
import json
from types import SimpleNamespace

from state.extractor import StateExtractor
from state.models import SessionState


class FakeLLM:
    """返回一个固定的 submit_state_delta 工具调用，不需要 API key。"""

    async def chat(self, messages, **kwargs):
        payload = {
            "preferences_set": {"budget": 800},
            "preferences_remove": [],
            "facts_set": {"产区": "勃艮第"},
            "facts_remove": [],
            "goals_add": ["再买两瓶干型酒"],
            "goals_complete": [],
            "goals_abandon": [],
            "constraints_add": ["预算不超过 800 元"],
            "constraints_remove": ["预算不超过 500 元"],
            "progress_set": {"选酒": "in_progress"},
            "progress_remove": [],
        }
        call = SimpleNamespace(
            id="call_state",
            function=SimpleNamespace(
                name="submit_state_delta",
                arguments=json.dumps(payload, ensure_ascii=False),
            ),
        )
        return SimpleNamespace(content=None, tool_calls=[call])


async def main() -> None:
    state = SessionState(
        preferences={"style": "dry", "budget": 500},
        goals=[{"text": "买两瓶干型酒"}],
        constraints=["预算不超过 500 元"],
        progress={"选酒": "in_progress"},
        facts={"产区": "波尔多"},
    )

    messages = [
        {"role": "user", "content": "预算改成 800，换勃艮第，再买两瓶。"},
        {"role": "assistant", "content": "好的，我记下了。"},
    ]

    delta = await StateExtractor(FakeLLM()).extract(state, messages)

    print("是否为空增量:", delta.is_empty())
    print(delta.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
