"""
核心异常定义

暴露接口：
- to_dict(self) -> dict[str, Any]：to_dict功能
- BaseAppException：BaseAppException类
- DomainException：DomainException类
- ValidationException：ValidationException类
- NotFoundException：NotFoundException类
- ConflictException：ConflictException类
- PermissionException：PermissionException类
- BusinessRuleException：BusinessRuleException类
- SystemException：SystemException类
- DatabaseException：DatabaseException类
- CacheException：CacheException类
- ExternalServiceException：ExternalServiceException类
- ConfigurationException：ConfigurationException类
- TimeoutException：TimeoutException类
- EmbeddingError：EmbeddingError类
"""

import logging
from typing import Any

from core.errors import ErrorCode, get_error_message

logger = logging.getLogger(__name__)


class BaseAppException(Exception):
    """基础异常类，集成 errors.py 错误码系统。

    默认从 ErrorCode 获取错误消息，优先使用传入的消息。
    """

    DEFAULT_CODE: str = "SYS_ERR_8003"

    def __init__(
        self,
        message: str | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ):
        self.code = code or self.DEFAULT_CODE
        self.message = message or get_error_message(self.code)
        self.details = details or {}
        self.cause = cause
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "type": self.__class__.__name__,
        }


class DomainException(BaseAppException):
    DEFAULT_CODE = "SYS_ERR_8003"


class ValidationException(DomainException):
    DEFAULT_CODE = ErrorCode.VAL_REQ_7001.value

    def __init__(
        self,
        message: str | None = None,
        field: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        error_details = details.copy() if details else {}
        if field:
            error_details["field"] = field
        super().__init__(message=message, code=code, details=error_details)
        self.field = field


class NotFoundException(DomainException):
    DEFAULT_CODE = ErrorCode.API_NOTF_2004

    def __init__(
        self,
        message: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        error_details = details.copy() if details else {}
        if resource_type:
            error_details["resource_type"] = resource_type
        if resource_id:
            error_details["resource_id"] = resource_id
        super().__init__(message=message, code=code, details=error_details)
        self.resource_type = resource_type
        self.resource_id = resource_id


class ConflictException(DomainException):
    DEFAULT_CODE = ErrorCode.TOOL_EXEC_3006

    def __init__(
        self,
        message: str | None = None,
        conflict_type: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        error_details = details.copy() if details else {}
        if conflict_type:
            error_details["conflict_type"] = conflict_type
        super().__init__(message=message, code=code, details=error_details)
        self.conflict_type = conflict_type


class PermissionException(DomainException):
    DEFAULT_CODE = ErrorCode.API_PERM_2002

    def __init__(
        self,
        message: str | None = None,
        required_permission: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        error_details = details.copy() if details else {}
        if required_permission:
            error_details["required_permission"] = required_permission
        super().__init__(message=message, code=code, details=error_details)
        self.required_permission = required_permission


class BusinessRuleException(DomainException):
    DEFAULT_CODE = ErrorCode.VAL_REQ_7001

    def __init__(
        self,
        message: str | None = None,
        rule: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        error_details = details.copy() if details else {}
        if rule:
            error_details["rule"] = rule
        super().__init__(message=message, code=code, details=error_details)
        self.rule = rule


class SystemException(BaseAppException):
    DEFAULT_CODE = "SYS_ERR_8003"


class DatabaseException(SystemException):
    DEFAULT_CODE = ErrorCode.DB_EXEC_4002.value

    def __init__(
        self,
        message: str | None = None,
        operation: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
        code: str | None = None,
    ):
        error_details = details.copy() if details else {}
        if operation:
            error_details["operation"] = operation
        super().__init__(message=message, code=code, details=error_details, cause=cause)
        self.operation = operation


class CacheException(SystemException):
    DEFAULT_CODE = ErrorCode.MEM_EXEC_5002.value

    def __init__(
        self,
        message: str | None = None,
        operation: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
        code: str | None = None,
    ):
        error_details = details.copy() if details else {}
        if operation:
            error_details["operation"] = operation
        super().__init__(message=message, code=code, details=error_details, cause=cause)
        self.operation = operation


class ExternalServiceException(SystemException):
    DEFAULT_CODE = ErrorCode.LLM_EXEC_9002

    def __init__(
        self,
        message: str | None = None,
        service_name: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
        code: str | None = None,
    ):
        error_details = details.copy() if details else {}
        if service_name:
            error_details["service_name"] = service_name
        super().__init__(message=message, code=code, details=error_details, cause=cause)
        self.service_name = service_name


class ConfigurationException(SystemException):
    DEFAULT_CODE = ErrorCode.API_VAL_2003.value

    def __init__(
        self,
        message: str | None = None,
        config_key: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        error_details = details.copy() if details else {}
        if config_key:
            error_details["config_key"] = config_key
        super().__init__(message=message, code=code, details=error_details)
        self.config_key = config_key


class TimeoutException(SystemException):
    DEFAULT_CODE = ErrorCode.SYS_TIME_8001

    def __init__(
        self,
        message: str | None = None,
        timeout_seconds: float | None = None,
        operation: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        error_details = details.copy() if details else {}
        if timeout_seconds is not None:
            error_details["timeout_seconds"] = timeout_seconds
        if operation:
            error_details["operation"] = operation
        super().__init__(message=message, code=code, details=error_details)
        self.timeout_seconds = timeout_seconds
        self.operation = operation


class EmbeddingError(SystemException):
    DEFAULT_CODE = ErrorCode.LLM_EXEC_9002

    def __init__(
        self,
        message: str | None = None,
        model_name: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
        code: str | None = None,
    ):
        error_details = details.copy() if details else {}
        if model_name:
            error_details["model_name"] = model_name
        super().__init__(message=message, code=code, details=error_details, cause=cause)
        self.model_name = model_name


class MCPConfigError(ConfigurationException):
    DEFAULT_CODE = ErrorCode.API_VAL_2003.value

    def __init__(self, message: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message=message, config_key="mcp", details=details)


class MCPConnectionError(ExternalServiceException):
    DEFAULT_CODE = ErrorCode.LLM_CONN_9001

    def __init__(
        self,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ):
        super().__init__(message=message, service_name="mcp", details=details, cause=cause)


class ReasoningRequiredError(BusinessRuleException):
    DEFAULT_CODE = ErrorCode.VAL_REQ_7001

    def __init__(self, message: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message=message, rule="reasoning_required", details=details)


class ToolAlreadyExistsError(ConflictException):
    DEFAULT_CODE = ErrorCode.TOOL_EXEC_3006

    def __init__(self, tool_name: str, details: dict[str, Any] | None = None):
        super().__init__(
            message=f"Tool already exists: {tool_name}",
            conflict_type="tool_duplicate",
            details=details,
        )
        self.tool_name = tool_name


class ToolNotFoundError(NotFoundException):
    DEFAULT_CODE = ErrorCode.TOOL_NOTF_3001.value

    def __init__(self, tool_name: str, details: dict[str, Any] | None = None):
        super().__init__(
            message=f"Tool not found: {tool_name}",
            resource_type="tool",
            resource_id=tool_name,
            details=details,
        )
        self.tool_name = tool_name
