"""
DI 模块异常定义
"""

from typing import Any

from core.exceptions.base import SystemException


class DIException(SystemException):
    """DI 容器异常基类

    依赖注入容器相关异常的基类。
    """

    pass


class ServiceNotFoundError(DIException):
    """服务未找到异常

    Attributes:
        service_name: 服务名称
    """

    def __init__(self, service_name: str, details: dict[str, Any] | None = None):
        """初始化服务未找到异常

        Args:
            service_name: 服务名称
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["service_name"] = service_name
        super().__init__(
            f"Service not found: {service_name}",
            code="SERVICE_NOT_FOUND",
            details=error_details,
        )
        self.service_name = service_name


class ServiceAlreadyRegisteredError(DIException):
    """服务已注册异常

    Attributes:
        service_name: 服务名称
    """

    def __init__(self, service_name: str, details: dict[str, Any] | None = None):
        """初始化服务已注册异常

        Args:
            service_name: 服务名称
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["service_name"] = service_name
        super().__init__(
            f"Service already registered: {service_name}",
            code="SERVICE_ALREADY_REGISTERED",
            details=error_details,
        )
        self.service_name = service_name


class CircularDependencyError(DIException):
    """循环依赖异常

    Attributes:
        dependency_chain: 依赖链
    """

    def __init__(
        self,
        dependency_chain: list,
        details: dict[str, Any] | None = None,
    ):
        """初始化循环依赖异常

        Args:
            dependency_chain: 依赖链
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["dependency_chain"] = dependency_chain
        chain_str = " -> ".join(dependency_chain)
        super().__init__(
            f"Circular dependency detected: {chain_str}",
            code="CIRCULAR_DEPENDENCY",
            details=error_details,
        )
        self.dependency_chain = dependency_chain


class InvalidServiceFactoryError(DIException):
    """无效的服务工厂异常"""

    def __init__(
        self,
        message: str = "Invalid service factory",
        details: dict[str, Any] | None = None,
    ):
        """初始化无效的服务工厂异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="INVALID_SERVICE_FACTORY", details=details)


class ServiceValidationError(DIException):
    """服务验证异常"""

    def __init__(
        self,
        message: str = "Service validation failed",
        details: dict[str, Any] | None = None,
    ):
        """初始化服务验证异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="SERVICE_VALIDATION_ERROR", details=details)
