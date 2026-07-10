"""
Config 模块异常定义
"""

from typing import Any

from core.exceptions.base import SystemException


class ConfigException(SystemException):
    """配置异常基类

    配置模块相关异常的基类。
    """

    pass


class ConfigNotFoundError(ConfigException):
    """配置文件不存在异常

    Attributes:
        path: 配置文件路径
    """

    def __init__(self, path: str, details: dict[str, Any] | None = None):
        """初始化配置文件不存在异常

        Args:
            path: 配置文件路径
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["path"] = path
        super().__init__(
            f"配置文件不存在: {path}",
            code="CONFIG_NOT_FOUND",
            details=error_details,
        )
        self.path = path


class ConfigValidationError(ConfigException):
    """配置验证失败异常

    Attributes:
        errors: 验证错误列表
    """

    def __init__(
        self,
        errors: list[str],
        details: dict[str, Any] | None = None,
    ):
        """初始化配置验证失败异常

        Args:
            errors: 验证错误列表
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["errors"] = errors
        super().__init__(
            f"配置验证失败: {'; '.join(errors)}",
            code="CONFIG_VALIDATION_ERROR",
            details=error_details,
        )
        self.errors = errors


class ModelNotFoundError(ConfigException):
    """模型别名不存在异常

    Attributes:
        alias: 模型别名
    """

    def __init__(self, alias: str, details: dict[str, Any] | None = None):
        """初始化模型别名不存在异常

        Args:
            alias: 模型别名
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["alias"] = alias
        super().__init__(
            f"模型别名不存在: {alias}",
            code="MODEL_NOT_FOUND",
            details=error_details,
        )
        self.alias = alias


class ProviderNotFoundError(ConfigException):
    """提供商不存在异常

    Attributes:
        provider: 提供商名称
    """

    def __init__(self, provider: str, details: dict[str, Any] | None = None):
        """初始化提供商不存在异常

        Args:
            provider: 提供商名称
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["provider"] = provider
        super().__init__(
            f"提供商不存在: {provider}",
            code="PROVIDER_NOT_FOUND",
            details=error_details,
        )
        self.provider = provider


class EndpointNotFoundError(ConfigException):
    """端点不存在异常

    Attributes:
        endpoint: 端点名称
    """

    def __init__(self, endpoint: str, details: dict[str, Any] | None = None):
        """初始化端点不存在异常

        Args:
            endpoint: 端点名称
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["endpoint"] = endpoint
        super().__init__(
            f"端点不存在: {endpoint}",
            code="ENDPOINT_NOT_FOUND",
            details=error_details,
        )
        self.endpoint = endpoint


class EnvVarNotFoundError(ConfigException):
    """环境变量未设置异常

    Attributes:
        var_name: 环境变量名称
    """

    def __init__(self, var_name: str, details: dict[str, Any] | None = None):
        """初始化环境变量未设置异常

        Args:
            var_name: 环境变量名称
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["var_name"] = var_name
        super().__init__(
            f"环境变量未设置: {var_name}",
            code="ENV_VAR_NOT_FOUND",
            details=error_details,
        )
        self.var_name = var_name
