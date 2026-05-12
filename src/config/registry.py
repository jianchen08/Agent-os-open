"""
配置注册中心

提供统一的配置访问入口，管理所有配置管理器
"""

import logging
from typing import Any, Optional

from src.config.interfaces import IConfigManager

logger = logging.getLogger(__name__)


class ConfigRegistry:
    """
    配置注册中心

    统一管理所有配置管理器，提供统一的访问接口
    """

    _instance: Optional["ConfigRegistry"] = None

    def __init__(self):
        """初始化注册中心"""
        self._managers: dict[str, IConfigManager] = {}
        self._initialized = False

    @classmethod
    def get_instance(cls) -> "ConfigRegistry":
        """
        获取单例实例

        Returns:
            ConfigRegistry 实例
        """
        if cls._instance is None:
            cls._instance = cls()
            cls._instance._register_default_managers()
        return cls._instance

    def _register_default_managers(self) -> None:
        """注册默认的配置管理器"""
        if self._initialized:
            return

        try:
            # 注册系统配置管理器
            from src.config.system_config import get_system_config_manager

            self.register("system", get_system_config_manager())
            logger.debug("已注册系统配置管理器")

            # 注册 LLM 配置管理器
            from src.config.llm_config import get_llm_config

            self.register("llm", get_llm_config())
            logger.debug("已注册 LLM 配置管理器")

            self._initialized = True
            logger.info("配置注册中心初始化完成")

        except Exception as e:
            logger.error(f"注册默认配置管理器失败: {e}")

    def register(self, name: str, manager: IConfigManager) -> None:
        """
        注册配置管理器

        Args:
            name: 管理器名称（如 'system', 'llm'）
            manager: 配置管理器实例
        """
        if name in self._managers:
            logger.warning(f"配置管理器 '{name}' 已存在，将被覆盖")

        self._managers[name] = manager
        logger.debug(f"已注册配置管理器: {name}")

    def unregister(self, name: str) -> None:
        """
        注销配置管理器

        Args:
            name: 管理器名称
        """
        if name in self._managers:
            del self._managers[name]
            logger.debug(f"已注销配置管理器: {name}")

    def get_manager(self, name: str) -> IConfigManager:
        """
        获取配置管理器

        Args:
            name: 管理器名称

        Returns:
            配置管理器实例

        Raises:
            KeyError: 管理器不存在
        """
        if name not in self._managers:
            raise KeyError(f"配置管理器 '{name}' 不存在")

        return self._managers[name]

    def has_manager(self, name: str) -> bool:
        """
        检查管理器是否存在

        Args:
            name: 管理器名称

        Returns:
            是否存在
        """
        return name in self._managers

    def list_managers(self) -> list[str]:
        """
        列出所有已注册的管理器

        Returns:
            管理器名称列表
        """
        return list(self._managers.keys())

    # ============================================
    # 统一访问接口
    # ============================================

    def get(self, manager_name: str, key: str) -> Any:
        """
        统一读取配置

        Args:
            manager_name: 管理器名称（如 'system', 'llm'）
            key: 配置键

        Returns:
            配置值

        Example:
            >>> registry = get_config_registry()
            >>> config = registry.get("system", "context_window")
            >>> model = registry.get("llm", "model:deepseek-chat")
        """
        manager = self.get_manager(manager_name)
        return manager.load(key)

    def set(self, manager_name: str, key: str, value: Any) -> None:
        """
        统一写入配置

        Args:
            manager_name: 管理器名称
            key: 配置键
            value: 配置值

        Example:
            >>> registry = get_config_registry()
            >>> registry.set("system", "context_window", {...})
        """
        manager = self.get_manager(manager_name)
        manager.save(key, value)

    def reload(self, manager_name: str, key: str | None = None) -> None:
        """
        重新加载配置

        Args:
            manager_name: 管理器名称
            key: 配置键，None 表示重新加载所有
        """
        manager = self.get_manager(manager_name)
        manager.reload(key)

    def get_all_keys(self, manager_name: str) -> list[str]:
        """
        获取管理器的所有配置键

        Args:
            manager_name: 管理器名称

        Returns:
            配置键列表
        """
        manager = self.get_manager(manager_name)
        return manager.get_all_keys()

    def has_key(self, manager_name: str, key: str) -> bool:
        """
        检查配置键是否存在

        Args:
            manager_name: 管理器名称
            key: 配置键

        Returns:
            是否存在
        """
        manager = self.get_manager(manager_name)
        return manager.has_key(key)

    def get_metadata(self, manager_name: str) -> dict[str, Any]:
        """
        获取管理器的元数据

        Args:
            manager_name: 管理器名称

        Returns:
            元数据字典
        """
        manager = self.get_manager(manager_name)
        return manager.get_metadata()

    def get_all_metadata(self) -> dict[str, dict[str, Any]]:
        """
        获取所有管理器的元数据

        Returns:
            管理器名称到元数据的映射
        """
        return {
            name: manager.get_metadata() for name, manager in self._managers.items()
        }


# ============================================
# 便捷函数
# ============================================


def get_config_registry() -> ConfigRegistry:
    """
    获取配置注册中心单例

    Returns:
        ConfigRegistry 实例
    """
    return ConfigRegistry.get_instance()


def get_config(manager_name: str, key: str) -> Any:
    """
    便捷函数：读取配置

    Args:
        manager_name: 管理器名称
        key: 配置键

    Returns:
        配置值

    Example:
        >>> from src.config.registry import get_config
        >>> config = get_config("system", "context_window")
    """
    registry = get_config_registry()
    return registry.get(manager_name, key)


def set_config(manager_name: str, key: str, value: Any) -> None:
    """
    便捷函数：写入配置

    Args:
        manager_name: 管理器名称
        key: 配置键
        value: 配置值

    Example:
        >>> from src.config.registry import set_config
        >>> set_config("system", "context_window", {...})
    """
    registry = get_config_registry()
    registry.set(manager_name, key, value)
