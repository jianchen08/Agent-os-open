"""服务提供者 — 统一管理运行时服务实例的获取。

替代分散在各个工具类中的 sys._agent_os_* 全局变量获取模式。
"""

import logging
import sys
from typing import Any, Callable

logger = logging.getLogger(__name__)


class ServiceProvider:
    """运行时服务提供者（单例）。

    获取优先级：
    1. 显式注册的实例（register()）
    2. sys._agent_os_* 全局变量（CLI 设置）
    3. 懒加载创建（通过 factory）
    """

    _instance: "ServiceProvider | None" = None

    def __new__(cls) -> "ServiceProvider":
        """创建或返回单例实例。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._services: dict[str, Any] = {}
        return cls._instance

    def register(self, name: str, instance: Any) -> None:
        """注册服务实例。

        Args:
            name: 服务名称（不含 _agent_os_ 前缀）
            instance: 服务实例
        """
        self._services[name] = instance

    def get(self, name: str) -> Any | None:
        """获取服务实例。

        优先从已注册实例中获取，其次从 sys._agent_os_{name} 获取。
        找到后自动缓存到本地字典。

        Args:
            name: 服务名称（不含 _agent_os_ 前缀）

        Returns:
            服务实例，未找到返回 None
        """
        if name in self._services:
            return self._services[name]
        key = f"_agent_os_{name}"
        instance = getattr(sys, key, None)
        if instance is not None:
            self._services[name] = instance
            return instance
        return None

    def get_or_create(self, name: str, factory: Callable[[], Any]) -> Any | None:
        """获取或创建服务实例。

        先尝试 get()，如果获取不到则调用 factory 创建并缓存。

        Args:
            name: 服务名称（不含 _agent_os_ 前缀）
            factory: 创建服务实例的可调用对象

        Returns:
            服务实例，创建失败返回 None
        """
        instance = self.get(name)
        if instance is not None:
            return instance
        try:
            instance = factory()
            self._services[name] = instance
            return instance
        except Exception as e:
            logger.error("[ServiceProvider] 创建服务 %s 失败: %s", name, e)
            return None

    @classmethod
    def reset(cls) -> None:
        """重置单例（仅用于测试）。"""
        cls._instance = None


def get_service_provider() -> ServiceProvider:
    """获取全局 ServiceProvider 实例。

    Returns:
        ServiceProvider 单例实例
    """
    return ServiceProvider()
