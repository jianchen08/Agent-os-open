"""
工具模块异常定义

暴露接口：
- ToolException：工具异常基类
- ToolNotFoundError：工具不存在异常
- ToolAlreadyExistsError：工具已存在异常
- ToolValidationError：工具验证失败异常
- ToolExecutionError：工具执行失败异常
- ApprovalRequiredError：需要审批异常
- MCPException：MCP 异常基类
- MCPConnectionError：MCP 连接错误异常
- MCPConfigError：MCP 配置错误异常
"""

from __future__ import annotations

from typing import Any

from src.core.exceptions.base import DomainException


class ToolException(DomainException):
    """工具异常基类。"""

    pass


class ToolNotFoundError(ToolException):
    """工具不存在异常。"""

    def __init__(self, name: str, details: dict[str, Any] | None = None) -> None:
        error_details = (details or {}).copy()
        error_details["tool_name"] = name
        super().__init__(f"工具不存在: {name}", code="TOOL_NOT_FOUND", details=error_details)
        self.name = name


class ToolAlreadyExistsError(ToolException):
    """工具已存在异常。"""

    def __init__(self, name: str, details: dict[str, Any] | None = None) -> None:
        error_details = (details or {}).copy()
        error_details["tool_name"] = name
        super().__init__(f"工具已存在: {name}", code="TOOL_EXISTS", details=error_details)
        self.name = name


class ToolValidationError(ToolException):
    """工具验证失败异常。"""

    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        error_details = (details or {}).copy()
        if errors:
            error_details["errors"] = errors
        super().__init__(message, code="TOOL_VALIDATION_ERROR", details=error_details)
        self.errors = errors or []


class ToolExecutionError(ToolException):
    """工具执行失败异常。"""

    def __init__(
        self,
        tool_name: str,
        message: str,
        cause: Exception | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        error_details = (details or {}).copy()
        error_details["tool_name"] = tool_name
        super().__init__(
            f"工具 '{tool_name}' 执行失败: {message}",
            code="TOOL_EXECUTION_ERROR",
            details=error_details,
            cause=cause,
        )
        self.tool_name = tool_name
        self.cause = cause


class ApprovalRequiredError(ToolException):
    """需要审批异常。"""

    def __init__(
        self,
        tool_name: str,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        reason = reason or "此工具需要用户审批后才能执行"
        error_details = (details or {}).copy()
        error_details["tool_name"] = tool_name
        error_details["reason"] = reason
        super().__init__(
            f"工具 '{tool_name}' 需要审批: {reason}",
            code="APPROVAL_REQUIRED",
            details=error_details,
        )
        self.tool_name = tool_name
        self.reason = reason


class MCPException(ToolException):
    """MCP 异常基类。"""

    pass


class MCPConnectionError(MCPException):
    """MCP 连接错误异常。"""

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        error_details = (details or {}).copy()
        super().__init__(
            message,
            code="MCP_CONNECTION_ERROR",
            details=error_details,
            cause=cause,
        )
        self.server_name = error_details.get("server") or error_details.get("server_name")


class MCPConfigError(MCPException):
    """MCP 配置错误异常。"""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            f"MCP 配置错误: {message}",
            code="MCP_CONFIG_ERROR",
            details=details,
        )
