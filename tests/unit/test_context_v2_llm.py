import asyncio
from types import SimpleNamespace

from agent.llm import LLM, LLMResponse


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.kwargs: dict | None = None
        self.calls = 0

    async def create(self, **kwargs):
        self.kwargs = kwargs
        self.calls += 1
        return self.response


def make_llm(
    *,
    content: str = "你好",
    finish_reason: str | None = "stop",
    usage=None,
) -> tuple[LLM, FakeCompletions]:
    message = SimpleNamespace(content=content)
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )
    completions = FakeCompletions(response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return LLM(client=client), completions


def test_chat_with_meta_returns_metadata():
    usage = SimpleNamespace(model_dump=lambda: {"total_tokens": 7})
    llm, _ = make_llm(content="摘要", finish_reason="stop", usage=usage)

    response = asyncio.run(
        llm.chat_with_meta([{"role": "user", "content": "hi"}])
    )

    assert isinstance(response, LLMResponse)
    assert response.message.content == "摘要"
    assert response.finish_reason == "stop"
    assert response.usage == {"total_tokens": 7}


def test_chat_with_meta_forwards_options():
    llm, completions = make_llm()

    asyncio.run(
        llm.chat_with_meta(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function"}],
            tool_choice="auto",
            thinking=False,
            temperature=0,
            max_tokens=100,
        )
    )

    assert completions.kwargs["model"] == llm.model
    assert completions.kwargs["max_tokens"] == 100
    assert completions.kwargs["tools"] == [{"type": "function"}]
    assert completions.kwargs["tool_choice"] == "auto"
    assert completions.kwargs["temperature"] == 0
    assert completions.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_chat_with_meta_omits_unset_options():
    llm, completions = make_llm()

    asyncio.run(llm.chat_with_meta([{"role": "user", "content": "hi"}]))

    assert "max_tokens" not in completions.kwargs
    assert "tools" not in completions.kwargs
    assert "extra_body" not in completions.kwargs


def test_chat_keeps_old_signature():
    llm, _ = make_llm(content="ok")

    message = asyncio.run(llm.chat([{"role": "user", "content": "hi"}]))

    assert message.content == "ok"


def test_usage_is_none_when_absent():
    llm, _ = make_llm()

    response = asyncio.run(llm.chat_with_meta([]))

    assert response.usage is None
