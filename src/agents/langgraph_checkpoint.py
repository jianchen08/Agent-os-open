"""
LangGraph Checkpoint 管理器

管理 LangGraph 的 checkpoint 状态，支持清除特定线程的 checkpoint。
"""

import copy
import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

logger = logging.getLogger(__name__)

# 不可序列化的字段列表（在 checkpoint 时跳过）
NON_SERIALIZABLE_FIELDS = {
    "llm_client",
    "tool_executor",
    "agent_config",
    "thinking_callback",
    "tools",  # tools 列表包含 LangChain 工具对象,也不可序列化
}


class SerializableCheckpointer:
    """
    可序列化的 Checkpointer 包装器

    在保存 checkpoint 时自动过滤掉不可序列化的字段
    """

    def __init__(self, base_checkpointer: Any):
        """
        初始化包装器

        Args:
            base_checkpointer: 基础 checkpointer (如 MemorySaver)
        """
        self._base = base_checkpointer

    def _filter_state(self, state: dict) -> dict:
        """
        过滤掉不可序列化的字段

        Args:
            state: 原始状态

        Returns:
            过滤后的状态
        """
        filtered = {}
        for key, value in state.items():
            if key in NON_SERIALIZABLE_FIELDS:
                # 跳过不可序列化的字段
                continue

            # 跳过 messages 字段，因为消息由 LayeredContextStore 管理
            if key == "messages":
                continue

            # 保留 layered_context_store（它实现了 __getstate__）
            if key == "layered_context_store" and value is not None:
                filtered[key] = value
                continue

            # 深拷贝以避免修改原始状态
            try:
                filtered[key] = copy.deepcopy(value)
            except Exception:
                # 如果深拷贝失败,尝试浅拷贝
                try:
                    filtered[key] = copy.copy(value)
                except Exception:
                    # 如果都失败,跳过该字段
                    logging.warning(f"无法序列化字段 {key},跳过")
                    continue
        return filtered

    async def aput(
        self, config: dict, checkpoint: dict, metadata: dict, new_versions: dict
    ) -> dict:
        """异步保存 checkpoint"""
        # 过滤状态
        filtered_checkpoint = {
            **checkpoint,
            "channel_values": self._filter_state(checkpoint.get("channel_values", {})),
        }
        return await self._base.aput(
            config, filtered_checkpoint, metadata, new_versions
        )

    async def aget(self, config: dict) -> dict | None:
        """异步获取 checkpoint"""
        return await self._base.aget(config)

    async def alist(self, config: dict) -> list:
        """异步列出 checkpoints"""
        return await self._base.alist(config)

    def put(
        self, config: dict, checkpoint: dict, metadata: dict, new_versions: dict
    ) -> dict:
        """同步保存 checkpoint"""
        # 过滤状态
        filtered_checkpoint = {
            **checkpoint,
            "channel_values": self._filter_state(checkpoint.get("channel_values", {})),
        }
        return self._base.put(config, filtered_checkpoint, metadata, new_versions)

    def get(self, config: dict) -> dict | None:
        """同步获取 checkpoint"""
        return self._base.get(config)

    def list(self, config: dict) -> list:
        """同步列出 checkpoints"""
        return self._base.list(config)

    def get_next_version(self, current: int | None, channel: Any) -> int:
        """获取下一个版本号"""
        return self._base.get_next_version(current, channel)

    async def aget_tuple(self, config: dict) -> Any | None:
        """异步获取 checkpoint 元组"""
        if hasattr(self._base, "aget_tuple"):
            return await self._base.aget_tuple(config)
        return None

    def get_tuple(self, config: dict) -> Any | None:
        """同步获取 checkpoint 元组"""
        if hasattr(self._base, "get_tuple"):
            return self._base.get_tuple(config)
        return None

    async def aput_writes(self, config: dict, writes: list, task_id: str) -> None:
        """异步写入 writes"""
        if hasattr(self._base, "aput_writes"):
            await self._base.aput_writes(config, writes, task_id)

    def put_writes(self, config: dict, writes: list, task_id: str) -> None:
        """同步写入 writes"""
        if hasattr(self._base, "put_writes"):
            self._base.put_writes(config, writes, task_id)


class LangGraphCheckpointManager:
    """
    LangGraph Checkpoint 管理器

    负责管理 LangGraph MemorySaver 的 checkpoint 状态，
    支持按线程清除 checkpoint，确保消息删除后 LLM 不会看到已删除的历史。
    """

    def __init__(self):
        """初始化 checkpoint 管理器"""
        # 存储每个线程的 MemorySaver 实例
        self._checkpointers: dict[str, MemorySaver] = {}
        logger.info("LangGraphCheckpointManager 初始化完成")

    def get_checkpointer(self, thread_id: str) -> MemorySaver:
        """
        获取或创建指定线程的 MemorySaver

        Args:
            thread_id: 线程 ID

        Returns:
            MemorySaver 实例
        """
        if thread_id not in self._checkpointers:
            self._checkpointers[thread_id] = MemorySaver()
            logger.debug(f"为线程 {thread_id} 创建新的 MemorySaver")
        return self._checkpointers[thread_id]

    async def clear_thread_checkpoints(self, thread_id: str) -> bool:
        """
        清除指定线程的所有 checkpoint

        当用户删除消息时调用此方法，确保 LangGraph 不会使用已删除的历史消息。

        Args:
            thread_id: 线程 ID

        Returns:
            是否成功清除
        """
        try:
            if thread_id in self._checkpointers:
                checkpointer = self._checkpointers[thread_id]

                # 使用 MemorySaver 的 adelete_thread 方法清除特定线程的 checkpoint
                # 这会清除该线程的所有状态，包括消息历史、工具调用记录等
                try:
                    await checkpointer.adelete_thread(thread_id)
                    logger.info(f"已使用 adelete_thread 清除线程 {thread_id} 的 checkpoint 数据")
                except Exception as e:
                    logger.warning(f"adelete_thread 失败，回退到手动清除: {e}")
                    # 回退方案：手动清除 storage
                    if hasattr(checkpointer, 'storage') and isinstance(checkpointer.storage, dict):
                        # 清除该线程的所有 checkpoint 数据
                        keys_to_remove = [k for k in checkpointer.storage.keys() if thread_id in str(k)]
                        for key in keys_to_remove:
                            del checkpointer.storage[key]
                        logger.info(f"已手动清除线程 {thread_id} 的 {len(keys_to_remove)} 个 checkpoint 数据")

                # 删除该线程的 MemorySaver 实例
                # 新的实例会在下次使用时自动创建
                del self._checkpointers[thread_id]
                logger.info(f"已清除线程 {thread_id} 的 LangGraph checkpoint 实例")
            else:
                logger.debug(f"线程 {thread_id} 没有活跃的 checkpoint 需要清除")
            return True
        except Exception as e:
            logger.error(f"清除线程 {thread_id} 的 checkpoint 失败: {e}")
            return False

    def has_checkpoint(self, thread_id: str) -> bool:
        """
        检查线程是否有 checkpoint

        Args:
            thread_id: 线程 ID

        Returns:
            是否存在 checkpoint
        """
        return thread_id in self._checkpointers

    def get_active_thread_count(self) -> int:
        """
        获取活跃线程数量

        Returns:
            活跃线程数量
        """
        return len(self._checkpointers)

    async def clear_all_checkpoints(self) -> int:
        """
        清除所有线程的 checkpoint

        Returns:
            清除的线程数量
        """
        count = len(self._checkpointers)
        self._checkpointers.clear()
        logger.info(f"已清除所有 {count} 个线程的 LangGraph checkpoint")
        return count


# 全局单例实例
_checkpoint_manager: LangGraphCheckpointManager | None = None


def get_checkpoint_manager() -> LangGraphCheckpointManager:
    """
    获取全局 LangGraph checkpoint 管理器实例

    Returns:
        LangGraphCheckpointManager 实例
    """
    global _checkpoint_manager

    if _checkpoint_manager is None:
        _checkpoint_manager = LangGraphCheckpointManager()

    return _checkpoint_manager


async def cleanup_global_checkpoint_manager():
    """清理全局 checkpoint 管理器"""
    global _checkpoint_manager

    if _checkpoint_manager is not None:
        await _checkpoint_manager.clear_all_checkpoints()
        _checkpoint_manager = None
        logger.info("全局 LangGraph checkpoint 管理器已清理")
