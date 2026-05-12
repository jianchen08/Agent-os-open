"""
API 配置管理器

管理 API 端点配置和限流配置
"""

from typing import Any, Optional

from src.config.loader import ConfigLoader
from src.config.schemas import CORSConfig, EndpointConfig, RateLimitConfig
from src.core.exceptions import EndpointNotFoundError

# 模块级单例
_api_config_instance: Optional["APIConfigManager"] = None


class APIConfigManager:
    """API 配置管理器"""

    def __init__(self, config: dict[str, Any] | None = None):
        """
        初始化 API 配置管理器

        Args:
            config: API 配置字典，如果为 None 则从文件加载
        """
        if config is None:
            loader = ConfigLoader()
            try:
                config = loader.load("api.yaml")
            except Exception:
                config = {}

        self._endpoints: dict[str, EndpointConfig] = {}
        self._default_endpoint: str = "local"
        self._rate_limits = RateLimitConfig()
        self._cors = CORSConfig()

        self._parse_config(config)

    def _parse_config(self, config: dict[str, Any]) -> None:
        """解析配置"""
        # 解析端点配置
        for name, endpoint_data in config.get("endpoints", {}).items():
            self._endpoints[name] = EndpointConfig(**endpoint_data)

        # 解析默认端点
        defaults = config.get("defaults", {})
        self._default_endpoint = defaults.get("endpoint", "local")

        # 解析限流配置
        rate_limits_data = config.get("rate_limits", {})
        if rate_limits_data:
            # 转换键名（global -> global_limit）
            if "global" in rate_limits_data:
                rate_limits_data["global_limit"] = rate_limits_data.pop("global")
            self._rate_limits = RateLimitConfig(**rate_limits_data)

        # 解析 CORS 配置
        cors_data = config.get("cors", {})
        if cors_data:
            self._cors = CORSConfig(**cors_data)

    def get_endpoint(self, alias: str) -> EndpointConfig:
        """
        获取端点配置

        Args:
            alias: 端点别名

        Returns:
            端点配置

        Raises:
            EndpointNotFoundError: 端点不存在
        """
        if alias not in self._endpoints:
            raise EndpointNotFoundError(alias)
        return self._endpoints[alias]

    def get_default(self) -> EndpointConfig:
        """
        获取默认端点

        Returns:
            默认端点配置

        Raises:
            EndpointNotFoundError: 默认端点不存在
        """
        return self.get_endpoint(self._default_endpoint)

    def get_full_url(self, alias: str, path: str) -> str:
        """
        构建完整 URL

        Args:
            alias: 端点别名
            path: API 路径

        Returns:
            完整 URL
        """
        endpoint = self.get_endpoint(alias)
        base = endpoint.base_url.rstrip("/")
        version = endpoint.version
        path = path.lstrip("/")

        return f"{base}/{version}/{path}"

    def list_endpoints(self) -> list[str]:
        """
        列出所有端点

        Returns:
            端点别名列表
        """
        return list(self._endpoints.keys())

    def has_endpoint(self, alias: str) -> bool:
        """检查端点是否存在"""
        return alias in self._endpoints

    @property
    def rate_limits(self) -> RateLimitConfig:
        """获取限流配置"""
        return self._rate_limits

    @property
    def cors(self) -> CORSConfig:
        """获取 CORS 配置"""
        return self._cors


def get_api_config() -> APIConfigManager:
    """
    获取 API 配置管理器单例

    Returns:
        APIConfigManager 实例
    """
    global _api_config_instance
    if _api_config_instance is None:
        _api_config_instance = APIConfigManager()
    return _api_config_instance


def reset_api_config() -> None:
    """重置 API 配置单例（用于测试）"""
    global _api_config_instance
    _api_config_instance = None
