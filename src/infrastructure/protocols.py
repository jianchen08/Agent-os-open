"""Infrastructure 层协议定义。

定义跨层依赖的抽象接口，避免底层模块直接导入上层模块（如 channels/）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from infrastructure.session.models import SessionModel


@runtime_checkable
class MemoryStoreProtocol(Protocol):
    """会话存储协议。

    定义 task_executor 等底层模块需要的会话存储接口，
    解耦 infrastructure 层对 channels.api.memory_store 的直接依赖。

    实现方：channels.api.memory_store.MemoryStore（通过 services["api_store"] 注入）。
    """

    def get_session(self, thread_id: str) -> SessionModel | None:
        """获取指定线程关联的会话模型。

        Args:
            thread_id: 线程 ID

        Returns:
            关联的 SessionModel，不存在则返回 None
        """
        ...

    def set_session(self, thread_id: str, session: SessionModel) -> None:
        """将 SessionModel 关联到指定线程。

        Args:
            thread_id: 线程 ID
            session: 要关联的会话模型实例
        """
        ...
