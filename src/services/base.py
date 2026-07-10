"""
基础服务类

提供通用的服务功能和数据库会话管理（非 ORM 存根）
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class BaseService:
    """基础服务类"""

    def __init__(self, session: Any | None = None):
        """
        初始化基础服务

        Args:
            session: 可选的数据库会话（降级模式，可为 None）
        """
        self.session = session
        self._owns_session = session is None

    async def _get_session(self) -> Any:
        """
        获取数据库会话

        Returns:
            数据库会话实例（降级模式下为 None）
        """
        return self.session

    async def _commit_transaction(self):
        """提交事务（降级：空操作）"""
        pass

    async def _rollback_transaction(self):
        """回滚事务（降级：空操作）"""
        pass

    async def close(self):
        """关闭服务和数据库会话"""
        self.session = None

    async def __aenter__(self):
        """异步上下文管理器入口"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()
