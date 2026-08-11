"""
核心异常基类

提供统一的异常层次结构基础。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class BaseAppException(Exception):
    """应用异常基类

    所有自定义异常的基类，提供统一的错误信息结构。

    Attributes:
        message: 错误消息
        code: 错误码
        details: 额外的错误详情字典
        cause: 原始异常（可选）
    """

    def __init__(
        self,
        message: str,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ):
        """初始化基础异常

        Args:
            message: 错误消息
            code: 错误码（可选，默认使用类名）
            details: 额外的错误详情（可选）
            cause: 原始异常（可选）
        """
        self.message = message
        self.code = code or self._default_code()
        self.details = details or {}
        self.cause = cause
        super().__init__(message)

        # 记录异常日志
        self._log_exception()

    def _default_code(self) -> str:
        """生成默认错误码（基于类名）"""
        class_name = self.__class__.__name__
        # 移除 Exception 后缀并转为大写
        if class_name.endswith("Exception"):
            class_name = class_name[:-9]
        return class_name.upper()

    def _log_exception(self):
        """记录异常日志"""
        # 子类可以覆盖此方法以自定义日志记录

    def __str__(self) -> str:
        """返回异常的字符串表示"""
        return f"[{self.code}] {self.message}"

    def __repr__(self) -> str:
        """返回异常的详细表示"""
        return f"{self.__class__.__name__}(message={self.message!r}, code={self.code!r}, details={self.details!r})"

    def to_dict(self) -> dict[str, Any]:
        """将异常转换为字典格式

        Returns:
            包含错误信息的字典
        """
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "type": self.__class__.__name__,
        }


# ============================================================================
# 域异常（业务逻辑错误）
# ============================================================================


class DomainException(BaseAppException):
    """域异常基类

    用于表示业务逻辑中的错误，这些错误是预期的和可恢复的。
    """

    def _log_exception(self):
        """记录域异常为警告级别"""
        logger.warning(
            f"[{self.code}] {self.message}",
            extra={"details": self.details, "exception_type": self.__class__.__name__},
        )


class ValidationException(DomainException):
    """验证异常

    当输入数据验证失败时抛出。

    Attributes:
        message: 错误消息
        field: 验证失败的字段名称
        details: 额外的错误详情
    """

    def __init__(
        self,
        message: str,
        field: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        """初始化验证异常

        Args:
            message: 错误消息
            field: 验证失败的字段名称（可选）
            details: 额外的错误详情（可选）
            code: 错误码（可选）
        """
        error_details = details.copy() if details else {}
        if field:
            error_details["field"] = field
        super().__init__(message, code=code or "VAL_REQ_7001", details=error_details)
        self.field = field


class NotFoundException(DomainException):
    """未找到异常

    当请求的资源不存在时抛出。

    Attributes:
        message: 错误消息
        resource_type: 资源类型
        resource_id: 资源ID
        details: 额外的错误详情
    """

    def __init__(
        self,
        message: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        """初始化未找到异常

        Args:
            message: 错误消息
            resource_type: 资源类型（可选）
            resource_id: 资源ID（可选）
            details: 额外的错误详情（可选）
            code: 错误码（可选）
        """
        error_details = details.copy() if details else {}
        if resource_type:
            error_details["resource_type"] = resource_type
        if resource_id:
            error_details["resource_id"] = resource_id
        super().__init__(message, code=code or "NOT_FOUND", details=error_details)
        self.resource_type = resource_type
        self.resource_id = resource_id


class ConflictException(DomainException):
    """冲突异常

    当操作与当前状态冲突时抛出（如重复创建、状态转换错误等）。

    Attributes:
        message: 错误消息
        conflict_type: 冲突类型
        details: 额外的错误详情
    """

    def __init__(
        self,
        message: str,
        conflict_type: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        """初始化冲突异常

        Args:
            message: 错误消息
            conflict_type: 冲突类型（可选）
            details: 额外的错误详情（可选）
            code: 错误码（可选）
        """
        error_details = details.copy() if details else {}
        if conflict_type:
            error_details["conflict_type"] = conflict_type
        super().__init__(message, code=code or "CONFLICT", details=error_details)
        self.conflict_type = conflict_type


class PermissionException(DomainException):
    """权限异常

    当用户没有权限执行操作时抛出。

    Attributes:
        message: 错误消息
        required_permission: 需要的权限
        details: 额外的错误详情
    """

    def __init__(
        self,
        message: str,
        required_permission: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        """初始化权限异常

        Args:
            message: 错误消息
            required_permission: 需要的权限（可选）
            details: 额外的错误详情（可选）
            code: 错误码（可选）
        """
        error_details = details.copy() if details else {}
        if required_permission:
            error_details["required_permission"] = required_permission
        super().__init__(message, code=code or "PERMISSION_DENIED", details=error_details)
        self.required_permission = required_permission


class BusinessRuleException(DomainException):
    """业务规则异常

    当操作违反业务规则时抛出。

    Attributes:
        message: 错误消息
        rule: 违反的规则
        details: 额外的错误详情
    """

    def __init__(
        self,
        message: str,
        rule: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        """初始化业务规则异常

        Args:
            message: 错误消息
            rule: 违反的规则（可选）
            details: 额外的错误详情（可选）
            code: 错误码（可选）
        """
        error_details = details.copy() if details else {}
        if rule:
            error_details["rule"] = rule
        super().__init__(message, code=code or "BUSINESS_RULE_VIOLATION", details=error_details)
        self.rule = rule


# ============================================================================
# 系统异常（系统级错误）
# ============================================================================


class SystemException(BaseAppException):
    """系统异常基类

    用于表示系统级的错误，通常需要运维介入。
    """

    def _log_exception(self):
        """记录系统异常为错误级别"""
        logger.error(
            f"[{self.code}] {self.message}",
            extra={"details": self.details, "exception_type": self.__class__.__name__},
            exc_info=self.cause is not None,
        )


class DatabaseException(SystemException):
    """数据库异常

    当数据库操作失败时抛出。

    Attributes:
        message: 错误消息
        operation: 执行的操作
        details: 额外的错误详情
        cause: 原始异常
    """

    def __init__(
        self,
        message: str,
        operation: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
        code: str | None = None,
    ):
        """初始化数据库异常

        Args:
            message: 错误消息
            operation: 执行的操作（可选）
            details: 额外的错误详情（可选）
            cause: 原始异常（可选）
            code: 错误码（可选）
        """
        error_details = details.copy() if details else {}
        if operation:
            error_details["operation"] = operation
        super().__init__(message, code=code or "DB_ERROR", details=error_details, cause=cause)
        self.operation = operation


class CacheException(SystemException):
    """缓存异常

    当缓存操作失败时抛出。

    Attributes:
        message: 错误消息
        operation: 执行的操作
        details: 额外的错误详情
        cause: 原始异常
    """

    def __init__(
        self,
        message: str,
        operation: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
        code: str | None = None,
    ):
        """初始化缓存异常

        Args:
            message: 错误消息
            operation: 执行的操作（可选）
            details: 额外的错误详情（可选）
            cause: 原始异常（可选）
            code: 错误码（可选）
        """
        error_details = details.copy() if details else {}
        if operation:
            error_details["operation"] = operation
        super().__init__(message, code=code or "CACHE_ERROR", details=error_details, cause=cause)
        self.operation = operation


class ExternalServiceException(SystemException):
    """外部服务异常

    当调用外部服务失败时抛出。

    Attributes:
        message: 错误消息
        service_name: 服务名称
        details: 额外的错误详情
        cause: 原始异常
    """

    def __init__(
        self,
        message: str,
        service_name: str | None = None,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
        code: str | None = None,
    ):
        """初始化外部服务异常

        Args:
            message: 错误消息
            service_name: 服务名称（可选）
            details: 额外的错误详情（可选）
            cause: 原始异常（可选）
            code: 错误码（可选）
        """
        error_details = details.copy() if details else {}
        if service_name:
            error_details["service_name"] = service_name
        super().__init__(
            message,
            code=code or "EXTERNAL_SERVICE_ERROR",
            details=error_details,
            cause=cause,
        )
        self.service_name = service_name


class ConfigurationException(SystemException):
    """配置异常

    当配置错误时抛出。

    Attributes:
        message: 错误消息
        config_key: 配置键
        details: 额外的错误详情
    """

    def __init__(
        self,
        message: str,
        config_key: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        """初始化配置异常

        Args:
            message: 错误消息
            config_key: 配置键（可选）
            details: 额外的错误详情（可选）
            code: 错误码（可选）
        """
        error_details = details.copy() if details else {}
        if config_key:
            error_details["config_key"] = config_key
        super().__init__(message, code=code or "CONFIG_ERROR", details=error_details)
        self.config_key = config_key


# ============================================================================
# 超时异常
# ============================================================================


class TimeoutException(SystemException):
    """超时异常

    当操作超时时抛出。

    Attributes:
        message: 错误消息
        timeout_seconds: 超时时间（秒）
        operation: 执行的操作
        details: 额外的错误详情
    """

    def __init__(
        self,
        message: str,
        timeout_seconds: float | None = None,
        operation: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ):
        """初始化超时异常

        Args:
            message: 错误消息
            timeout_seconds: 超时时间（秒）（可选）
            operation: 执行的操作（可选）
            details: 额外的错误详情（可选）
            code: 错误码（可选）
        """
        error_details = details.copy() if details else {}
        if timeout_seconds is not None:
            error_details["timeout_seconds"] = timeout_seconds
        if operation:
            error_details["operation"] = operation
        super().__init__(message, code=code or "TIMEOUT", details=error_details)
        self.timeout_seconds = timeout_seconds
        self.operation = operation
