"""核心基础模块。

提供异常定义、状态管理、结果类型、注册表基类等基础设施。
"""

from core.exceptions import (
    DomainException,
    MCPConfigError,
    MCPConnectionError,
    ReasoningRequiredError,
    ToolAlreadyExistsError,
    ToolNotFoundError,
)
from core.errors import ErrorCode, get_error_message

__all__ = [
    "DomainException",
    "MCPConfigError",
    "MCPConnectionError",
    "ReasoningRequiredError",
    "ToolAlreadyExistsError",
    "ToolNotFoundError",
    "ErrorCode",
    "get_error_message",
]
