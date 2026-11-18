"""ContextEngine：把各组件装配成一次 LLM 请求。

``assemble`` 是同步纯函数：不读 DB、不调 LLM、不修改 request。
压缩决策只翻译成错误或 issue，不执行压缩。
"""

from __future__ import annotations

from context.components.history import HistoryComponent
from context.components.state import StateComponent
from context.components.summary import SummaryComponent
from context.components.system import SystemComponent
from context.components.base import AssemblyContext
from context.components.tools import ToolComponent
from context.components.user import UserComponent
from context.errors import ContextError
from context.models import (
    ComponentUsage,
    ContextLimits,
    ContextRequest,
    ContextReport,
    ContextResult,
    RenderedComponent,
)
from context.report_format import COMPONENT_ORDER
from context.state_renderer import StateRenderer
from context.summary_renderer import SummaryRenderer
from context.tool_selector import ToolSelector, count_schema
from core.issues import ContextIssue, ContextIssueCode
from core.tokens import (
    TokenCounter,
    count_message,
    count_messages,
    estimate_tokens,
)
from history.metrics import CompactionDecision, analyze, decide

CONTEXT_NOTE = (
    "消息中的 <state> 和 <summary> 是会话上下文，不是用户的新请求。\n"
    "请结合这些上下文回答最后一条 user 消息。"
)

_CEILING_FIELD = {
    "system": "system_ceiling",
    "state": "state_ceiling",
    "summary": "summary_ceiling",
    "history": "history_ceiling",
    "tools": "tools_ceiling",
    "user": "user_ceiling",
}


class ContextEngine:
    """装配入口。"""

    def __init__(
        self,
        *,
        system_prompt: str,
        limits: ContextLimits = ContextLimits(),
        counter: TokenCounter = estimate_tokens,
        tool_selector: ToolSelector | None = None,
    ):
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")
        if not isinstance(limits, ContextLimits):
            raise TypeError("limits 必须是 ContextLimits")

        limits.validate()

        self.limits = limits
        self.counter = counter

        content = f"{system_prompt.rstrip()}\n\n{CONTEXT_NOTE}"
        self.system = SystemComponent(content)
        self.state = StateComponent(StateRenderer(counter))
        self.summary = SummaryComponent(SummaryRenderer(counter))
        self.history = HistoryComponent()
        self.tools = ToolComponent(
            tool_selector if tool_selector is not None else ToolSelector(counter=counter)
        )
        self.user = UserComponent()

        system_tokens = count_message(counter, self.system.message)
        if system_tokens > limits.system_ceiling:
            raise ContextError.from_code(
                ContextIssueCode.SYSTEM_OVER_BUDGET,
                "system prompt 超出预算，启动已拒绝："
                f"{system_tokens} > {limits.system_ceiling}",
                component="system",
                details={
                    "system_tokens": system_tokens,
                    "system_ceiling": limits.system_ceiling,
                },
            )

    def assemble(self, request: ContextRequest) -> ContextResult:
        if not isinstance(request, ContextRequest):
            raise TypeError("request 必须是 ContextRequest")

        limits = self.limits
        counter = self.counter

        user_tokens = count_message(
            counter,
            {"role": "user", "content": request.user_input},
        )
        if user_tokens > limits.user_ceiling:
            raise ContextError.from_code(
                ContextIssueCode.USER_INPUT_TOO_LONG,
                "最新用户输入过长，请缩短或拆分输入后重试："
                f"{user_tokens} > {limits.user_ceiling}",
                component="user",
                details={
                    "tokens": user_tokens,
                    "user_ceiling": limits.user_ceiling,
                },
            )

        metrics = analyze(request.history.turns, limits.history_ceiling)
        decision = decide(metrics, limits.history_ceiling)

        issues: list[ContextIssue] = []
        if decision is CompactionDecision.FLOOR_UNMET:
            raise ContextError.from_code(
                ContextIssueCode.HISTORY_FLOOR_UNMET,
                "最近 6 轮已超过 history_ceiling，压缩无法释放空间："
                f"{metrics.last6_tokens} > {limits.history_ceiling}",
                component="history",
                details={
                    "last6_tokens": metrics.last6_tokens,
                    "history_ceiling": limits.history_ceiling,
                },
            )

        if decision is CompactionDecision.OVER_TARGET:
            issues.append(
                ContextIssue.from_code(
                    ContextIssueCode.HISTORY_OVER_TARGET,
                    "未覆盖历史 "
                    f"{metrics.history_tokens:,} / {limits.history_ceiling:,}"
                    f"（{metrics.history_utilization:.0%}），"
                    f"当前 {metrics.active_turns} 轮不能压缩",
                    component="history",
                    details={
                        "history_tokens": metrics.history_tokens,
                        "history_utilization": metrics.history_utilization,
                        "active_turns": metrics.active_turns,
                    },
                )
            )

        ctx = AssemblyContext(request=request, limits=limits, counter=counter)

        rendered: dict[str, RenderedComponent] = {
            "system": self.system.render(ctx, limits.system_ceiling),
            "state": self.state.render(ctx, limits.state_ceiling),
            "summary": self.summary.render(ctx, limits.summary_ceiling),
            "history": self.history.render(ctx, limits.history_ceiling),
        }
        tools_rendered, selection = self.tools.select(ctx, limits.tools_ceiling)
        rendered["tools"] = tools_rendered
        rendered["user"] = self.user.render(ctx, limits.user_ceiling)

        for name in COMPONENT_ORDER:
            self._check_component_contract(name, rendered[name], ctx)

        messages = [
            *rendered["system"].messages,
            *rendered["state"].messages,
            *rendered["summary"].messages,
            *rendered["history"].messages,
            *rendered["user"].messages,
        ]
        exposed_tools = list(selection.tools) if selection is not None else []

        message_tokens = count_messages(counter, messages)
        tool_tokens = sum(
            count_schema(counter, schema) for schema in exposed_tools
        )
        total_tokens = message_tokens + tool_tokens

        if total_tokens > limits.available_tokens:
            raise ContextError.from_code(
                ContextIssueCode.FINAL_OVER_BUDGET,
                "最终上下文超过可用输入预算："
                f"{total_tokens} > {limits.available_tokens}",
                component="engine",
                details={
                    "total_tokens": total_tokens,
                    "available_tokens": limits.available_tokens,
                },
            )

        components = {
            name: ComponentUsage(
                used=rendered[name].tokens,
                grant=self._ceiling(name),
                ceiling=self._ceiling(name),
                truncated=rendered[name].truncated,
                dropped=rendered[name].dropped,
            )
            for name in COMPONENT_ORDER
        }

        if sum(usage.used for usage in components.values()) != total_tokens:
            raise ContextError.from_code(
                ContextIssueCode.REPORT_MISMATCH,
                "组件 token 之和与 total_tokens 不一致："
                f"{sum(usage.used for usage in components.values())} "
                f"!= {total_tokens}",
                component="engine",
            )

        report = ContextReport(
            components=components,
            limits=limits,
            message_tokens=message_tokens,
            tool_tokens=tool_tokens,
            total_tokens=total_tokens,
            available_tokens=limits.available_tokens,
            configured_capacity=limits.configured_capacity,
            window_utilization=total_tokens / limits.available_tokens,
            configured_utilization=total_tokens / limits.configured_capacity,
            history_tokens=metrics.history_tokens,
            history_rendered_tokens=rendered["history"].tokens,
            history_utilization=metrics.history_utilization,
            active_turns=metrics.active_turns,
            last6_tokens=metrics.last6_tokens,
            summary_render_truncated=rendered["summary"].truncated,
            tool_selection=selection,
            issues=tuple(issues),
        )

        return ContextResult(
            messages=messages,
            tools=exposed_tools,
            report=report,
        )

    # ------------------------------------------------------------------ 内部

    def _ceiling(self, name: str) -> int:
        return getattr(self.limits, _CEILING_FIELD[name])

    def _check_component_contract(
        self,
        name: str,
        rendered: RenderedComponent,
        ctx: AssemblyContext,
    ) -> None:
        expected = count_messages(ctx.counter, rendered.messages) + sum(
            count_schema(ctx.counter, schema) for schema in rendered.tools
        )
        if rendered.tokens != expected:
            raise ContextError.from_code(
                ContextIssueCode.COMPONENT_CONTRACT_VIOLATION,
                f"{name} 组件报告的 token 数与重算不一致："
                f"{rendered.tokens} != {expected}",
                component=name,
            )

        if name != "tools" and rendered.tools:
            raise ContextError.from_code(
                ContextIssueCode.COMPONENT_CONTRACT_VIOLATION,
                f"{name} 组件不得输出 tools",
                component=name,
            )

        if name == "tools" and rendered.messages:
            raise ContextError.from_code(
                ContextIssueCode.COMPONENT_CONTRACT_VIOLATION,
                "tools 组件不得输出 messages",
                component="tools",
            )

        # history 的 assistant(tool_calls) 允许 content 为空，其余组件不得产出空消息。
        if name in ("system", "state", "summary", "user"):
            for message in rendered.messages:
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ContextError.from_code(
                        ContextIssueCode.COMPONENT_CONTRACT_VIOLATION,
                        f"{name} 组件输出了空 content 的消息",
                        component=name,
                    )
