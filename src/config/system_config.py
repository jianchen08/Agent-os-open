"""
系统配置管理

统一管理所有系统级配置文件的读写
"""

from pathlib import Path
from typing import Any

import yaml

from src.config.loader import ConfigLoader


class SystemConfigManager:
    """系统配置管理器"""

    def __init__(self, config_dir: str = "config/system"):
        """
        初始化配置管理器

        Args:
            config_dir: 系统配置目录
        """
        self.config_dir = Path(config_dir)
        self.loader = ConfigLoader(config_dir=self.config_dir.parent)
        self._cache: dict[str, dict[str, Any]] = {}

    def load_context_window_config(self) -> dict[str, Any]:
        """
        加载上下文窗口配置

        Returns:
            配置字典
        """
        return self._load_config("context_window_config.yaml")

    def save_context_window_config(self, config: dict[str, Any]) -> None:
        """
        保存上下文窗口配置

        Args:
            config: 配置字典
        """
        self._save_config("context_window_config.yaml", config)

    def load_cost_control_config(self) -> dict[str, Any]:
        """
        加载成本控制配置

        Returns:
            配置字典
        """
        return self._load_config("cost_control.yaml")

    def save_cost_control_config(self, config: dict[str, Any]) -> None:
        """
        保存成本控制配置

        Args:
            config: 配置字典
        """
        self._save_config("cost_control.yaml", config)

    def load_redis_config(self) -> dict[str, Any]:
        """
        加载 Redis 配置

        Returns:
            配置字典
        """
        return self._load_config("redis.yaml")

    def load_memory_storage_config(self) -> dict[str, Any]:
        """
        加载记忆存储配置

        Returns:
            配置字典
        """
        return self._load_config("memory_storage.yaml")

    def load_long_term_task_config(self) -> dict[str, Any]:
        """
        加载长期任务调度配置

        Returns:
            配置字典
        """
        return self._load_config("long_term_task.yaml")

    def save_long_term_task_config(self, config: dict[str, Any]) -> None:
        """
        保存长期任务调度配置

        Args:
            config: 配置字典
        """
        self._save_config("long_term_task.yaml", config)

    def _load_config(self, filename: str, use_cache: bool = True) -> dict[str, Any]:
        """
        加载配置文件

        Args:
            filename: 配置文件名
            use_cache: 是否使用缓存

        Returns:
            配置字典
        """
        # 检查缓存
        if use_cache and filename in self._cache:
            return self._cache[filename].copy()

        # 加载配置
        try:
            config = self.loader.load(f"system/{filename}")
        except Exception:
            config = {}

        # 缓存配置
        if use_cache:
            self._cache[filename] = config.copy()

        return config

    def _save_config(self, filename: str, config: dict[str, Any]) -> None:
        """
        保存配置文件

        Args:
            filename: 配置文件名
            config: 配置字典
        """
        file_path = self.config_dir / filename

        # 确保目录存在
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # 保存配置
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(
                config,
                f,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )

        # 更新缓存
        self._cache[filename] = config.copy()

    def reload(self, filename: str | None = None) -> None:
        """
        重新加载配置

        Args:
            filename: 配置文件名，None 表示重新加载所有
        """
        if filename:
            self._cache.pop(filename, None)
        else:
            self._cache.clear()

    # ============================================
    # 统一接口实现 (IConfigManager)
    # ============================================

    def load(self, key: str) -> dict[str, Any]:
        """
        统一加载接口

        Args:
            key: 配置键

        Returns:
            配置字典
        """
        key_map = {
            "context_window": "context_window_config.yaml",
            "cost_control": "cost_control.yaml",
            "redis": "redis.yaml",
            "memory_storage": "memory_storage.yaml",
            "long_term_task": "long_term_task.yaml",
        }

        if key not in key_map:
            raise KeyError(f"未知的配置键: {key}")

        return self._load_config(key_map[key])

    def save(self, key: str, config: dict[str, Any]) -> None:
        """
        统一保存接口

        Args:
            key: 配置键
            config: 配置字典
        """
        key_map = {
            "context_window": "context_window_config.yaml",
            "cost_control": "cost_control.yaml",
            "redis": "redis.yaml",
            "memory_storage": "memory_storage.yaml",
            "long_term_task": "long_term_task.yaml",
        }

        if key not in key_map:
            raise KeyError(f"未知的配置键: {key}")

        self._save_config(key_map[key], config)

    def get_all_keys(self) -> list[str]:
        """
        获取所有可用的配置键

        Returns:
            配置键列表
        """
        return [
            "context_window",
            "cost_control",
            "redis",
            "memory_storage",
            "long_term_task",
        ]

    def has_key(self, key: str) -> bool:
        """
        检查配置键是否存在

        Args:
            key: 配置键

        Returns:
            是否存在
        """
        return key in self.get_all_keys()

    def get_metadata(self) -> dict[str, Any]:
        """
        获取配置管理器的元数据

        Returns:
            元数据字典
        """
        return {
            "name": "system",
            "description": "系统级配置管理器",
            "version": "1.0.0",
            "config_dir": str(self.config_dir),
            "available_keys": self.get_all_keys(),
        }


# 全局单例
_system_config_manager: SystemConfigManager | None = None


def get_system_config_manager() -> SystemConfigManager:
    """
    获取系统配置管理器单例

    Returns:
        SystemConfigManager 实例
    """
    global _system_config_manager
    if _system_config_manager is None:
        _system_config_manager = SystemConfigManager()
    return _system_config_manager
