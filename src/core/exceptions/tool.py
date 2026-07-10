"""
Tool 模块异常定义
"""

from typing import Any

from core.exceptions.base import DomainException


class ToolException(DomainException):
    """工具异常基类

    工具模块相关异常的基类。
    """

    pass


class ToolNotFoundError(ToolException):
    """工具不存在异常

    Attributes:
        name: 工具名称
    """

    def __init__(self, name: str, details: dict[str, Any] | None = None):
        """初始化工具不存在异常

        Args:
            name: 工具名称
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["tool_name"] = name
        super().__init__(f"工具不存在: {name}", code="TOOL_NOT_FOUND", details=error_details)
        self.name = name


class ToolAlreadyExistsError(ToolException):
    """工具已存在异常

    Attributes:
        name: 工具名称
    """

    def __init__(self, name: str, details: dict[str, Any] | None = None):
        """初始化工具已存在异常

        Args:
            name: 工具名称
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["tool_name"] = name
        super().__init__(f"工具已存在: {name}", code="TOOL_EXISTS", details=error_details)
        self.name = name


class ToolValidationError(ToolException):
    """工具验证失败异常

    Attributes:
        errors: 验证错误列表
    """

    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ):
        """初始化工具验证失败异常

        Args:
            message: 错误消息
            errors: 验证错误列表（可选）
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        if errors:
            error_details["errors"] = errors
        super().__init__(message, code="TOOL_VALIDATION_ERROR", details=error_details)
        self.errors = errors or []


class ToolExecutionError(ToolException):
    """工具执行失败异常

    Attributes:
        tool_name: 工具名称
        cause: 原始异常
    """

    def __init__(
        self,
        tool_name: str,
        message: str,
        cause: Exception | None = None,
        details: dict[str, Any] | None = None,
    ):
        """初始化工具执行失败异常

        Args:
            tool_name: 工具名称
            message: 错误消息
            cause: 原始异常（可选）
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["tool_name"] = tool_name
        super().__init__(
            f"工具 '{tool_name}' 执行失败: {message}",
            code="TOOL_EXECUTION_ERROR",
            details=error_details,
        )
        self.tool_name = tool_name
        self.cause = cause


class ApprovalRequiredError(ToolException):
    """需要审批异常

    Attributes:
        tool_name: 工具名称
        reason: 审批原因
    """

    def __init__(
        self,
        tool_name: str,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        """初始化需要审批异常

        Args:
            tool_name: 工具名称
            reason: 审批原因（可选）
            details: 额外的错误详情（可选）
        """
        self.reason = reason or "此工具需要用户审批后才能执行"
        error_details = details.copy() if details else {}
        error_details["tool_name"] = tool_name
        error_details["reason"] = self.reason
        super().__init__(
            f"工具 '{tool_name}' 需要审批: {self.reason}",
            code="APPROVAL_REQUIRED",
            details=error_details,
        )
        self.tool_name = tool_name


class MCPException(ToolException):
    """MCP 异常基类"""

    pass


class MCPConnectionError(MCPException):
    """MCP 连接错误异常

    Attributes:
        server_name: MCP 服务器名称（从 details 中提取，可能为空）
        cause: 原始异常（可选）
    """

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ):
        """初始化 MCP 连接错误异常

        Args:
            message: 错误消息（应包含服务器名等上下文）
            details: 额外的错误详情（可选，通常包含 server/tool 等 key）
            cause: 原始异常（可选）
        """
        error_details = details.copy() if details else {}
        # 兼容历史 key：调用点用 "server"，旧定义用 "server_name"
        server_name = error_details.get("server") or error_details.get("server_name")
        super().__init__(
            message,
            code="MCP_CONNECTION_ERROR",
            details=error_details,
            cause=cause,
        )
        self.server_name = server_name


class MCPConfigError(MCPException):
    """MCP 配置错误异常"""

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        """初始化 MCP 配置错误异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(f"MCP 配置错误: {message}", code="MCP_CONFIG_ERROR", details=details)
