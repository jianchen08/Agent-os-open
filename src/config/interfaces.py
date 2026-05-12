"""
配置管理器统一接口

定义所有配置管理器必须实现的接口，确保统一的访问方式
"""

from typing import Any, Protocol


class IConfigManager(Protocol):
    """
    配置管理器统一接口

    所有配置管理器都应该实现这个接口，提供统一的配置访问方式
    """

    def load(self, key: str) -> dict[str, Any]:
        """
        加载配置

        Args:
            key: 配置键（如 'context_window', 'cost_control'）

        Returns:
            配置字典

        Raises:
            KeyError: 配置键不存在
        """
        ...

    def save(self, key: str, config: dict[str, Any]) -> None:
        """
        保存配置

        Args:
            key: 配置键
            config: 配置字典

        Raises:
            ValueError: 配置格式错误
        """
        ...

    def reload(self, key: str | None = None) -> None:
        """
        重新加载配置

        Args:
            key: 配置键，None 表示重新加载所有配置
        """
        ...

    def get_all_keys(self) -> list[str]:
        """
        获取所有可用的配置键

        Returns:
            配置键列表
        """
        ...

    def has_key(self, key: str) -> bool:
        """
        检查配置键是否存在

        Args:
            key: 配置键

        Returns:
            是否存在
        """
        ...

    def get_metadata(self) -> dict[str, Any]:
        """
        获取配置管理器的元数据

        Returns:
            元数据字典，包含 name, description, version 等
        """
        ...
