"""
统一会话管理模块

提供标准化的数据库会话管理策略，解决系统中会话管理方式不一致的问题。

设计原则：
1. 分层管理：不同层级使用不同的会话获取策略
2. 显式边界：事务边界必须显式定义
3. 资源安全：确保会话始终正确关闭
4. 向后兼容：支持现有的会话传递模式

使用模式：
- API 层：使用 get_async_session() 依赖注入
- Service 层：接收外部会话，可创建独立事务
- Manager 层：接收外部会话，提供事务封装
- Executor 层：使用上下文管理器自主管理会话
"""

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_db_manager

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SessionManager:
    """
    会话管理器

    提供统一的会话获取和管理接口，支持多种使用场景：
    1. 独立会话：完全自主管理的会话生命周期
    2. 嵌套会话：在已有会话中执行操作（共享事务）
    3. 独立事务：在已有会话中创建独立事务
    """

    def __init__(self):
        self._db_manager = get_db_manager()

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        获取独立管理的会话

        适用于：
        - TaskExecutor 等需要自主管理会话生命周期的组件
        - 后台任务、定时任务等无请求上下文场景
        - 长时间运行的操作

        Yields:
            AsyncSession: 数据库会话（自动提交/回滚/关闭）

        Example:
            async with session_manager.get_session() as session:
                result = await session.execute(query)
                # 自动提交和关闭
        """
        session = self._db_manager.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @asynccontextmanager
    async def get_nested_session(
        self, parent_session: AsyncSession
    ) -> AsyncGenerator[AsyncSession, None]:
        """
        获取嵌套会话（共享父会话的事务）

        适用于：
        - 在已有事务中执行操作
        - Repository 层操作
        - 需要访问父会话但不想创建新事务的场景

        Args:
            parent_session: 父会话

        Yields:
            AsyncSession: 父会话本身（不管理事务）

        Example:
            async with session_manager.get_nested_session(parent_session) as session:
                # session 就是 parent_session，共享事务
                result = await session.execute(query)
        """
        # 直接返回父会话，不管理事务
        yield parent_session

    @asynccontextmanager
    async def get_independent_transaction(
        self, parent_session: AsyncSession | None = None
    ) -> AsyncGenerator[AsyncSession, None]:
        """
        获取独立事务的会话

        适用于：
        - 需要在独立事务中执行操作（如评估结果保存）
        - 不想影响父会话事务的操作
        - 需要立即提交的操作

        Args:
            parent_session: 父会话（可选，用于获取连接）

        Yields:
            AsyncSession: 新会话（独立事务）

        Example:
            async with session_manager.get_independent_transaction() as session:
                # 独立事务，不影响外部事务
                await session.execute(update_stmt)
                # 自动提交
        """
        # 创建新会话，与父会话无关
        session = self._db_manager.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def execute_in_transaction(
        self,
        operation: Callable[[AsyncSession], Any],
        session: AsyncSession | None = None,
    ) -> Any:
        """
        在事务中执行操作

        如果提供了会话，则在提供的会话中执行（不管理事务）
        如果没有提供会话，则创建新会话并管理事务

        Args:
            operation: 异步操作函数，接收会话参数
            session: 可选的现有会话

        Returns:
            操作函数的返回值

        Example:
            result = await session_manager.execute_in_transaction(
                lambda s: repo.get_task(s, task_id)
            )
        """
        if session is not None:
            # 使用提供的会话，不管理事务
            return await operation(session)
        else:
            # 创建新会话并管理事务
            async with self.get_session() as new_session:
                return await operation(new_session)


# 全局会话管理器实例
_session_manager_instance: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """
    获取会话管理器单例

    Returns:
        SessionManager 实例
    """
    global _session_manager_instance
    if _session_manager_instance is None:
        _session_manager_instance = SessionManager()
    return _session_manager_instance


def reset_session_manager() -> None:
    """重置会话管理器单例（用于测试）"""
    global _session_manager_instance
    _session_manager_instance = None


# =============================================================================
# 便捷函数
# =============================================================================


@asynccontextmanager
async def managed_session() -> AsyncGenerator[AsyncSession, None]:
    """
    便捷函数：获取托管会话

    Example:
        async with managed_session() as session:
            result = await session.execute(query)
    """
    manager = get_session_manager()
    async with manager.get_session() as session:
        yield session


@asynccontextmanager
async def independent_transaction(
    parent_session: AsyncSession | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """
    便捷函数：获取独立事务

    Example:
        async with independent_transaction() as session:
            # 独立事务
            await session.execute(update_stmt)
    """
    manager = get_session_manager()
    async with manager.get_independent_transaction(parent_session) as session:
        yield session
