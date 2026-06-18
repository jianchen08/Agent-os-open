"""
工具模块异常定义
"""
from src.core.exceptions import DomainException


class ToolException(DomainException):
    pass


class ToolNotFoundError(ToolException):
    def __init__(self, name: str, details: dict = None):
        error_details = (details or {}).copy()
        error_details["tool_name"] = name
        super().__init__(f"工具不存在: {name}", code="TOOL_NOT_FOUND", details=error_details)
        self.name = name


class ToolAlreadyExistsError(ToolException):
    def __init__(self, name: str, details: dict = None):
        error_details = (details or {}).copy()
        error_details["tool_name"] = name
        super().__init__(f"工具已存在: {name}", code="TOOL_EXISTS", details=error_details)
        self.name = name


class ToolValidationError(ToolException):
    def __init__(self, message: str, errors: list = None, details: dict = None):
        error_details = (details or {}).copy()
        if errors:
            error_details["errors"] = errors
        super().__init__(message, code="TOOL_VALIDATION_ERROR", details=error_details)
        self.errors = errors or []


class ToolExecutionError(ToolException):
    def __init__(self, tool_name: str, message: str, cause: Exception = None, details: dict = None):
        error_details = (details or {}).copy()
        error_details["tool_name"] = tool_name
        super().__init__(f"工具 '{tool_name}' 执行失败: {message}", code="TOOL_EXECUTION_ERROR", details=error_details, cause=cause)
        self.tool_name = tool_name
        self.cause = cause


class ApprovalRequiredError(ToolException):
    def __init__(self, tool_name: str, reason: str = None, details: dict = None):
        reason = reason or "此工具需要用户审批后才能执行"
        error_details = (details or {}).copy()
        error_details["tool_name"] = tool_name
        error_details["reason"] = reason
        super().__init__(f"工具 '{tool_name}' 需要审批: {reason}", code="APPROVAL_REQUIRED", details=error_details)
        self.tool_name = tool_name
        self.reason = reason


class MCPException(ToolException):
    pass


class MCPConnectionError(MCPException):
    def __init__(self, message: str, details: dict = None, cause: Exception = None):
        error_details = (details or {}).copy()
        super().__init__(message, code="MCP_CONNECTION_ERROR", details=error_details, cause=cause)
        self.server_name = error_details.get("server") or error_details.get("server_name")


class MCPConfigError(MCPException):
    def __init__(self, message: str, details: dict = None):
        super().__init__(f"MCP 配置错误: {message}", code="MCP_CONFIG_ERROR", details=details)
