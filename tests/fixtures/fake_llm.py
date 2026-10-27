from types import SimpleNamespace
import json


class FakeToolCall:
    def __init__(self, name="submit_task_plan", arguments="{}", call_id="call_1"):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


def plan_call(tasks, call_id="call_1"):
    return FakeToolCall(
        arguments=json.dumps({"tasks": tasks}, ensure_ascii=False),
        call_id=call_id,
    )


class FakeLLM:
    """按顺序返回预设响应，并记录每次 chat 的入参。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, messages, tools=None, tool_choice=None, thinking=None, temperature=None):
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "tool_choice": tool_choice,
                "thinking": thinking,
                "temperature": temperature,
            }
        )
        if not self.responses:
            raise AssertionError("FakeLLM 没有更多响应")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp