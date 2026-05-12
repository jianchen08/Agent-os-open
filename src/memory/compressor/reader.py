"""
上下文读取器（读取端）

只负责读取压缩层（L1/L2/L3）和消息层（L0）
不涉及四层架构的完整组装
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.context_repository import ContextRepository

from .config import CompressionConfig
from .db import MemoryChunkDB
from .metadata_store import ChunkMetadataStore
from .models import ChunkMetadata, ChunkStatus

logger = logging.getLogger(__name__)


class ContextReader:
    """
    上下文读取器

    只负责：
    1. 读取压缩层（L1/L2/L3）的内容
    2. 读取消息层（L0）的未压缩消息
    3. 返回原始数据，不做四层组装

    不负责：
    - 写入消息
    - 执行压缩
    - 更新元数据
    - 四层架构组装（由上层处理）
    """

    def __init__(
        self,
        session_id: str,
        config: CompressionConfig,
        metadata_store: ChunkMetadataStore,
        db_session: AsyncSession,
        context_repository: ContextRepository,
        executor_type: str | None = None,
        executor_id: str | None = None,
    ):
        self.session_id = session_id
        self.config = config
        self.metadata_store = metadata_store
        self.db_session = db_session
        self.context_repository = context_repository
        self.executor_type = executor_type
        self.executor_id = executor_id

        self.chunk_db = MemoryChunkDB()

    async def read_compressed_layer(self, layer: str) -> list[str]:
        """
        读取指定压缩层的内容

        Args:
            layer: 层级 (L1/L2/L3)

        Returns:
            内容列表（按时间从新到旧排序）
        """
        if layer not in ["L1", "L2", "L3"]:
            logger.warning(f"[ContextReader] 无效的层: {layer}")
            return []

        # 获取该层的块（按时间排序，旧的在前）
        chunks = self.metadata_store.get_layer_chunks(
            self.session_id, layer, ChunkStatus.ACTIVE
        )

        if not chunks:
            return []

        # 加载每个块的内容
        contents = []
        for chunk in chunks:
            content = await self._load_chunk_content(chunk)
            if content:
                contents.append(content)

        # 返回内容列表（从新到旧，需要反转）
        return list(reversed(contents))

    async def read_message_layer(self) -> list[dict]:
        """
        读取消息层（L0）的未压缩消息

        Returns:
            消息列表（按时间从旧到新排序，符合对话顺序）
        """
        try:
            # 不应用 executor 过滤，获取会话所有消息
            # 过滤逻辑应该在调用方根据需要进行
            messages = await self.context_repository.get_uncompressed_messages(
                session_id=self.session_id,
                executor_type=None,
                executor_id=None
            )
            logger.info(f"[ContextReader] 读取 L0 消息 | session_id={self.session_id} | 数量={len(messages)}")
            # 按时间从旧到新排序（升序，符合对话顺序）
            # context_repository 已经按 ASC 排序返回，这里确保一致性
            messages.sort(key=lambda m: m.get("created_at", ""), reverse=False)
            return messages
        except Exception as e:
            logger.warning(f"[ContextReader] 读取 L0 消息失败: {e}")
            return []

    async def _load_chunk_content(self, chunk: ChunkMetadata) -> str:
        """
        按需加载块内容

        从数据库加载指定块的内容
        """
        try:
            # 使用 load_chunk_by_id 直接加载单个块（更高效）
            chunk_data = await self.chunk_db.load_chunk_by_id(
                session=self.db_session,
                chunk_id=chunk.chunk_id
            )

            if chunk_data:
                return chunk_data.get("content", "")

            logger.warning(f"[ContextReader] 未找到块内容: {chunk.chunk_id}")
            return ""

        except Exception as e:
            logger.warning(f"[ContextReader] 加载块内容失败: {e}")
            return ""

    async def get_recent_messages(self, limit: int | None = None) -> list[dict]:
        """
        获取最近的未压缩消息

        返回按时间从旧到新排序的消息列表（符合对话顺序）。
        如果指定 limit，返回最近的 N 条消息（但仍保持旧到新顺序）。

        Args:
            limit: 可选，限制返回的消息数量

        Returns:
            消息列表（按时间从旧到新排序）
        """
        messages = await self.read_message_layer()
        if limit and limit > 0:
            # 返回最近的 N 条，但保持旧到新顺序
            return messages[-limit:]
        return messages

    async def get_layer_chunks(self, layer: str) -> list[ChunkMetadata]:
        """
        获取指定层的块元数据

        返回按时间排序的块列表（旧的在前）
        """
        if layer not in ["L1", "L2", "L3"]:
            return []
        return self.metadata_store.get_layer_chunks(
            self.session_id, layer, ChunkStatus.ACTIVE
        )
