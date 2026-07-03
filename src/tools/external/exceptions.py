"""外部工具连接机制异常体系。

暴露接口：
- ExternalToolException：外部工具基础异常
- ConnectionError：连接异常
- ExecutionError：执行异常
- ExternalTimeoutError：超时异常
- ConfigError：配置异常
- SecretError：密钥异常
- SandboxError：沙箱异常
"""

from __future__ import annotations

from typing import Any

from core.exceptions import DomainException


class ExternalToolException(DomainException):
    """外部工具基础异常。"""

    DEFAULT_CODE = "EXT_TOOL_ERR"

    def __init__(
        self,
        message: str | None = None,
        tool_name: str | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        error_details = (details or {}).copy()
        if tool_name:
            error_details["tool_name"] = tool_name
        super().__init__(
            message=message,
            code=code or self.DEFAULT_CODE,
            details=error_details,
            cause=cause,
        )
        self.tool_name = tool_name


class ConnectionError(ExternalToolException):
    """连接异常（与 Python 内置 ConnectionError 同名，通过模块隔离）。"""

    DEFAULT_CODE = "EXT_CONN_ERR"

    def __init__(
        self,
        message: str | None = None,
        tool_name: str | None = None,
        endpoint: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        error_details = (details or {}).copy()
        if endpoint:
            error_details["endpoint"] = endpoint
        super().__init__(
            message=message or "外部工具连接失败",
            tool_name=tool_name,
            code=self.DEFAULT_CODE,
            details=error_details,
            cause=cause,
        )
        self.endpoint = endpoint


class ExecutionError(ExternalToolException):
    """执行异常。"""

    DEFAULT_CODE = "EXT_EXEC_ERR"

    def __init__(
        self,
        message: str | None = None,
        tool_name: str | None = None,
        operation: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        error_details = (details or {}).copy()
        if operation:
            error_details["operation"] = operation
        super().__init__(
            message=message or "外部工具执行失败",
            tool_name=tool_name,
            code=self.DEFAULT_CODE,
            details=error_details,
            cause=cause,
        )
        self.operation = operation


class ExternalTimeoutError(ExternalToolException):
    """超时异常。"""

    DEFAULT_CODE = "EXT_TIMEOUT_ERR"

    def __init__(
        self,
        message: str | None = None,
        tool_name: str | None = None,
        timeout_seconds: float | None = None,
        operation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        error_details = (details or {}).copy()
        if timeout_seconds is not None:
            error_details["timeout_seconds"] = timeout_seconds
        if operation:
            error_details["operation"] = operation
        super().__init__(
            message=message or "外部工具操作超时",
            tool_name=tool_name,
            code=self.DEFAULT_CODE,
            details=error_details,
        )
        self.timeout_seconds = timeout_seconds
        self.operation = operation


class ConfigError(ExternalToolException):
    """配置异常。"""

    DEFAULT_CODE = "EXT_CONFIG_ERR"

    def __init__(
        self,
        message: str | None = None,
        tool_name: str | None = None,
        config_key: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        error_details = (details or {}).copy()
        if config_key:
            error_details["config_key"] = config_key
        super().__init__(
            message=message or "外部工具配置错误",
            tool_name=tool_name,
            code=self.DEFAULT_CODE,
            details=error_details,
            cause=cause,
        )
        self.config_key = config_key


class SecretError(ExternalToolException):
    """密钥异常。"""

    DEFAULT_CODE = "EXT_SECRET_ERR"

    def __init__(
        self,
        message: str | None = None,
        tool_name: str | None = None,
        secret_key: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        error_details = (details or {}).copy()
        if secret_key:
            error_details["secret_key"] = "***"  # 脱敏
        super().__init__(
            message=message or "密钥操作失败",
            tool_name=tool_name,
            code=self.DEFAULT_CODE,
            details=error_details,
            cause=cause,
        )
        self.secret_key = secret_key


class SandboxError(ExternalToolException):
    """沙箱异常。"""

    DEFAULT_CODE = "EXT_SANDBOX_ERR"

    def __init__(
        self,
        message: str | None = None,
        tool_name: str | None = None,
        sandbox_id: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        error_details = (details or {}).copy()
        if sandbox_id:
            error_details["sandbox_id"] = sandbox_id
        super().__init__(
            message=message or "沙箱操作失败",
            tool_name=tool_name,
            code=self.DEFAULT_CODE,
            details=error_details,
            cause=cause,
        )
        self.sandbox_id = sandbox_id
