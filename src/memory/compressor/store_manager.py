"""
LayeredContextStore 管理器

管理所有 LayeredContextStore 实例，支持按会话清除缓存。
"""

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .store import LayeredContextStore

logger = logging.getLogger(__name__)


class LayeredContextStoreManager:
    """
    LayeredContextStore 管理器

    负责管理所有 LayeredContextStore 实例，支持：
    - 按会话注册/注销实例
    - 按会话清除所有实例的缓存
    - 全局清除所有缓存
    """

    def __init__(self):
        """初始化管理器"""
        # 存储每个会话的 LayeredContextStore 实例 {session_id: [instances]}
        self._stores: dict[str, list[LayeredContextStore]] = {}
        logger.info("LayeredContextStoreManager 初始化完成")

    def register(self, session_id: str, store: "LayeredContextStore") -> None:
        """
        注册 LayeredContextStore 实例

        Args:
            session_id: 会话 ID
            store: LayeredContextStore 实例
        """
        if session_id not in self._stores:
            self._stores[session_id] = []

        if store not in self._stores[session_id]:
            self._stores[session_id].append(store)
            logger.debug(f"注册 LayeredContextStore | session_id={session_id}")

    def unregister(self, session_id: str, store: "LayeredContextStore") -> None:
        """
        注销 LayeredContextStore 实例

        Args:
            session_id: 会话 ID
            store: LayeredContextStore 实例
        """
        if session_id in self._stores and store in self._stores[session_id]:
            self._stores[session_id].remove(store)
            logger.debug(f"注销 LayeredContextStore | session_id={session_id}")

    def get_store(self, session_id: str) -> Optional["LayeredContextStore"]:
        """
        获取指定会话的第一个 LayeredContextStore 实例

        Args:
            session_id: 会话 ID

        Returns:
            LayeredContextStore 实例，如果不存在则返回 None
        """
        if session_id in self._stores and self._stores[session_id]:
            return self._stores[session_id][0]
        return None

    async def clear_session_cache(self, session_id: str) -> int:
        """
        清除指定会话的所有 LayeredContextStore 缓存

        当用户删除消息时调用此方法，确保 LLM 不会收到已删除的消息。

        Args:
            session_id: 会话 ID

        Returns:
            清除的实例数量
        """
        count = 0
        if session_id in self._stores:
            for store in self._stores[session_id]:
                try:
                    # 清除内存中的消息缓存
                    store.clear_messages()
                    count += 1
                    logger.debug(f"清除 LayeredContextStore 缓存 | session_id={session_id}")
                except Exception as e:
                    logger.warning(f"清除 LayeredContextStore 缓存失败 | session_id={session_id} | error={e}")

        logger.info(f"已清除 {count} 个 LayeredContextStore 缓存 | session_id={session_id}")
        return count

    async def clear_all_cache(self) -> int:
        """
        清除所有 LayeredContextStore 缓存

        Returns:
            清除的实例数量
        """
        total_count = 0
        for session_id in list(self._stores.keys()):
            count = await self.clear_session_cache(session_id)
            total_count += count

        logger.info(f"已清除所有 {total_count} 个 LayeredContextStore 缓存")
        return total_count


# 全局单例实例
_store_manager: LayeredContextStoreManager | None = None


def get_layered_context_store_manager() -> LayeredContextStoreManager:
    """
    获取全局 LayeredContextStore 管理器实例

    Returns:
        LayeredContextStoreManager 实例
    """
    global _store_manager

    if _store_manager is None:
        _store_manager = LayeredContextStoreManager()

    return _store_manager


async def cleanup_global_layered_context_store_manager():
    """清理全局 LayeredContextStore 管理器"""
    global _store_manager

    if _store_manager is not None:
        await _store_manager.clear_all_cache()
        _store_manager = None
        logger.info("全局 LayeredContextStore 管理器已清理")
