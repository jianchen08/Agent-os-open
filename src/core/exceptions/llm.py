"""
LLM 模块异常定义
"""

from typing import Any

from core.exceptions.base import SystemException


class LLMException(SystemException):
    """LLM 异常基类

    LLM 模块相关异常的基类。
    """

    pass


class RateLimitError(LLMException):
    """速率限制错误异常

    当 API 请求触发速率限制时抛出。

    Attributes:
        retry_after: 重试等待时间（秒）
    """

    def __init__(
        self,
        message: str = "API 速率限制",
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ):
        """初始化速率限制错误异常

        Args:
            message: 错误消息
            retry_after: 重试等待时间（秒）（可选）
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        if retry_after is not None:
            error_details["retry_after"] = retry_after
        super().__init__(message, code="RATE_LIMIT", details=error_details)
        self.retry_after = retry_after


class AuthenticationError(LLMException):
    """认证错误异常

    当 API 认证失败时抛出。
    """

    def __init__(
        self,
        message: str = "API 认证失败",
        details: dict[str, Any] | None = None,
    ):
        """初始化认证错误异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="LLM_AUTH_ERROR", details=details)


class InvalidRequestError(LLMException):
    """无效请求错误异常

    当请求参数无效时抛出。
    """

    def __init__(
        self,
        message: str = "无效的请求",
        details: dict[str, Any] | None = None,
    ):
        """初始化无效请求错误异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="LLM_INVALID_REQUEST", details=details)


class ModelNotAvailableError(LLMException):
    """模型不可用错误异常

    当请求的模型不可用时抛出。

    Attributes:
        model: 模型名称
    """

    def __init__(
        self,
        model: str,
        details: dict[str, Any] | None = None,
    ):
        """初始化模型不可用错误异常

        Args:
            model: 模型名称
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["model"] = model
        super().__init__(f"模型不可用: {model}", code="MODEL_NOT_AVAILABLE", details=error_details)
        self.model = model


class LLMTimeoutError(LLMException):
    """LLM 超时错误异常

    当 LLM 请求超时时抛出。
    """

    def __init__(
        self,
        message: str = "请求超时",
        details: dict[str, Any] | None = None,
    ):
        """初始化 LLM 超时错误异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="LLM_TIMEOUT", details=details)


class ContentFilterError(LLMException):
    """内容过滤错误异常

    当请求内容被过滤时抛出。
    """

    def __init__(
        self,
        message: str = "内容被过滤",
        details: dict[str, Any] | None = None,
    ):
        """初始化内容过滤错误异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="CONTENT_FILTERED", details=details)


class BudgetExhaustedError(LLMException):
    """预算耗尽错误异常

    当 Token 预算耗尽时抛出。

    Attributes:
        remaining_tokens: 剩余 Token 数
        usage_percent: 使用百分比
    """

    def __init__(
        self,
        message: str = "Token 预算已耗尽",
        remaining_tokens: int = 0,
        usage_percent: float = 100.0,
        details: dict[str, Any] | None = None,
    ):
        """初始化预算耗尽错误异常

        Args:
            message: 错误消息
            remaining_tokens: 剩余 Token 数
            usage_percent: 使用百分比
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["remaining_tokens"] = remaining_tokens
        error_details["usage_percent"] = usage_percent
        super().__init__(message, code="BUDGET_EXHAUSTED", details=error_details)
        self.remaining_tokens = remaining_tokens
        self.usage_percent = usage_percent
