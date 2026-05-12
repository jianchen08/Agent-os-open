"""
认证模块异常定义
"""
from src.core.exceptions import DomainException


class AuthException(DomainException):
    pass


class TokenError(AuthException):
    def __init__(self, message: str = "Token 错误", code: str = None, details: dict = None):
        super().__init__(message, code=code or "TOKEN_ERROR", details=details)


class TokenExpiredError(TokenError):
    def __init__(self, message: str = "Token 已过期", details: dict = None):
        super().__init__(message, code="TOKEN_EXPIRED", details=details)


class TokenInvalidError(TokenError):
    def __init__(self, message: str = "Token 无效", details: dict = None):
        super().__init__(message, code="TOKEN_INVALID", details=details)


class TokenRevokedError(TokenError):
    def __init__(self, message: str = "Token 已被撤销", details: dict = None):
        super().__init__(message, code="TOKEN_REVOKED", details=details)


class AuthenticationError(AuthException):
    def __init__(self, message: str = "认证失败", code: str = None, details: dict = None):
        super().__init__(message, code=code or "AUTH_FAILED", details=details)


class InvalidCredentialsError(AuthenticationError):
    def __init__(self, message: str = "用户名或密码错误", details: dict = None):
        super().__init__(message, code="INVALID_CREDENTIALS", details=details)


class UserNotFoundError(AuthenticationError):
    def __init__(self, message: str = "用户不存在", details: dict = None):
        super().__init__(message, code="USER_NOT_FOUND", details=details)


class UserInactiveError(AuthenticationError):
    def __init__(self, message: str = "用户已被禁用", details: dict = None):
        super().__init__(message, code="USER_INACTIVE", details=details)


class UserExistsError(AuthException):
    def __init__(self, message: str = "用户名已存在", details: dict = None):
        super().__init__(message, code="USER_EXISTS", details=details)


class PermissionDeniedError(AuthException):
    def __init__(self, message: str = "权限不足", required_permission: str = None, details: dict = None):
        error_details = (details or {}).copy()
        if required_permission:
            error_details["required_permission"] = required_permission
        super().__init__(message, code="PERMISSION_DENIED", details=error_details)
        self.required_permission = required_permission


class RateLimitExceededError(AuthException):
    def __init__(self, message: str = "请求过于频繁，请稍后再试", details: dict = None):
        super().__init__(message, code="RATE_LIMIT_EXCEEDED", details=details)
