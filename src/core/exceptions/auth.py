"""
Auth 模块异常定义
"""

from typing import Any

from core.exceptions.base import DomainException


class AuthException(DomainException):
    """认证异常基类

    认证模块相关异常的基类。
    """

    pass


class TokenError(AuthException):
    """Token 相关异常基类"""

    def __init__(
        self,
        message: str = "Token 错误",
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        """初始化 Token 错误异常

        Args:
            message: 错误消息
            code: 错误码（可选）
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code=code or "TOKEN_ERROR", details=details)


class TokenExpiredError(TokenError):
    """Token 已过期异常"""

    def __init__(
        self,
        message: str = "Token 已过期",
        details: dict[str, Any] | None = None,
    ):
        """初始化 Token 已过期异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="TOKEN_EXPIRED", details=details)


class TokenInvalidError(TokenError):
    """Token 无效异常"""

    def __init__(
        self,
        message: str = "Token 无效",
        details: dict[str, Any] | None = None,
    ):
        """初始化 Token 无效异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="TOKEN_INVALID", details=details)


class TokenRevokedError(TokenError):
    """Token 已被撤销异常"""

    def __init__(
        self,
        message: str = "Token 已被撤销",
        details: dict[str, Any] | None = None,
    ):
        """初始化 Token 已被撤销异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="TOKEN_REVOKED", details=details)


class AuthenticationFailedError(AuthException):
    """认证失败异常"""

    def __init__(
        self,
        message: str = "认证失败",
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        """初始化认证失败异常

        Args:
            message: 错误消息
            code: 错误码（可选）
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code=code or "AUTH_FAILED", details=details)


class InvalidCredentialsError(AuthenticationFailedError):
    """凭证无效异常"""

    def __init__(
        self,
        message: str = "用户名或密码错误",
        details: dict[str, Any] | None = None,
    ):
        """初始化凭证无效异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="INVALID_CREDENTIALS", details=details)


class UserNotFoundError(AuthenticationFailedError):
    """用户不存在异常"""

    def __init__(
        self,
        message: str = "用户不存在",
        details: dict[str, Any] | None = None,
    ):
        """初始化用户不存在异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="USER_NOT_FOUND", details=details)


class UserInactiveError(AuthenticationFailedError):
    """用户已禁用异常"""

    def __init__(
        self,
        message: str = "用户已被禁用",
        details: dict[str, Any] | None = None,
    ):
        """初始化用户已禁用异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="USER_INACTIVE", details=details)


class UserExistsError(AuthException):
    """用户已存在异常"""

    def __init__(
        self,
        message: str = "用户名已存在",
        details: dict[str, Any] | None = None,
    ):
        """初始化用户已存在异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="USER_EXISTS", details=details)


class PermissionDeniedError(AuthException):
    """权限不足异常"""

    def __init__(
        self,
        message: str = "权限不足",
        required_permission: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        """初始化权限不足异常

        Args:
            message: 错误消息
            required_permission: 需要的权限（可选）
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        if required_permission:
            error_details["required_permission"] = required_permission
        super().__init__(message, code="PERMISSION_DENIED", details=error_details)
        self.required_permission = required_permission


class RateLimitExceededError(AuthException):
    """请求频率超限异常"""

    def __init__(
        self,
        message: str = "请求过于频繁，请稍后再试",
        details: dict[str, Any] | None = None,
    ):
        """初始化请求频率超限异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="RATE_LIMIT_EXCEEDED", details=details)
