"""
配置模块异常定义
"""
from src.core.exceptions import SystemException


class ConfigException(SystemException):
    pass


class ConfigNotFoundError(ConfigException):
    def __init__(self, path: str, details: dict = None):
        error_details = (details or {}).copy()
        error_details["path"] = path
        super().__init__(f"配置文件不存在: {path}", code="CONFIG_NOT_FOUND", details=error_details)
        self.path = path


class ConfigValidationError(ConfigException):
    def __init__(self, errors: list, details: dict = None):
        error_details = (details or {}).copy()
        error_details["errors"] = errors
        super().__init__(f"配置验证失败: {'; '.join(errors)}", code="CONFIG_VALIDATION_ERROR", details=error_details)
        self.errors = errors


class ModelNotFoundError(ConfigException):
    def __init__(self, alias: str, details: dict = None):
        error_details = (details or {}).copy()
        error_details["alias"] = alias
        super().__init__(f"模型别名不存在: {alias}", code="MODEL_NOT_FOUND", details=error_details)
        self.alias = alias


class ProviderNotFoundError(ConfigException):
    def __init__(self, provider: str, details: dict = None):
        error_details = (details or {}).copy()
        error_details["provider"] = provider
        super().__init__(f"提供商不存在: {provider}", code="PROVIDER_NOT_FOUND", details=error_details)
        self.provider = provider


class EndpointNotFoundError(ConfigException):
    def __init__(self, endpoint: str, details: dict = None):
        error_details = (details or {}).copy()
        error_details["endpoint"] = endpoint
        super().__init__(f"端点不存在: {endpoint}", code="ENDPOINT_NOT_FOUND", details=error_details)
        self.endpoint = endpoint


class EnvVarNotFoundError(ConfigException):
    def __init__(self, var_name: str, details: dict = None):
        error_details = (details or {}).copy()
        error_details["var_name"] = var_name
        super().__init__(f"环境变量未设置: {var_name}", code="ENV_VAR_NOT_FOUND", details=error_details)
        self.var_name = var_name
