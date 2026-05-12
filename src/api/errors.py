"""
统一错误响应模块
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from src.core.errors import (
    ErrorCode,
    ERROR_MESSAGES,
    get_error_message,
    get_http_status,
)


class ErrorResponse(BaseModel):
    code: str = Field(..., description="错误码")
    message: str = Field(..., description="用户友好的错误消息")
    detail: str | None = Field(None, description="详细错误信息")
    trace_id: str = Field(..., description="链路追踪ID")
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    details: dict[str, Any] | None = Field(None, description="额外详情")

    model_config = {"json_encoders": {datetime: lambda v: v.isoformat()}}


def create_error_response(
    code: str,
    message: str | None = None,
    trace_id: str = "unknown",
    detail: str | None = None,
    details: dict[str, Any] | None = None,
    errors: dict[str, Any] | None = None,
) -> ErrorResponse:
    if message is None:
        message = get_error_message(code)
    return ErrorResponse(
        code=code,
        message=message,
        trace_id=trace_id,
        detail=detail,
        details=details or errors,  # 使用 details 或 errors
    )


def is_valid_error_code(code: str) -> bool:
    return code in ERROR_MESSAGES


class BaseError(Exception):
    def __init__(self, message: str, code: str, details: dict[str, Any] | None = None):
        self.message = message
        self.code = code
        self.details = details or {}
        super().__init__(message)


class DatabaseConnectionError(BaseError):
    def __init__(self, message: str = "数据库连接失败", details: dict[str, Any] | None = None):
        super().__init__(message, "DB_CONN_4001", details)


class CacheConnectionError(BaseError):
    def __init__(self, message: str = "缓存连接失败", details: dict[str, Any] | None = None):
        super().__init__(message, "CACHE_CONN_4002", details)


class ConfigurationError(BaseError):
    def __init__(self, message: str = "配置错误", details: dict[str, Any] | None = None):
        super().__init__(message, "CONFIG_4003", details)


class ValidationError(BaseError):
    def __init__(self, message: str = "验证失败", details: dict[str, Any] | None = None):
        super().__init__(message, "VAL_001", details)
