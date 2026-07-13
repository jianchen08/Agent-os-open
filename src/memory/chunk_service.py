"""压缩块服务。

管理压缩块的 JSON + PG 混合持久化。
JSON 文件存储完整内容，PG 存储向量索引用于检索。

暴露接口：
- ChunkService: 压缩块服务
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from memory.types import ChunkData

logger = logging.getLogger(__name__)


class ChunkService:
    """压缩块服务。

    负责压缩块的 CRUD 操作，实现 JSON 文件 + PG 向量索引的混合持久化。

    Attributes:
        _content_store: JSON 内容存储
        _vector_retriever: PG 向量检索器（可选）
        _tag_service: Tag 服务（可选）
        _chunks_dir: 压缩块 JSON 文件目录
        _cache: 内存缓存
    """

    def __init__(
        self,
        content_store: Any = None,
        vector_retriever: Any = None,
        tag_service: Any = None,
        data_dir: str = "data/memory",
    ) -> None:
        """初始化压缩块服务。

        Args:
            content_store: JSON 内容存储实例
            vector_retriever: PG 向量检索器实例（可选）
            tag_service: Tag 服务实例（可选）
            data_dir: 数据存储根目录
        """
        self._content_store = content_store
        self._vector_retriever = vector_retriever
        self._tag_service = tag_service
        self._chunks_dir = Path(data_dir) / "chunks"
        self._chunks_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, ChunkData] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """从磁盘加载已有压缩块。"""
        for f in self._chunks_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                chunk = ChunkData.from_dict(data)
                self._cache[chunk.id] = chunk
            except Exception as e:
                logger.warning("[ChunkService] 加载压缩块失败 | file=%s | error=%s", f, e)
        logger.info("[ChunkService] 加载完成 | chunks=%d", len(self._cache))

    async def save(self, chunk_data: ChunkData) -> str:
        """保存压缩块。

        写入流程：
        1. 写 JSON 文件
        2. 如果有 vector_retriever，写入 PG memory_chunks 表
        3. 如果有 tag_service，通过 TagService 创建/更新 Tag

        Args:
            chunk_data: 压缩块数据

        Returns:
            保存的压缩块 ID
        """
        # 1. 写 JSON 文件
        self._cache[chunk_data.id] = chunk_data
        self._save_to_disk(chunk_data)

        # 2. 写 PG 向量索引
        if self._vector_retriever and hasattr(self._vector_retriever, "save_chunk_index"):
            try:
                embedding = await self._get_embedding(chunk_data.content)
                if embedding:
                    await self._vector_retriever.save_chunk_index(
                        chunk_id=chunk_data.id,
                        user_id=chunk_data.user_id,
                        session_id=chunk_data.session_id,
                        layer=chunk_data.layer,
                        embedding=embedding,
                    )
            except Exception as e:
                logger.warning("[ChunkService] 写入压缩块向量索引失败 | id=%s | error=%s", chunk_data.id, e)

        # 3. 关联 Tag
        if self._tag_service and chunk_data.keywords:
            try:
                await self._tag_service.link_to_memory(
                    memory_id=chunk_data.id,
                    memory_type="chunk",
                    keywords=chunk_data.keywords,
                )
            except Exception as e:
                logger.warning("[ChunkService] 关联 Tag 失败 | id=%s | error=%s", chunk_data.id, e)

        logger.info(
            "[ChunkService] 保存压缩块 | id=%s | layer=%s | tokens=%d",
            chunk_data.id,
            chunk_data.layer,
            chunk_data.token_count,
        )
        return chunk_data.id

    async def load(self, chunk_id: str) -> ChunkData | None:
        """加载压缩块。

        Args:
            chunk_id: 压缩块 ID

        Returns:
            压缩块数据，不存在则返回 None
        """
        return self._cache.get(chunk_id)

    async def delete(self, chunk_id: str) -> bool:
        """删除压缩块。

        同时删除 JSON 文件和 PG 向量索引。

        Args:
            chunk_id: 压缩块 ID

        Returns:
            是否删除成功
        """
        if chunk_id not in self._cache:
            return False

        del self._cache[chunk_id]

        # 删 JSON 文件
        file_path = self._chunks_dir / f"{chunk_id}.json"
        if file_path.exists():
            file_path.unlink()

        # 删 PG 向量索引
        if self._vector_retriever and hasattr(self._vector_retriever, "delete_chunk_index"):
            try:
                await self._vector_retriever.delete_chunk_index(chunk_id)
            except Exception as e:
                logger.warning("[ChunkService] 删除压缩块向量索引失败 | id=%s | error=%s", chunk_id, e)

        return True

    async def find_by_pipeline(
        self,
        pipeline_run_id: str,
        layer: str | None = None,
    ) -> list[ChunkData]:
        """按管道运行 ID 查找压缩块。

        cache miss 时从磁盘懒加载该 pipeline 的所有块。

        Args:
            pipeline_run_id: 管道运行 ID
            layer: 分层标识过滤（可选）

        Returns:
            压缩块列表（按创建时间升序）
        """
        results = [chunk for chunk in self._cache.values() if chunk.pipeline_run_id == pipeline_run_id]
        if not results:
            results = self._lazy_load_pipeline(pipeline_run_id)
        if layer:
            results = [c for c in results if c.layer == layer]
        results.sort(key=lambda x: x.created_at)
        return results

    def _lazy_load_pipeline(self, pipeline_run_id: str) -> list[ChunkData]:
        """从磁盘懒加载指定 pipeline 的压缩块到缓存。

        当 find_by_pipeline 在缓存中找不到时调用，
        扫描磁盘 JSON 文件，只加载匹配 pipeline_run_id 的块。

        Args:
            pipeline_run_id: 管道运行 ID

        Returns:
            加载到的压缩块列表
        """
        loaded: list[ChunkData] = []
        for f in self._chunks_dir.glob("*.json"):
            chunk_id = f.stem
            if chunk_id in self._cache:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                chunk = ChunkData.from_dict(data)
                if chunk.pipeline_run_id == pipeline_run_id:
                    self._cache[chunk.id] = chunk
                    loaded.append(chunk)
            except Exception:
                pass
        if loaded:
            logger.info(
                "[ChunkService] 懒加载 | pipeline=%s | loaded=%d",
                pipeline_run_id,
                len(loaded),
            )
        return loaded

    async def evict_pipeline(self, pipeline_run_id: str) -> int:
        """从内存缓存中移除指定管道的所有压缩块。

        磁盘文件和 PG 向量索引不受影响，仅释放内存。
        下次需要时可通过 load() 从磁盘重新加载。

        Args:
            pipeline_run_id: 管道运行 ID

        Returns:
            移除的块数量
        """
        to_remove = [cid for cid, chunk in self._cache.items() if chunk.pipeline_run_id == pipeline_run_id]
        for cid in to_remove:
            del self._cache[cid]
        if to_remove:
            logger.info(
                "[ChunkService] 已释放管道缓存 | pipeline=%s | count=%d",
                pipeline_run_id,
                len(to_remove),
            )
        return len(to_remove)

    async def find_by_user(self, user_id: str, limit: int = 20) -> list[ChunkData]:
        """按用户 ID 查找压缩块。

        Args:
            user_id: 用户 ID
            limit: 返回数量上限

        Returns:
            压缩块列表
        """
        results = [chunk for chunk in self._cache.values() if chunk.user_id == user_id]
        results.sort(key=lambda x: x.created_at, reverse=True)
        return results[:limit]

    def _save_to_disk(self, chunk_data: ChunkData) -> None:
        """将压缩块保存到磁盘。

        Args:
            chunk_data: 压缩块数据
        """
        file_path = self._chunks_dir / f"{chunk_data.id}.json"
        try:
            file_path.write_text(
                json.dumps(chunk_data.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("[ChunkService] 保存压缩块失败 | id=%s | error=%s", chunk_data.id, e)

    async def _get_embedding(self, text: str) -> list[float] | None:
        """获取文本的嵌入向量。

        Args:
            text: 文本内容

        Returns:
            嵌入向量
        """
        if not text:
            return None

        if self._vector_retriever and hasattr(self._vector_retriever, "_embedding_fn"):
            try:
                return await self._vector_retriever._embedding_fn(text)
            except Exception as e:
                logger.warning("[ChunkService] 生成嵌入向量失败: %s", e)

        return None
