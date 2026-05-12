"""
基础服务类

提供通用的服务功能和数据库会话管理
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_async_session

logger = logging.getLogger(__name__)


class BaseService:
    """基础服务类"""

    def __init__(self, session: AsyncSession | None = None):
        """
        初始化基础服务

        Args:
            session: 可选的数据库会话，如果不提供则会自动创建
        """
        self.session = session
        self._owns_session = session is None

    async def _get_session(self) -> AsyncSession:
        """
        获取数据库会话

        Returns:
            数据库会话实例

        Raises:
            ValueError: 当没有提供会话且无法自动创建时
        """
        if self.session is not None:
            return self.session

        if self._owns_session:
            # 如果服务拥有会话，创建新的会话
            self.session = await get_async_session().__anext__()
            return self.session
        else:
            # 如果没有提供会话，抛出异常要求外部管理
            raise ValueError(f"{self.__class__.__name__} 需要外部提供数据库会话")

    async def _commit_transaction(self):
        """提交事务"""
        if self.session:
            await self.session.commit()

    async def _rollback_transaction(self):
        """回滚事务"""
        if self.session:
            await self.session.rollback()

    async def close(self):
        """关闭服务和数据库会话"""
        if self.session and self._owns_session:
            await self.session.close()
            self.session = None

    async def __aenter__(self):
        """异步上下文管理器入口"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        if exc_type:
            await self._rollback_transaction()
        else:
            await self._commit_transaction()
        await self.close()
