"""
会话管理器

管理会话级别的资源和生命周期，包括MemoryService实例的创建和缓存
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.connection import get_db_manager
from src.memory.service import MemoryService

logger = logging.getLogger(__name__)


class SessionManager:
    """
    会话管理器 - 管理会话级别的资源

    职责：
    - 管理 MemoryService 实例的创建和缓存
    - 管理会话级别的数据库连接
    - 清理会话资源
    """

    def __init__(self):
        """初始化会话管理器"""
        # MemoryService 实例缓存 {session_id: MemoryService}
        self._memory_services: dict[str, MemoryService] = {}

        # 数据库会话缓存 {session_id: AsyncSession}
        self._db_sessions: dict[str, AsyncSession] = {}

        # 数据库管理器
        self._db_manager = get_db_manager()

        logger.info("SessionManager 初始化完成")

    async def get_memory_service(self, session_id: str) -> MemoryService | None:
        """
        获取或创建指定会话的记忆服务实例

        Args:
            session_id: 会话ID

        Returns:
            MemoryService实例，失败返回None
        """
        try:
            # 检查缓存
            if session_id in self._memory_services:
                return self._memory_services[session_id]

            # 创建新的数据库会话
            db_session = await self._get_db_session(session_id)
            if not db_session:
                return None

            # 创建MemoryService实例
            memory_service = MemoryService(session=db_session)

            # 缓存实例
            self._memory_services[session_id] = memory_service

            logger.info(f"为会话 {session_id} 创建了新的MemoryService实例")
            return memory_service

        except Exception as e:
            logger.error(
                f"创建MemoryService失败 - session_id: {session_id}, error: {e}"
            )
            return None

    async def _get_db_session(self, session_id: str) -> AsyncSession | None:
        """
        获取或创建数据库会话

        Args:
            session_id: 会话ID

        Returns:
            数据库会话实例
        """
        try:
            # 检查缓存
            if session_id in self._db_sessions:
                return self._db_sessions[session_id]

            # 直接创建新的数据库会话
            db_session = self._db_manager.session_factory()
            if not db_session:
                return None

            # 缓存会话
            self._db_sessions[session_id] = db_session

            return db_session

        except Exception as e:
            logger.error(f"创建数据库会话失败 - session_id: {session_id}, error: {e}")
            return None

    async def cleanup_session(self, session_id: str) -> None:
        """
        清理指定会话的资源

        Args:
            session_id: 会话ID
        """
        try:
            # 清理MemoryService
            if session_id in self._memory_services:
                del self._memory_services[session_id]
                logger.debug(f"清理了会话 {session_id} 的MemoryService")

            # 清理数据库会话
            if session_id in self._db_sessions:
                db_session = self._db_sessions[session_id]
                try:
                    await db_session.close()
                except Exception as e:
                    logger.warning(f"关闭数据库会话失败: {e}")

                del self._db_sessions[session_id]
                logger.debug(f"清理了会话 {session_id} 的数据库会话")

            logger.info(f"会话 {session_id} 资源清理完成")

        except Exception as e:
            logger.error(f"清理会话资源失败 - session_id: {session_id}, error: {e}")

    async def cleanup_all(self) -> None:
        """清理所有会话资源"""
        try:
            session_ids = list(self._memory_services.keys())

            for session_id in session_ids:
                await self.cleanup_session(session_id)

            logger.info(f"清理了所有会话资源，共 {len(session_ids)} 个会话")

        except Exception as e:
            logger.error(f"清理所有会话资源失败: {e}")

    def has_session(self, session_id: str) -> bool:
        """
        检查会话是否存在

        Args:
            session_id: 会话ID

        Returns:
            是否存在
        """
        return session_id in self._memory_services

    def get_active_session_count(self) -> int:
        """
        获取活跃会话数量

        Returns:
            活跃会话数量
        """
        return len(self._memory_services)

    def get_session_stats(self) -> dict[str, int]:
        """
        获取会话统计信息

        Returns:
            统计信息字典
        """
        return {
            "active_sessions": len(self._memory_services),
            "db_sessions": len(self._db_sessions),
        }


# 全局单例实例
_session_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """
    获取全局会话管理器实例

    Returns:
        SessionManager实例
    """
    global _session_manager

    if _session_manager is None:
        _session_manager = SessionManager()

    return _session_manager


async def cleanup_global_session_manager():
    """清理全局会话管理器"""
    global _session_manager

    if _session_manager is not None:
        await _session_manager.cleanup_all()
        _session_manager = None
