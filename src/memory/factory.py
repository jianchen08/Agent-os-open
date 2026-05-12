"""
记忆存储工厂

根据配置创建存储实例，实现依赖注入
"""

import logging
from typing import Any

from src.config.loader import ConfigLoader
from src.memory.adapters.db_storage import DBEpisodeStorage, DBSemanticStorage
from src.memory.adapters.file_storage import FileEpisodeStorage, FileSemanticStorage
from src.memory.ports import IEpisodeStorage, ISemanticStorage

logger = logging.getLogger(__name__)


class StorageFactory:
    """
    存储工厂

    根据配置创建不同类型的存储实例
    支持的存储后端：database | redis | file
    """

    # 默认配置
    DEFAULT_CONFIG = {
        "episode_backend": "database",
        "semantic_backend": "database",
        "database": {
            "pool_size": 10,
        },
        "redis": {
            "host": "localhost",
            "port": 6379,
            "ttl": 86400,
        },
        "file": {
            "base_path": "./data/memory",
            "compression": False,
        },
    }

    def __init__(self, config: dict[str, Any] | None = None):
        """
        初始化存储工厂

        Args:
            config: 配置字典，如果为 None 则加载默认配置
        """
        if config is None:
            config = self._load_config()

        self.config = {**self.DEFAULT_CONFIG, **config}

    def _load_config(self) -> dict[str, Any]:
        """
        从配置文件加载存储配置

        Returns:
            配置字典
        """
        try:
            loader = ConfigLoader()
            return loader.load("memory_storage.yaml")
        except Exception as e:
            logger.warning(f"加载 memory_storage.yaml 失败，使用默认配置: {e}")
            return {}

    def create_episode_storage(
        self,
        backend: str | None = None,
        session_factory: Any | None = None,
    ) -> IEpisodeStorage:
        """
        创建情景记忆存储实例

        Args:
            backend: 存储后端类型（database | redis | file）
                     如果为 None，则使用配置文件中的默认值
            session_factory: 数据库会话工厂（仅 database 后端需要）

        Returns:
            情景记忆存储实例

        Raises:
            ValueError: 不支持的存储后端类型
        """
        if backend is None:
            backend = self.config.get("episode_backend", "database")

        backend = backend.lower()

        if backend == "database":
            if session_factory is None:
                raise ValueError("database 后端需要提供 session_factory 参数")
            return DBEpisodeStorage(session_factory)

        elif backend == "redis":
            raise ValueError(
                "Redis 存储后端尚未实现。"
                "请使用 database 或 file 后端。"
            )

        elif backend == "file":
            file_config = self.config.get("file", {})
            return FileEpisodeStorage(
                base_path=file_config.get("base_path", "./data/memory"),
                compression=file_config.get("compression", False),
            )

        else:
            raise ValueError(
                f"不支持的情景记忆存储后端: {backend}。"
                f"支持的类型: database, redis, file"
            )

    def create_semantic_storage(
        self,
        backend: str | None = None,
        session_factory: Any | None = None,
    ) -> ISemanticStorage:
        """
        创建语义记忆存储实例

        Args:
            backend: 存储后端类型（database | redis | file）
                     如果为 None，则使用配置文件中的默认值
            session_factory: 数据库会话工厂（仅 database 后端需要）

        Returns:
            语义记忆存储实例

        Raises:
            ValueError: 不支持的存储后端类型
        """
        if backend is None:
            backend = self.config.get("semantic_backend", "database")

        backend = backend.lower()

        if backend == "database":
            if session_factory is None:
                raise ValueError("database 后端需要提供 session_factory 参数")
            return DBSemanticStorage(session_factory)

        elif backend == "redis":
            raise ValueError(
                "Redis 存储后端尚未实现。"
                "请使用 database 或 file 后端。"
            )

        elif backend == "file":
            file_config = self.config.get("file", {})
            return FileSemanticStorage(
                base_path=file_config.get("base_path", "./data/memory/knowledge"),
            )

        else:
            raise ValueError(
                f"不支持的语义记忆存储后端: {backend}。"
                f"支持的类型: database, redis, file"
            )

    def create_storage_pair(
        self,
        episode_backend: str | None = None,
        semantic_backend: str | None = None,
        session_factory: Any | None = None,
    ) -> tuple[IEpisodeStorage, ISemanticStorage]:
        """
        创建情景记忆和语义记忆存储实例对

        Args:
            episode_backend: 情景记忆存储后端类型
            semantic_backend: 语义记忆存储后端类型
            session_factory: 数据库会话工厂

        Returns:
            (情景记忆存储, 语义记忆存储) 元组
        """
        episode_storage = self.create_episode_storage(
            backend=episode_backend,
            session_factory=session_factory,
        )
        semantic_storage = self.create_semantic_storage(
            backend=semantic_backend,
            session_factory=session_factory,
        )

        return episode_storage, semantic_storage


# 全局工厂实例（单例）
_global_factory: StorageFactory | None = None


def get_storage_factory(config: dict[str, Any] | None = None) -> StorageFactory:
    """
    获取全局存储工厂实例（单例模式）

    Args:
        config: 配置字典（仅在首次调用时生效）

    Returns:
        存储工厂实例
    """
    global _global_factory

    if _global_factory is None:
        _global_factory = StorageFactory(config)

    return _global_factory


def create_episode_storage(
    backend: str | None = None,
    session_factory: Any | None = None,
) -> IEpisodeStorage:
    """
    便捷函数：创建情景记忆存储实例

    Args:
        backend: 存储后端类型
        session_factory: 数据库会话工厂

    Returns:
        情景记忆存储实例
    """
    factory = get_storage_factory()
    return factory.create_episode_storage(backend, session_factory)


def create_semantic_storage(
    backend: str | None = None,
    session_factory: Any | None = None,
) -> ISemanticStorage:
    """
    便捷函数：创建语义记忆存储实例

    Args:
        backend: 存储后端类型
        session_factory: 数据库会话工厂

    Returns:
        语义记忆存储实例
    """
    factory = get_storage_factory()
    return factory.create_semantic_storage(backend, session_factory)
