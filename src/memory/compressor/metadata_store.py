"""
块元数据存储

内存中只存储块的元数据，不存储内容
"""

import logging

from .models import ChunkMetadata, ChunkStatus

logger = logging.getLogger(__name__)


class ChunkMetadataStore:
    """
    块元数据存储

    内存中只存储轻量级元数据，内容按需从数据库加载
    """

    def __init__(self):
        # chunk_id -> ChunkMetadata
        self._metadata: dict[str, ChunkMetadata] = {}
        # session_id -> {layer -> [chunk_id]}
        self._index: dict[str, dict[str, list[str]]] = {}

    def register(self, metadata: ChunkMetadata) -> None:
        """注册新块元数据"""
        self._metadata[metadata.chunk_id] = metadata

        # 更新索引
        if metadata.session_id not in self._index:
            self._index[metadata.session_id] = {"L0": [], "L1": [], "L2": [], "L3": []}

        if metadata.layer in self._index[metadata.session_id]:
            self._index[metadata.session_id][metadata.layer].append(metadata.chunk_id)
            # 按创建时间排序
            self._index[metadata.session_id][metadata.layer].sort(
                key=lambda cid: self._metadata[cid].created_at
            )

        logger.debug(f"[MetadataStore] 注册块 | chunk_id={metadata.chunk_id}, layer={metadata.layer}")

    def get(self, chunk_id: str) -> ChunkMetadata | None:
        """获取块元数据"""
        return self._metadata.get(chunk_id)

    def update_status(self, chunk_id: str, status: ChunkStatus) -> None:
        """更新块状态"""
        if chunk_id in self._metadata:
            self._metadata[chunk_id].status = status
            logger.debug(f"[MetadataStore] 更新状态 | chunk_id={chunk_id}, status={status.value}")

    def remove(self, chunk_id: str) -> None:
        """移除块元数据"""
        if chunk_id in self._metadata:
            metadata = self._metadata[chunk_id]
            # 从索引中移除
            if metadata.session_id in self._index:
                if metadata.layer in self._index[metadata.session_id]:
                    if chunk_id in self._index[metadata.session_id][metadata.layer]:
                        self._index[metadata.session_id][metadata.layer].remove(chunk_id)
            # 从元数据中移除
            del self._metadata[chunk_id]
            logger.debug(f"[MetadataStore] 移除块 | chunk_id={chunk_id}")

    def get_layer_chunks(
        self,
        session_id: str,
        layer: str,
        status: ChunkStatus | None = None
    ) -> list[ChunkMetadata]:
        """
        获取指定层的所有块（按创建时间排序，旧的在前）

        Args:
            session_id: 会话 ID
            layer: 层级 (L0/L1/L2/L3)
            status: 可选，按状态筛选
        """
        if session_id not in self._index:
            return []

        chunk_ids = self._index[session_id].get(layer, [])
        chunks = [self._metadata[cid] for cid in chunk_ids if cid in self._metadata]

        if status:
            chunks = [c for c in chunks if c.status == status]

        # 按创建时间排序（旧的在前）
        chunks.sort(key=lambda c: c.created_at)
        return chunks

    def get_layer_tokens(self, session_id: str, layer: str) -> int:
        """获取指定层的总 token 数"""
        chunks = self.get_layer_chunks(session_id, layer, status=ChunkStatus.ACTIVE)
        return sum(c.token_count for c in chunks)

    def get_total_tokens(self, session_id: str) -> int:
        """获取总会话的总 token 数"""
        total = 0
        for layer in ["L0", "L1", "L2", "L3"]:
            total += self.get_layer_tokens(session_id, layer)
        return total

    def clear_session(self, session_id: str) -> None:
        """清空会话的所有元数据"""
        if session_id in self._index:
            for layer in ["L0", "L1", "L2", "L3"]:
                for chunk_id in self._index[session_id].get(layer, []):
                    if chunk_id in self._metadata:
                        del self._metadata[chunk_id]
            del self._index[session_id]
            logger.info(f"[MetadataStore] 清空会话 | session_id={session_id}")
