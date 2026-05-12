"""
配置验证器

验证配置文件的完整性和正确性
"""

import re
from dataclasses import dataclass, field
from typing import Any

# 支持的 LLM 提供商
SUPPORTED_PROVIDERS: set[str] = {
    "openai",
    "anthropic",
    "ollama",
    "openai_compatible",
}

# URL 验证正则
URL_PATTERN = re.compile(
    r"^https?://"  # http:// 或 https://
    r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"  # 域名
    r"localhost|"  # localhost
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"  # IP 地址
    r"(?::\d+)?"  # 可选端口
    r"(?:/?|[/?]\S+)$",
    re.IGNORECASE,
)


@dataclass
class ValidationResult:
    """验证结果"""

    is_valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, message: str) -> None:
        """添加错误"""
        self.errors.append(message)
        self.is_valid = False

    def add_warning(self, message: str) -> None:
        """添加警告"""
        self.warnings.append(message)

    def merge(self, other: "ValidationResult") -> None:
        """合并另一个验证结果"""
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        if not other.is_valid:
            self.is_valid = False


class ConfigValidator:
    """配置验证器"""

    def validate_llm_config(self, config: dict[str, Any]) -> ValidationResult:
        """
        验证 LLM 配置

        Args:
            config: LLM 配置字典

        Returns:
            验证结果
        """
        result = ValidationResult()

        # 检查必需字段
        if "models" not in config:
            result.add_error("缺少必需字段: models")
            return result

        models = config.get("models", {})
        defaults = config.get("defaults", {})
        providers = config.get("providers", {})

        # 验证每个模型配置
        for alias, model_config in models.items():
            self._validate_model_config(alias, model_config, providers, result)

        # 验证默认模型是否存在
        for purpose, model_alias in defaults.items():
            if model_alias not in models:
                result.add_error(
                    f"默认模型 '{model_alias}' (用途: {purpose}) 不存在于 models 中"
                )

        # 验证提供商配置
        for provider_name, provider_config in providers.items():
            self._validate_provider_config(provider_name, provider_config, result)

        return result

    def _validate_model_config(
        self,
        alias: str,
        config: dict[str, Any],
        providers: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        """验证单个模型配置"""
        # 检查必需字段
        required_fields = ["provider", "model_name", "display_name"]
        for field_name in required_fields:
            if field_name not in config:
                result.add_error(f"模型 '{alias}' 缺少必需字段: {field_name}")

        # 检查提供商是否支持
        provider = config.get("provider")
        if provider and provider not in SUPPORTED_PROVIDERS:
            result.add_error(f"模型 '{alias}' 使用了不支持的 provider: {provider}")

        # 检查提供商配置是否存在
        if provider and provider not in providers:
            # 对于 ollama，可以没有提供商配置
            if provider != "ollama":
                result.add_warning(
                    f"模型 '{alias}' 的 provider '{provider}' 没有对应的配置"
                )

    def _validate_provider_config(
        self,
        name: str,
        config: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        """验证提供商配置"""
        # ollama 不需要 api_key
        if name == "ollama":
            return

        # 其他提供商需要 api_key
        if "api_key" not in config:
            result.add_warning(f"提供商 '{name}' 没有配置 api_key")

        # 验证 api_base URL 格式
        api_base = config.get("api_base")
        if api_base and not URL_PATTERN.match(api_base):
            result.add_error(f"提供商 '{name}' 的 api_base URL 格式无效: {api_base}")

    def validate_api_config(self, config: dict[str, Any]) -> ValidationResult:
        """
        验证 API 配置

        Args:
            config: API 配置字典

        Returns:
            验证结果
        """
        result = ValidationResult()

        endpoints = config.get("endpoints", {})
        defaults = config.get("defaults", {})

        # 验证每个端点配置
        for name, endpoint_config in endpoints.items():
            self._validate_endpoint_config(name, endpoint_config, result)

        # 验证默认端点是否存在
        default_endpoint = defaults.get("endpoint")
        if default_endpoint and default_endpoint not in endpoints:
            result.add_error(f"默认端点 '{default_endpoint}' 不存在于 endpoints 中")

        return result

    def _validate_endpoint_config(
        self,
        name: str,
        config: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        """验证端点配置"""
        # 检查必需字段
        if "base_url" not in config:
            result.add_error(f"端点 '{name}' 缺少必需字段: base_url")
            return

        # 验证 URL 格式
        base_url = config.get("base_url")
        if base_url and not URL_PATTERN.match(base_url):
            result.add_error(f"端点 '{name}' 的 base_url URL 格式无效: {base_url}")

        # 验证 timeout
        timeout = config.get("timeout")
        if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
            result.add_error(f"端点 '{name}' 的 timeout 必须是正整数")

    def validate_app_config(self, config: dict[str, Any]) -> ValidationResult:
        """
        验证应用配置

        Args:
            config: 应用配置字典

        Returns:
            验证结果
        """
        result = ValidationResult()

        app_config = config.get("app", {})
        server_config = config.get("server", {})

        # 验证服务器端口
        port = server_config.get("port")
        if port is not None:
            if not isinstance(port, int) or port < 1 or port > 65535:
                result.add_error(f"服务器端口无效: {port}，必须在 1-65535 之间")

        # 验证环境
        environment = app_config.get("environment")
        valid_environments = {"development", "staging", "production"}
        if environment and environment not in valid_environments:
            result.add_error(
                f"无效的环境: {environment}，必须是 {valid_environments} 之一"
            )

        return result

    def validate_all(self, configs: dict[str, dict[str, Any]]) -> ValidationResult:
        """
        验证所有配置

        Args:
            configs: 配置字典，键为配置类型

        Returns:
            合并后的验证结果
        """
        result = ValidationResult()

        if "llm" in configs:
            result.merge(self.validate_llm_config(configs["llm"]))

        if "api" in configs:
            result.merge(self.validate_api_config(configs["api"]))

        if "app" in configs:
            result.merge(self.validate_app_config(configs["app"]))

        return result
