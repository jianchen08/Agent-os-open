"""
DI 容器异常定义
"""
from src.core.exceptions import SystemException


class DIException(SystemException):
    pass


class ServiceNotFoundError(DIException):
    def __init__(self, service_name: str, details: dict = None):
        error_details = (details or {}).copy()
        error_details["service_name"] = service_name
        super().__init__(f"Service not found: {service_name}", code="SERVICE_NOT_FOUND", details=error_details)
        self.service_name = service_name


class ServiceAlreadyRegisteredError(DIException):
    def __init__(self, service_name: str, details: dict = None):
        error_details = (details or {}).copy()
        error_details["service_name"] = service_name
        super().__init__(f"Service already registered: {service_name}", code="SERVICE_ALREADY_REGISTERED", details=error_details)
        self.service_name = service_name


class CircularDependencyError(DIException):
    def __init__(self, dependency_chain: list, details: dict = None):
        error_details = (details or {}).copy()
        error_details["dependency_chain"] = dependency_chain
        chain_str = " -> ".join(dependency_chain)
        super().__init__(f"Circular dependency detected: {chain_str}", code="CIRCULAR_DEPENDENCY", details=error_details)
        self.dependency_chain = dependency_chain


class InvalidServiceFactoryError(DIException):
    def __init__(self, message: str = "Invalid service factory", details: dict = None):
        super().__init__(message, code="INVALID_SERVICE_FACTORY", details=details)


class ServiceValidationError(DIException):
    def __init__(self, message: str = "Service validation failed", details: dict = None):
        super().__init__(message, code="SERVICE_VALIDATION_ERROR", details=details)
