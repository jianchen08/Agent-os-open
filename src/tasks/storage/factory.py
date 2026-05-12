"""
任务存储工厂

根据配置创建对应的任务存储实例。
"""

import logging
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.tasks.storage.base import ITaskStorage
from src.tasks.storage.db_storage import DatabaseTaskStorage
from src.tasks.storage.file_storage import FileTaskStorage

logger = logging.getLogger(__name__)

# 存储类型定义
StorageType = Literal["file", "database"]


class TaskStorageFactory:
    """
    任务存储工厂

    根据配置创建对应的任务存储实例。
    支持的存储类型：
    - file: 文件系统存储
    - database: 数据库存储（默认）
    """

    @staticmethod
    def create(
        storage_type: StorageType | None = None,
        session: AsyncSession | None = None,
        base_path: str | None = None,
    ) -> ITaskStorage:
        """
        创建任务存储实例

        Args:
            storage_type: 存储类型，如果为 None 则从配置读取
            session: 数据库会话（仅 database 类型需要）
            base_path: 文件存储路径（仅 file 类型需要）

        Returns:
            任务存储实例

        Raises:
            ValueError: 存储类型无效或缺少必要参数
        """
        # 从配置获取存储类型
        if storage_type is None:
            storage_type = getattr(settings, "task_storage_type", "database")

        storage_type = storage_type.lower()

        if storage_type == "file":
            # 文件存储
            path = base_path or getattr(settings, "task_storage_path", "data/tasks")
            logger.info("创建文件任务存储: %s", path)
            return FileTaskStorage(base_path=path)

        elif storage_type == "database":
            # 数据库存储
            if session is None:
                raise ValueError("数据库存储需要提供 session 参数")
            logger.info("创建数据库任务存储")
            return DatabaseTaskStorage(session=session)

        else:
            raise ValueError(f"不支持的存储类型: {storage_type}，支持的类型: file, database")

    @staticmethod
    def create_file_storage(base_path: str | None = None) -> FileTaskStorage:
        """
        创建文件存储实例

        Args:
            base_path: 存储路径

        Returns:
            文件存储实例
        """
        path = base_path or getattr(settings, "task_storage_path", "data/tasks")
        return FileTaskStorage(base_path=path)

    @staticmethod
    def create_database_storage(session: AsyncSession) -> DatabaseTaskStorage:
        """
        创建数据库存储实例

        Args:
            session: 数据库会话

        Returns:
            数据库存储实例
        """
        return DatabaseTaskStorage(session=session)


def get_task_storage(
    storage_type: StorageType | None = None,
    session: AsyncSession | None = None,
    base_path: str | None = None,
) -> ITaskStorage:
    """
    获取任务存储实例（便捷函数）

    Args:
        storage_type: 存储类型，如果为 None 则从配置读取
        session: 数据库会话（仅 database 类型需要）
        base_path: 文件存储路径（仅 file 类型需要）

    Returns:
        任务存储实例
    """
    return TaskStorageFactory.create(
        storage_type=storage_type,
        session=session,
        base_path=base_path,
    )
