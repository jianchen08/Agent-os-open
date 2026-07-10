"""媒体 Provider 注册表。

暴露接口：
- MediaProviderRegistry：管理所有已注册的媒体 Provider
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from tools.media.base import MediaProvider, MediaType
from tools.media.fallback import FallbackStrategy, ProviderChain

logger = logging.getLogger(__name__)


class MediaProviderRegistry:
    """管理所有已注册的媒体 Provider。

    支持：
    - 按 provider_name 注册/注销/查询 Provider
    - 按 media_type 查询可用 Provider
    - 从 YAML 配置自动加载 Provider 配置
    - 获取指定 media_type 的 Fallback Chain
    """

    def __init__(self) -> None:
        """初始化注册表。"""
        self._providers: dict[str, MediaProvider] = {}
        self._configs: dict[str, dict[str, Any]] = {}

    def register(self, provider: MediaProvider) -> None:
        """注册 Provider。

        如果同名 Provider 已存在，则覆盖。

        Args:
            provider: 要注册的 MediaProvider 实例
        """
        name = provider.provider_name
        if name in self._providers:
            logger.debug("[MediaRegistry] Provider '%s' 已存在，将被覆盖", name)
        self._providers[name] = provider
        logger.debug(
            "[MediaRegistry] 已注册 Provider '%s' (type=%s)",
            name,
            provider.media_type.value,
        )

    def unregister(self, provider_name: str) -> None:
        """注销 Provider。

        Args:
            provider_name: 要注销的 Provider 名称
        """
        if provider_name in self._providers:
            del self._providers[provider_name]
            logger.debug("[MediaRegistry] 已注销 Provider '%s'", provider_name)

    def get(self, provider_name: str) -> MediaProvider | None:
        """获取已注册的 Provider（大小写不敏感）。

        Args:
            provider_name: Provider 名称

        Returns:
            MediaProvider 实例，不存在时返回 None
        """
        result = self._providers.get(provider_name)
        if result is not None:
            return result
        lower_name = provider_name.lower()
        for name, provider in self._providers.items():
            if name.lower() == lower_name:
                return provider
        return None

    def has(self, provider_name: str) -> bool:
        """检查 Provider 是否已注册。

        Args:
            provider_name: Provider 名称

        Returns:
            True 表示已注册
        """
        return provider_name in self._providers

    def list_all(self) -> list[MediaProvider]:
        """列出所有已注册的 Provider。

        Returns:
            Provider 列表
        """
        return list(self._providers.values())

    def list_by_type(self, media_type: MediaType) -> list[MediaProvider]:
        """按 media_type 查询可用 Provider。

        Args:
            media_type: 媒体类型

        Returns:
            匹配的 Provider 列表（按 priority 排序）
        """
        providers = [p for p in self._providers.values() if p.media_type == media_type]
        return sorted(providers, key=lambda p: p.config.priority)

    def get_chain_for_type(
        self,
        media_type: MediaType,
        strategy: FallbackStrategy = FallbackStrategy.SEQUENTIAL,
    ) -> ProviderChain:
        """获取指定 media_type 的 Fallback Chain。

        Args:
            media_type: 媒体类型
            strategy: Fallback 策略

        Returns:
            ProviderChain 实例
        """
        providers = self.list_by_type(media_type)
        return ProviderChain(providers=providers, strategy=strategy)

    def load_config(self, config_path: Path) -> dict[str, Any]:
        """从 YAML 配置文件加载 Provider 配置。

        解析配置文件中的 media 条目，过滤禁用的 Provider，
        存储配置供后续实例化使用。

        Args:
            config_path: YAML 配置文件路径

        Returns:
            解析后的配置字典（已过滤禁用项）

        Raises:
            FileNotFoundError: 配置文件不存在
            yaml.YAMLError: YAML 解析错误
        """
        if not config_path.exists():
            raise FileNotFoundError(f"媒体配置文件不存在: {config_path}")

        with open(config_path, encoding="utf-8") as f:
            raw_config = yaml.safe_load(f)

        if not raw_config or "media" not in raw_config:
            logger.warning("[MediaRegistry] 配置文件为空或缺少 'media' 键")
            return {}

        media_config: dict[str, Any] = raw_config["media"]
        parsed: dict[str, Any] = {}

        for media_type_key, type_config in media_config.items():
            if not isinstance(type_config, dict):
                continue

            default_provider = type_config.get("default_provider", "")
            providers_raw: dict[str, Any] = type_config.get("providers", {})

            # 过滤禁用的 Provider
            enabled_providers: dict[str, Any] = {}
            for name, provider_conf in providers_raw.items():
                if not isinstance(provider_conf, dict):
                    continue
                if provider_conf.get("enabled", True):
                    enabled_providers[name] = provider_conf

            parsed[media_type_key] = {
                "default_provider": default_provider,
                "providers": enabled_providers,
            }

        self._configs = parsed
        logger.info("[MediaRegistry] 已加载配置，共 %d 种媒体类型", len(parsed))
        return parsed

    def get_config(self, media_type_key: str) -> dict[str, Any] | None:
        """获取指定媒体类型的配置。

        Args:
            media_type_key: 媒体类型键名（如 'tts', 'image'）

        Returns:
            配置字典，不存在时返回 None
        """
        return self._configs.get(media_type_key)

    def clear(self) -> None:
        """清空注册表。"""
        self._providers.clear()
        self._configs.clear()
