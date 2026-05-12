"""
简化的版本管理器

统一处理消息版本控制，简化版本管理逻辑
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MessageVersionManager:
    """
    消息版本管理器

    简化版本控制逻辑，提供统一的版本管理接口
    """

    def __init__(self):
        """初始化版本管理器"""
        self._current_versions: dict[str, str] = {}  # thread_id -> current_message_id

    def set_current_message(self, thread_id: str, message_id: str) -> None:
        """
        设置当前消息版本

        Args:
            thread_id: 线程ID
            message_id: 消息ID
        """
        self._current_versions[thread_id] = message_id
        logger.debug(
            "设置当前消息版本 | thread_id=%s | message_id=%s", thread_id, message_id
        )

    def get_current_message(self, thread_id: str) -> str | None:
        """
        获取当前消息版本

        Args:
            thread_id: 线程ID

        Returns:
            当前消息ID，如果不存在则返回None
        """
        return self._current_versions.get(thread_id)

    def is_current_message(self, thread_id: str, message_id: str) -> bool:
        """
        检查是否为当前消息版本

        Args:
            thread_id: 线程ID
            message_id: 消息ID

        Returns:
            是否为当前版本
        """
        current = self.get_current_message(thread_id)
        return current == message_id if current else True  # 如果没有设置，默认为当前

    def clear_thread_versions(self, thread_id: str) -> None:
        """
        清除线程的版本信息

        Args:
            thread_id: 线程ID
        """
        if thread_id in self._current_versions:
            del self._current_versions[thread_id]
            logger.debug("清除线程版本信息 | thread_id=%s", thread_id)

    def get_stats(self) -> dict[str, Any]:
        """
        获取版本管理统计信息

        Returns:
            统计信息
        """
        return {
            "active_threads": len(self._current_versions),
            "current_versions": self._current_versions.copy(),
        }


# 全局版本管理器实例
_global_version_manager = MessageVersionManager()


def get_version_manager() -> MessageVersionManager:
    """获取全局版本管理器实例"""
    return _global_version_manager
