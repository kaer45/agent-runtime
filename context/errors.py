"""ContextEngine 的阻塞异常。

约定：``ContextError`` 只携带 ``level == ERROR`` 的 issue；
``WARNING`` / ``INFO`` 走报告或压缩结果，不进异常。
"""

from __future__ import annotations

from core.issues import ContextIssue, ContextIssueCode, IssueLevel


class ContextError(Exception):
    """装配或预算失败，带一条 ERROR 级 issue。"""

    issue: ContextIssue

    def __init__(self, issue: ContextIssue):
        if not isinstance(issue, ContextIssue):
            raise TypeError("ContextError.issue 必须是 ContextIssue")

        if issue.level is not IssueLevel.ERROR:
            raise ValueError(
                "ContextError 只接受 ERROR 级 issue，"
                f"收到 {issue.level.value}"
            )

        self.issue = issue
        super().__init__(issue.message)

    @classmethod
    def from_code(
        cls,
        code: ContextIssueCode,
        message: str,
        *,
        component: str | None = None,
        details: dict | None = None,
    ) -> "ContextError":
        return cls(
            ContextIssue.from_code(
                code,
                message,
                level=IssueLevel.ERROR,
                component=component,
                details=details,
            )
        )

    @property
    def code(self) -> ContextIssueCode:
        return self.issue.code

    @property
    def message(self) -> str:
        return self.issue.message
