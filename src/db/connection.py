"""
数据库连接管理

提供异步数据库连接和会话管理
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config.settings import get_settings

# 模块级单例
_db_manager_instance: Optional["DatabaseManager"] = None


class DatabaseManager:
    """数据库管理器"""

    def __init__(self, database_url: str | None = None):
        """
        初始化数据库管理器

        Args:
            database_url: 数据库连接 URL，如果为 None 则从 settings 获取
        """
        settings = get_settings()

        self.database_url = database_url or settings.database_url

        # 创建异步引擎 - 根据数据库类型选择配置
        if "sqlite" in self.database_url.lower():
            # SQLite 特定优化 - 使用 QueuePool 但配置更大的池
            self.engine: AsyncEngine = create_async_engine(
                self.database_url,
                echo=settings.db_echo,
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_timeout=settings.db_pool_timeout,
                pool_pre_ping=True,
                connect_args={
                    "check_same_thread": False,
                },
            )
        else:
            # PostgreSQL等支持连接池的数据库
            self.engine: AsyncEngine = create_async_engine(
                self.database_url,
                echo=settings.db_echo,
                # 连接池优化 - 使用配置参数
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_timeout=settings.db_pool_timeout,
                pool_recycle=3600,  # 1小时回收连接
                pool_pre_ping=True,  # 使用前检查连接是否有效
                # 查询优化
                connect_args={
                    "server_settings": {
                        "jit": "off",  # 关闭JIT以提高小查询性能
                        "statement_timeout": "30s",  # 查询超时
                        "idle_in_transaction_session_timeout": "60s",  # 事务空闲超时
                    }
                },
            )

        # 创建会话工厂
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        获取数据库会话（上下文管理器）

        Yields:
            AsyncSession: 数据库会话

        Example:
            async with manager.get_session() as session:
                result = await session.execute(query)
        """
        session = self.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def close(self) -> None:
        """关闭数据库连接"""
        await self.engine.dispose()

    async def create_all(self) -> None:
        """创建所有表（开发/测试用）"""
        from src.db.models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        """删除所有表（测试用）"""
        from src.db.models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


def get_db_manager() -> DatabaseManager:
    """
    获取数据库管理器单例

    Returns:
        DatabaseManager 实例
    """
    global _db_manager_instance
    if _db_manager_instance is None:
        _db_manager_instance = DatabaseManager()
    return _db_manager_instance


def reset_db_manager() -> None:
    """重置数据库管理器单例（用于测试）"""
    global _db_manager_instance
    _db_manager_instance = None


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI 依赖注入用的会话获取函数

    Yields:
        AsyncSession: 数据库会话
    """
    manager = get_db_manager()
    async with manager.get_session() as session:
        yield session


def get_session_context():
    """
    获取数据库会话上下文管理器

    用于非 FastAPI 场景下获取数据库会话

    Returns:
        上下文管理器，可用于 async with 语句

    Example:
        async with get_session_context() as session:
            result = await session.execute(query)
    """
    return get_db_manager().get_session()
