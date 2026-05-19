"""
数据库连接管理（非 ORM 存根）

提供降级接口，不再依赖 SQLAlchemy。
所有函数返回 None 或空操作，确保系统在无数据库环境下可降级运行。
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Optional

logger = logging.getLogger(__name__)


class DatabaseManager:
    """数据库管理器（降级存根）"""

    def __init__(self, database_url: str | None = None):
        self.database_url = database_url
        logger.debug("DatabaseManager 初始化（非 ORM 模式，无数据库连接）")

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[Any, None]:
        """获取数据库会话（降级：返回 None）。"""
        yield None

    async def close(self) -> None:
        """关闭数据库连接（空操作）。"""
        pass

    async def create_all(self) -> None:
        """创建所有表（空操作）。"""
        pass

    async def drop_all(self) -> None:
        """删除所有表（空操作）。"""
        pass


# 模块级单例
_db_manager_instance: Optional[DatabaseManager] = None


def get_db_manager() -> DatabaseManager:
    """获取数据库管理器单例。"""
    global _db_manager_instance
    if _db_manager_instance is None:
        _db_manager_instance = DatabaseManager()
    return _db_manager_instance


def reset_db_manager() -> None:
    """重置数据库管理器单例（用于测试）。"""
    global _db_manager_instance
    _db_manager_instance = None


async def get_async_session() -> AsyncGenerator[Any, None]:
    """FastAPI 依赖注入用的会话获取函数（降级：yield None）。"""
    yield None


def get_session_context():
    """获取数据库会话上下文管理器（降级）。"""

    @asynccontextmanager
    async def _ctx():
        yield None

    return _ctx()
