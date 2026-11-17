"""ContextEngine 的统一 Issue 模型与静态文案目录。

错误、警告和状态提示共用一套 code：

- ``ContextIssueCode``：稳定机器码，控制流与测试用；
- ``IssueLevel``：分层，决定阻塞与否；
- ``IssueSpec``：静态标题和说明，放在 ``ISSUE_CATALOG``，不在每条实例上重复；
- ``ContextIssue``：一次具体发生的 issue。

本模块只放数据，不做判断，也不依赖 context / history，避免反向依赖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class IssueLevel(str, Enum):
    """issue 级别。ERROR 阻塞，WARNING / INFO 不阻塞。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ContextIssueCode(str, Enum):
    """ContextEngine 与 History 层共用的稳定 issue 码。"""

    SYSTEM_OVER_BUDGET = "SYSTEM_OVER_BUDGET"
    USER_INPUT_TOO_LONG = "USER_INPUT_TOO_LONG"
    TOOLS_REQUIRED_MISSING = "TOOLS_REQUIRED_MISSING"
    HISTORY_FLOOR_UNMET = "HISTORY_FLOOR_UNMET"
    HISTORY_OVER_TARGET = "HISTORY_OVER_TARGET"
    HISTORY_HARD_CAP_EXCEEDED = "HISTORY_HARD_CAP_EXCEEDED"
    HISTORY_ARCHIVE_FAILED = "HISTORY_ARCHIVE_FAILED"
    SUMMARY_GENERATION_FAILED = "SUMMARY_GENERATION_FAILED"
    SUMMARY_INPUT_TOO_LONG = "SUMMARY_INPUT_TOO_LONG"
    SUMMARY_CAS_CONFLICT = "SUMMARY_CAS_CONFLICT"
    BUDGET_INFEASIBLE = "BUDGET_INFEASIBLE"
    COMPONENT_CONTRACT_VIOLATION = "COMPONENT_CONTRACT_VIOLATION"
    FINAL_OVER_BUDGET = "FINAL_OVER_BUDGET"
    REPORT_MISMATCH = "REPORT_MISMATCH"


@dataclass(frozen=True)
class IssueSpec:
    """一个 issue code 的静态定义。"""

    code: ContextIssueCode
    default_level: IssueLevel
    title: str
    description: str


ISSUE_CATALOG: Mapping[ContextIssueCode, IssueSpec] = {
    ContextIssueCode.SYSTEM_OVER_BUDGET: IssueSpec(
        code=ContextIssueCode.SYSTEM_OVER_BUDGET,
        default_level=IssueLevel.ERROR,
        title="system 超过预算",
        description="system prompt 的 token 数超过 system_ceiling，启动已拒绝。",
    ),
    ContextIssueCode.USER_INPUT_TOO_LONG: IssueSpec(
        code=ContextIssueCode.USER_INPUT_TOO_LONG,
        default_level=IssueLevel.ERROR,
        title="用户输入过长",
        description="最新 user 输入超过 user_ceiling，需要缩短或拆分输入。",
    ),
    ContextIssueCode.TOOLS_REQUIRED_MISSING: IssueSpec(
        code=ContextIssueCode.TOOLS_REQUIRED_MISSING,
        default_level=IssueLevel.ERROR,
        title="必需工具不可用",
        description=(
            "required 工具不在 registry 中、被白名单过滤，"
            "或放不进 tools_ceiling / max_tools。"
        ),
    ),
    ContextIssueCode.HISTORY_FLOOR_UNMET: IssueSpec(
        code=ContextIssueCode.HISTORY_FLOOR_UNMET,
        default_level=IssueLevel.ERROR,
        title="最近 6 轮超限",
        description="最近 6 个完整 turn 已超过 history_ceiling，压缩无法释放空间。",
    ),
    ContextIssueCode.HISTORY_OVER_TARGET: IssueSpec(
        code=ContextIssueCode.HISTORY_OVER_TARGET,
        default_level=IssueLevel.WARNING,
        title="历史超过目标水位",
        description=(
            "未覆盖历史超过 60% 目标水位，但受最近 6 轮 floor 限制，"
            "当前不能压缩。"
        ),
    ),
    ContextIssueCode.HISTORY_HARD_CAP_EXCEEDED: IssueSpec(
        code=ContextIssueCode.HISTORY_HARD_CAP_EXCEEDED,
        default_level=IssueLevel.ERROR,
        title="历史硬上限超限",
        description="硬归档与强制压缩后，主表 turn 数仍不少于 hard_limit。",
    ),
    ContextIssueCode.HISTORY_ARCHIVE_FAILED: IssueSpec(
        code=ContextIssueCode.HISTORY_ARCHIVE_FAILED,
        default_level=IssueLevel.WARNING,
        title="历史归档失败",
        description="归档已覆盖 turn 失败，本轮回答继续，下一轮重试。",
    ),
    ContextIssueCode.SUMMARY_GENERATION_FAILED: IssueSpec(
        code=ContextIssueCode.SUMMARY_GENERATION_FAILED,
        default_level=IssueLevel.WARNING,
        title="摘要生成失败",
        description=(
            "摘要生成、保存或写入失败，保留旧摘要和旧 covered_through_seq，"
            "下一轮重试。"
        ),
    ),
    ContextIssueCode.SUMMARY_INPUT_TOO_LONG: IssueSpec(
        code=ContextIssueCode.SUMMARY_INPUT_TOO_LONG,
        default_level=IssueLevel.WARNING,
        title="摘要输入放不下",
        description="旧摘要加待压缩 turn 超过输入上限，连一个 turn 都放不进。",
    ),
    ContextIssueCode.SUMMARY_CAS_CONFLICT: IssueSpec(
        code=ContextIssueCode.SUMMARY_CAS_CONFLICT,
        default_level=IssueLevel.INFO,
        title="摘要覆盖冲突",
        description=(
            "压缩期间 covered_through_seq 已被其他进程推进，"
            "本次摘要被丢弃，不重试。"
        ),
    ),
    ContextIssueCode.BUDGET_INFEASIBLE: IssueSpec(
        code=ContextIssueCode.BUDGET_INFEASIBLE,
        default_level=IssueLevel.ERROR,
        title="预算不可行",
        description="组件 ceiling 之和加 safety 超过可用输入预算，启动已拒绝。",
    ),
    ContextIssueCode.COMPONENT_CONTRACT_VIOLATION: IssueSpec(
        code=ContextIssueCode.COMPONENT_CONTRACT_VIOLATION,
        default_level=IssueLevel.ERROR,
        title="组件违反契约",
        description=(
            "组件返回的 token 数、输出槽位或消息结构不符合 "
            "RenderedComponent 合同。"
        ),
    ),
    ContextIssueCode.FINAL_OVER_BUDGET: IssueSpec(
        code=ContextIssueCode.FINAL_OVER_BUDGET,
        default_level=IssueLevel.ERROR,
        title="最终上下文超限",
        description="最终 messages 与 tools 的 token 总数超过 available_tokens。",
    ),
    ContextIssueCode.REPORT_MISMATCH: IssueSpec(
        code=ContextIssueCode.REPORT_MISMATCH,
        default_level=IssueLevel.ERROR,
        title="报告账不闭合",
        description=(
            "组件 token 之和与 message_tokens + tool_tokens 不一致，"
            "或报告字段与重算结果不符。"
        ),
    ),
}

_missing_specs = [code for code in ContextIssueCode if code not in ISSUE_CATALOG]
if _missing_specs:  # pragma: no cover - 只会在漏写 catalog 时触发
    raise RuntimeError(f"ISSUE_CATALOG 缺少 issue 定义：{_missing_specs}")


@dataclass(frozen=True)
class ContextIssue:
    """一次具体发生的 issue。"""

    code: ContextIssueCode
    level: IssueLevel
    message: str
    component: str | None = None
    details: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", ContextIssueCode(self.code))
        object.__setattr__(self, "level", IssueLevel(self.level))

        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("ContextIssue.message 不能为空")

        if self.component is not None and not str(self.component).strip():
            raise ValueError("ContextIssue.component 不能是空字符串")

        if not isinstance(self.details, dict):
            raise TypeError("ContextIssue.details 必须是 dict")

        # 冻结文档要求 details 只放 JSON 可序列化数据。
        try:
            json.dumps(self.details)
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"ContextIssue.details 必须 JSON 可序列化：{exc}"
            ) from exc

    @classmethod
    def from_code(
        cls,
        code: ContextIssueCode,
        message: str,
        *,
        level: IssueLevel | None = None,
        component: str | None = None,
        details: dict | None = None,
    ) -> "ContextIssue":
        """按 catalog 的默认级别创建 issue；``level`` 可显式覆盖。"""
        try:
            spec = ISSUE_CATALOG[ContextIssueCode(code)]
        except (KeyError, ValueError) as exc:
            raise ValueError(f"未知的 ContextIssueCode：{code!r}") from exc

        if details is not None and not isinstance(details, dict):
            raise TypeError("ContextIssue.details 必须是 dict")

        return cls(
            code=spec.code,
            level=level if level is not None else spec.default_level,
            message=message,
            component=component,
            details={} if details is None else dict(details),
        )

    @property
    def spec(self) -> IssueSpec:
        return ISSUE_CATALOG[self.code]

    @property
    def title(self) -> str:
        return self.spec.title

    @property
    def description(self) -> str:
        return self.spec.description
