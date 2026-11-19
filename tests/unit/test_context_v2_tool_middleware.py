import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.tool_middleware import (
    INVALID_ARGUMENTS,
    TOOL_NOT_ALLOWED,
    TOOL_NOT_EXPOSED,
    TOOL_NOT_FOUND,
    ToolMiddleware,
)
from tools.registry import ToolRegistry


# --------------------------------------------------------------------- 工具


def get_weather(city: str) -> str:
    """查询天气。

    Args:
        city: 城市名。
    """
    return f"{city} 晴"


def list_wines() -> list[str]:
    """列出酒单。"""
    return ["Bordeaux"]


def get_current_time() -> str:
    """返回当前时间。"""
    return "12:00"


@pytest.fixture
def registry() -> ToolRegistry:
    instance = ToolRegistry()
    instance.register(get_weather)
    instance.register(list_wines)
    instance.register(get_current_time)
    return instance


class FakeRunner:
    def __init__(self, *, delays=None, failures=None, exceptions=None):
        self.delays = dict(delays or {})
        self.failures = dict(failures or {})
        self.exceptions = dict(exceptions or {})
        self.calls: list[tuple[str, dict]] = []
        self.active = 0
        self.max_active = 0

    async def run(self, name, arguments):
        self.calls.append((name, arguments))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            delay = self.delays.get(name, 0)
            if delay:
                await asyncio.sleep(delay)
            if name in self.exceptions:
                raise self.exceptions[name]
            if name in self.failures:
                return self.failures[name]
            return {"success": True, "result": f"{name}-ok"}
        finally:
            self.active -= 1


def make_call(
    call_id: str = "call-1",
    name: str = "get_weather",
    arguments="{\"city\": \"上海\"}",
) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def run(coro):
    return asyncio.run(coro)


def body_of(message: dict) -> dict:
    assert message["role"] == "tool"
    assert isinstance(message["tool_call_id"], str)
    return json.loads(message["content"])


def error_code(message: dict) -> str:
    return body_of(message)["error"]


# --------------------------------------------------------------------- 正常执行


def test_executes_exposed_tool_and_returns_success_message(registry):
    runner = FakeRunner()
    middleware = ToolMiddleware(registry=registry, runner=runner)

    messages = run(
        middleware.execute([make_call()], exposed_tools=["get_weather"])
    )

    assert len(messages) == 1
    assert messages[0]["tool_call_id"] == "call-1"
    assert body_of(messages[0]) == {
        "success": True,
        "result": "get_weather-ok",
    }
    assert runner.calls == [("get_weather", {"city": "上海"})]


def test_accepts_sdk_style_objects(registry):
    runner = FakeRunner()
    middleware = ToolMiddleware(registry=registry, runner=runner)
    call = SimpleNamespace(
        id="call-sdk",
        function=SimpleNamespace(
            name="get_weather", arguments='{"city": "波尔多"}'
        ),
    )

    messages = run(
        middleware.execute([call], exposed_tools=["get_weather"])
    )

    assert messages[0]["tool_call_id"] == "call-sdk"
    assert runner.calls == [("get_weather", {"city": "波尔多"})]


def test_exposed_tools_accepts_schemas(registry):
    runner = FakeRunner()
    middleware = ToolMiddleware(registry=registry, runner=runner)

    messages = run(
        middleware.execute(
            [make_call()],
            exposed_tools=[registry.get_tool_schema("get_weather")],
        )
    )

    assert body_of(messages[0])["success"] is True


def test_empty_arguments_string_is_empty_object(registry):
    runner = FakeRunner()
    middleware = ToolMiddleware(registry=registry, runner=runner)

    messages = run(
        middleware.execute(
            [make_call(name="list_wines", arguments="")],
            exposed_tools=["list_wines"],
        )
    )

    assert body_of(messages[0])["success"] is True
    assert runner.calls == [("list_wines", {})]


def test_dict_arguments_are_accepted(registry):
    runner = FakeRunner()
    middleware = ToolMiddleware(registry=registry, runner=runner)

    messages = run(
        middleware.execute(
            [make_call(arguments={"city": "上海"})],
            exposed_tools=["get_weather"],
        )
    )

    assert body_of(messages[0])["success"] is True
    assert runner.calls == [("get_weather", {"city": "上海"})]


def test_no_tool_calls_returns_empty_list(registry):
    middleware = ToolMiddleware(registry=registry, runner=FakeRunner())

    assert run(middleware.execute([], exposed_tools=["get_weather"])) == []


# --------------------------------------------------------------------- 四层校验


def test_unknown_tool_returns_tool_not_found(registry):
    runner = FakeRunner()
    middleware = ToolMiddleware(registry=registry, runner=runner)

    messages = run(
        middleware.execute(
            [make_call(name="make_up_a_tool")],
            exposed_tools=["make_up_a_tool"],
        )
    )

    assert error_code(messages[0]) == TOOL_NOT_FOUND
    assert runner.calls == []


def test_known_but_not_exposed_returns_tool_not_exposed(registry):
    runner = FakeRunner()
    middleware = ToolMiddleware(registry=registry, runner=runner)

    messages = run(middleware.execute([make_call()], exposed_tools=[]))

    assert error_code(messages[0]) == TOOL_NOT_EXPOSED
    assert runner.calls == []


def test_whitelist_rejects_tool(registry):
    runner = FakeRunner()
    middleware = ToolMiddleware(
        registry=registry,
        runner=runner,
        allowed_tools=frozenset({"list_wines"}),
    )

    messages = run(
        middleware.execute([make_call()], exposed_tools=["get_weather"])
    )

    assert error_code(messages[0]) == TOOL_NOT_ALLOWED
    assert runner.calls == []


def test_registry_is_checked_before_exposed(registry):
    middleware = ToolMiddleware(registry=registry, runner=FakeRunner())

    messages = run(
        middleware.execute(
            [make_call(name="make_up_a_tool")], exposed_tools=[]
        )
    )

    assert error_code(messages[0]) == TOOL_NOT_FOUND


def test_exposed_is_checked_before_whitelist(registry):
    middleware = ToolMiddleware(
        registry=registry,
        runner=FakeRunner(),
        allowed_tools=frozenset({"list_wines"}),
    )

    messages = run(middleware.execute([make_call()], exposed_tools=[]))

    assert error_code(messages[0]) == TOOL_NOT_EXPOSED


def test_invalid_json_arguments_return_invalid_arguments(registry):
    runner = FakeRunner()
    middleware = ToolMiddleware(registry=registry, runner=runner)

    messages = run(
        middleware.execute(
            [make_call(arguments='{"city": ')], exposed_tools=["get_weather"]
        )
    )

    assert error_code(messages[0]) == INVALID_ARGUMENTS
    assert runner.calls == []


def test_non_object_arguments_return_invalid_arguments(registry):
    middleware = ToolMiddleware(registry=registry, runner=FakeRunner())

    messages = run(
        middleware.execute(
            [make_call(arguments="[1, 2]")], exposed_tools=["get_weather"]
        )
    )

    assert error_code(messages[0]) == INVALID_ARGUMENTS


def test_missing_required_argument_returns_invalid_arguments(registry):
    middleware = ToolMiddleware(registry=registry, runner=FakeRunner())

    messages = run(
        middleware.execute(
            [make_call(arguments="{}")], exposed_tools=["get_weather"]
        )
    )

    body = body_of(messages[0])
    assert body["error"] == INVALID_ARGUMENTS
    assert "city" in body["message"]


def test_extra_argument_returns_invalid_arguments(registry):
    middleware = ToolMiddleware(registry=registry, runner=FakeRunner())

    messages = run(
        middleware.execute(
            [make_call(arguments='{"city": "上海", "extra": 1}')],
            exposed_tools=["get_weather"],
        )
    )

    assert error_code(messages[0]) == INVALID_ARGUMENTS


# --------------------------------------------------------------------- 执行失败


def test_runner_exception_becomes_failure_message(registry):
    runner = FakeRunner(exceptions={"get_weather": RuntimeError("boom")})
    middleware = ToolMiddleware(registry=registry, runner=runner)

    messages = run(
        middleware.execute([make_call()], exposed_tools=["get_weather"])
    )

    body = body_of(messages[0])
    assert body["success"] is False
    assert "boom" in body["error"]
    assert messages[0]["tool_call_id"] == "call-1"


def test_runner_failure_is_passed_through(registry):
    runner = FakeRunner(
        failures={"get_weather": {"success": False, "error": "API down"}}
    )
    middleware = ToolMiddleware(registry=registry, runner=runner)

    messages = run(
        middleware.execute([make_call()], exposed_tools=["get_weather"])
    )

    assert body_of(messages[0]) == {"success": False, "error": "API down"}


def test_timeout_becomes_failure_message(registry):
    runner = FakeRunner(delays={"get_weather": 0.05})
    middleware = ToolMiddleware(
        registry=registry, runner=runner, timeout=0.01
    )

    messages = run(
        middleware.execute([make_call()], exposed_tools=["get_weather"])
    )

    body = body_of(messages[0])
    assert body["success"] is False
    assert "超时" in body["error"]


# --------------------------------------------------------------------- 并发与顺序


def test_calls_run_concurrently_and_keep_input_order(registry):
    runner = FakeRunner(
        delays={"get_weather": 0.05, "get_current_time": 0.01}
    )
    middleware = ToolMiddleware(registry=registry, runner=runner)
    calls = [
        make_call("call-slow", "get_weather"),
        make_call("call-fast", "get_current_time", ""),
        make_call("call-wines", "list_wines", ""),
    ]

    messages = run(
        middleware.execute(
            calls,
            exposed_tools=["get_weather", "get_current_time", "list_wines"],
        )
    )

    assert [message["tool_call_id"] for message in messages] == [
        "call-slow",
        "call-fast",
        "call-wines",
    ]
    assert [body_of(message)["success"] for message in messages] == [
        True,
        True,
        True,
    ]
    assert runner.max_active == 3


def test_mixed_batch_still_returns_one_message_per_call(registry):
    runner = FakeRunner(exceptions={"list_wines": RuntimeError("nope")})
    middleware = ToolMiddleware(registry=registry, runner=runner)
    calls = [
        make_call("call-ok", "get_weather"),
        make_call("call-missing", "make_up_a_tool"),
        make_call("call-boom", "list_wines", ""),
    ]

    messages = run(
        middleware.execute(
            calls, exposed_tools=["get_weather", "make_up_a_tool", "list_wines"]
        )
    )

    assert [message["tool_call_id"] for message in messages] == [
        "call-ok",
        "call-missing",
        "call-boom",
    ]
    assert body_of(messages[0])["success"] is True
    assert error_code(messages[1]) == TOOL_NOT_FOUND
    assert body_of(messages[2])["success"] is False


# --------------------------------------------------------------------- 参数校验


def test_constructor_rejects_bad_registry():
    with pytest.raises(TypeError, match="registry"):
        ToolMiddleware(registry=object(), runner=FakeRunner())


def test_constructor_rejects_bad_runner(registry):
    with pytest.raises(TypeError, match="runner"):
        ToolMiddleware(registry=registry, runner=object())


def test_constructor_rejects_bad_allowed_tools(registry):
    with pytest.raises(TypeError, match="allowed_tools"):
        ToolMiddleware(
            registry=registry,
            runner=FakeRunner(),
            allowed_tools=["get_weather"],
        )


def test_constructor_rejects_non_positive_timeout(registry):
    with pytest.raises(ValueError, match="timeout"):
        ToolMiddleware(registry=registry, runner=FakeRunner(), timeout=0)


def test_tool_call_without_id_is_rejected(registry):
    middleware = ToolMiddleware(registry=registry, runner=FakeRunner())

    with pytest.raises(ValueError, match="id"):
        run(
            middleware.execute(
                [{"function": {"name": "get_weather", "arguments": "{}"}}],
                exposed_tools=["get_weather"],
            )
        )


def test_tool_call_without_function_is_rejected(registry):
    middleware = ToolMiddleware(registry=registry, runner=FakeRunner())

    with pytest.raises(ValueError, match="function"):
        run(
            middleware.execute(
                [{"id": "call-1"}], exposed_tools=["get_weather"]
            )
        )


def test_exposed_tools_rejects_empty_name(registry):
    middleware = ToolMiddleware(registry=registry, runner=FakeRunner())

    with pytest.raises(ValueError, match="exposed_tools"):
        run(middleware.execute([make_call()], exposed_tools=[""]))
