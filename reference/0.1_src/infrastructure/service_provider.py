"""服务提供者 — 统一管理运行时服务实例的获取。

统一管理运行时服务实例的获取，替代 sys._agent_os_* 全局变量。
"""

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class ServiceProvider:
    """运行时服务提供者（单例）。

    获取优先级：
    1. 显式注册的实例（register() / register_services()）
    2. 懒加载创建（通过 factory / get_or_create()）
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
            name: 服务名称
            instance: 服务实例
        """
        self._services[name] = instance

    def register_services(
        self,
        services: dict[str, Any],
        *,
        overwrite: bool = False,
    ) -> None:
        """批量注册服务实例（幂等，不覆盖已注册的同名服务）。

        用于 Application.build_services() 等场景，一次性将多个服务
        注册到 ServiceProvider，统一管理服务实例。

        Args:
            services: 服务名称到实例的映射字典
            overwrite: 是否覆盖已存在的同名服务，默认 False（保留第一个）
        """
        for name, instance in services.items():
            if overwrite or name not in self._services:
                self._services[name] = instance

    def get(self, name: str) -> Any | None:
        """获取服务实例。

        所有服务通过 register() 或 register_services() 注册，
        不再使用 sys._agent_os_* 全局变量回退。

        Args:
            name: 服务名称

        Returns:
            服务实例，未找到返回 None
        """
        return self._services.get(name)

    def get_all_services(self) -> dict[str, Any]:
        """获取所有已注册服务的字典副本。

        用于需要批量传递 services 的场景（如管道引擎创建），
        避免外部直接访问 ``_services`` 私有属性。

        Returns:
            服务名称到实例的映射字典（浅拷贝）
        """
        return dict(self._services)

    def get_or_create(self, name: str, factory: Callable[[], Any]) -> Any | None:
        """获取或创建服务实例。

        先尝试 get()，如果获取不到则调用 factory 创建并缓存。

        Args:
            name: 服务名称
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
