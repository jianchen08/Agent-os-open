"""
LLM 模块异常定义
"""
from src.core.exceptions import SystemException


class LLMException(SystemException):
    pass


class RateLimitError(LLMException):
    def __init__(self, message: str = "API 速率限制", retry_after: float = None, details: dict = None):
        error_details = (details or {}).copy()
        if retry_after is not None:
            error_details["retry_after"] = retry_after
        super().__init__(message, code="RATE_LIMIT", details=error_details)
        self.retry_after = retry_after


class AuthenticationError(LLMException):
    def __init__(self, message: str = "API 认证失败", details: dict = None):
        super().__init__(message, code="LLM_AUTH_ERROR", details=details)


class InvalidRequestError(LLMException):
    def __init__(self, message: str = "无效的请求", details: dict = None):
        super().__init__(message, code="LLM_INVALID_REQUEST", details=details)


class ModelNotAvailableError(LLMException):
    def __init__(self, model: str, details: dict = None):
        error_details = (details or {}).copy()
        error_details["model"] = model
        super().__init__(f"模型不可用: {model}", code="MODEL_NOT_AVAILABLE", details=error_details)
        self.model = model


class TimeoutError(LLMException):
    def __init__(self, message: str = "请求超时", details: dict = None):
        super().__init__(message, code="LLM_TIMEOUT", details=details)


class ContentFilterError(LLMException):
    def __init__(self, message: str = "内容被过滤", details: dict = None):
        super().__init__(message, code="CONTENT_FILTERED", details=details)


class BudgetExhaustedError(LLMException):
    def __init__(self, message: str = "Token 预算已耗尽", remaining_tokens: int = 0, usage_percent: float = 100.0, details: dict = None):
        error_details = (details or {}).copy()
        error_details["remaining_tokens"] = remaining_tokens
        error_details["usage_percent"] = usage_percent
        super().__init__(message, code="BUDGET_EXHAUSTED", details=error_details)
        self.remaining_tokens = remaining_tokens
        self.usage_percent = usage_percent
