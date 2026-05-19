"""
统一会话管理模块（非 ORM 存根）

提供降级接口，不再依赖 SQLAlchemy。
"""

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SessionManager:
    """会话管理器（降级存根）"""

    def __init__(self):
        logger.debug("SessionManager 初始化（非 ORM 模式）")

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[Any, None]:
        """获取独立管理的会话（降级：yield None）。"""
        yield None

    @asynccontextmanager
    async def get_nested_session(
        self, parent_session: Any
    ) -> AsyncGenerator[Any, None]:
        """获取嵌套会话（降级：yield parent）。"""
        yield parent_session

    @asynccontextmanager
    async def get_independent_transaction(
        self, parent_session: Any | None = None
    ) -> AsyncGenerator[Any, None]:
        """获取独立事务的会话（降级：yield None）。"""
        yield None

    async def execute_in_transaction(
        self,
        operation: Callable[[Any], Any],
        session: Any | None = None,
    ) -> Any:
        """在事务中执行操作（降级：传 None）。"""
        return await operation(session)


# 全局会话管理器实例
_session_manager_instance: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """获取会话管理器单例。"""
    global _session_manager_instance
    if _session_manager_instance is None:
        _session_manager_instance = SessionManager()
    return _session_manager_instance


def reset_session_manager() -> None:
    """重置会话管理器单例（用于测试）。"""
    global _session_manager_instance
    _session_manager_instance = None


# =============================================================================
# 便捷函数
# =============================================================================


@asynccontextmanager
async def managed_session() -> AsyncGenerator[Any, None]:
    """便捷函数：获取托管会话（降级：yield None）。"""
    yield None


@asynccontextmanager
async def independent_transaction(
    parent_session: Any | None = None,
) -> AsyncGenerator[Any, None]:
    """便捷函数：获取独立事务（降级：yield None）。"""
    yield None
