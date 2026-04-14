"""记忆服务门面。

从旧代码 src/memory/service.py 搬迁。
移除 SQLAlchemy 和特定 retriever 的硬依赖，
通过注入接口实现三层决策检索模型。

暴露接口：
- MemoryService: 记忆服务门面
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from memory.constants import Retrieval
from memory.episode_service import EpisodeService
from memory.knowledge_service import KnowledgeService
from memory.ports import IEpisodeStorage, IRetriever, ISemanticStorage
from memory.types import (
    ChunkData,
    Episode,
    InjectType,
    Knowledge,
    RetrievalConfig,
    RetrievalMethod,
    SearchResult,
)

logger = logging.getLogger(__name__)


class MemoryService:
    """记忆服务门面。

    统一管理情景记忆、语义记忆和检索操作。

    三层决策模型：
    - 第一层：筛选条件（memory_type, knowledge_id/name, tags, session_id）
    - 第二层：注入方式（full, retrieval, summary）
    - 第三层：检索方法（vector, keyword, tagwave）

    Attributes:
        _episode_service: 情景记忆服务
        _knowledge_service: 知识服务
        _retrievers: 检索器字典（method -> IRetriever）
        _embedding_service: 向量嵌入服务
        _vector_retriever: 向量检索器（用于写入时同步索引）
        _chunk_service: 压缩块服务
        _tag_service: Tag 服务
    """

    def __init__(
        self,
        episode_storage: IEpisodeStorage | None = None,
        semantic_storage: ISemanticStorage | None = None,
        retrievers: dict[str, IRetriever] | None = None,
        embedding_service: Any = None,
        vector_retriever: Any = None,
        chunk_service: Any = None,
        tag_service: Any = None,
    ) -> None:
        """初始化记忆服务。

        Args:
            episode_storage: 情景记忆存储接口
            semantic_storage: 语义记忆存储接口
            retrievers: 检索器字典，key 为检索方法名
            embedding_service: 向量嵌入服务（可选）
            vector_retriever: 向量检索器（可选，写入时同步向量索引）
            chunk_service: 压缩块服务（可选）
            tag_service: Tag 服务（可选）
        """
        self._episode_service = EpisodeService(episode_storage=episode_storage)
        self._knowledge_service = KnowledgeService(semantic_storage=semantic_storage)
        self._retrievers: dict[str, IRetriever] = retrievers or {}
        self._embedding_service = embedding_service
        self._vector_retriever = vector_retriever
        self._chunk_service = chunk_service
        self._tag_service = tag_service

    def register_retriever(self, method: str, retriever: IRetriever) -> None:
        """注册检索器。

        Args:
            method: 检索方法名（vector/keyword/tagwave）
            retriever: 检索器实例
        """
        self._retrievers[method] = retriever

    # ============================================
    # 情景记忆操作 - 委托给 EpisodeService
    # ============================================

    async def store_episode(self, episode: Episode) -> str:
        """存储情景记忆。

        如果存在 vector_retriever 且 episode.intent_vector 存在，
        同步写入向量索引。

        Args:
            episode: 情景记忆实例

        Returns:
            存储的条目 ID
        """
        entry_id = await self._episode_service.store_episode(episode)

        # 同步写向量索引
        if (
            self._vector_retriever
            and episode.intent_vector
            and hasattr(self._vector_retriever, "save_index")
        ):
            try:
                await self._vector_retriever.save_index(
                    entry_id=entry_id,
                    embedding=episode.intent_vector,
                    user_id=episode.user_id,
                    memory_type="episode",
                )
            except Exception as e:
                logger.warning("[MemoryService] 写入情景向量索引失败 | id=%s | error=%s", entry_id, e)

        return entry_id

    async def create_episode(
        self,
        user_id: str,
        intent_text: str,
        plan_dag: dict[str, Any] | None = None,
        execution_summary: str | None = None,
        evaluation_report: dict[str, Any] | None = None,
        final_score: float | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """创建情景记忆。

        Args:
            user_id: 用户 ID
            intent_text: 意图文本
            plan_dag: 执行计划 DAG
            execution_summary: 执行摘要
            evaluation_report: 评估报告
            final_score: 最终得分
            tags: 标签列表

        Returns:
            创建的情景记忆字典
        """
        return await self._episode_service.create_episode(
            user_id=user_id,
            intent_text=intent_text,
            plan_dag=plan_dag,
            execution_summary=execution_summary,
            evaluation_report=evaluation_report,
            final_score=final_score,
            tags=tags,
        )

    async def get_episode(self, episode_id: str, user_id: str) -> dict[str, Any] | None:
        """获取情景记忆。

        Args:
            episode_id: 情景记忆 ID
            user_id: 用户 ID

        Returns:
            情景记忆字典
        """
        return await self._episode_service.get_episode(
            episode_id=episode_id, user_id=user_id,
        )

    async def list_episodes(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """获取情景记忆列表。

        Args:
            user_id: 用户 ID
            page: 页码
            page_size: 每页数量

        Returns:
            分页结果字典
        """
        return await self._episode_service.list_episodes(
            user_id=user_id, page=page, page_size=page_size,
        )

    async def consolidate_episode(self, episode_id: str, summary: str) -> bool:
        """整理情景记忆。

        Args:
            episode_id: 情景记忆 ID
            summary: 执行摘要

        Returns:
            是否更新成功
        """
        return await self._episode_service.consolidate_episode(
            episode_id=episode_id, summary=summary,
        )

    async def delete_episode(self, episode_id: str, user_id: str) -> bool:
        """删除情景记忆。

        如果存在 vector_retriever，同步删除向量索引。

        Args:
            episode_id: 情景记忆 ID
            user_id: 用户 ID

        Returns:
            是否删除成功
        """
        success = await self._episode_service.delete_episode(
            episode_id=episode_id, user_id=user_id,
        )

        # 同步删向量索引
        if success and self._vector_retriever and hasattr(self._vector_retriever, "delete_index"):
            try:
                await self._vector_retriever.delete_index(
                    entry_id=episode_id,
                    memory_type="episode",
                )
            except Exception as e:
                logger.warning("[MemoryService] 删除情景向量索引失败 | id=%s | error=%s", episode_id, e)

        return success

    # ============================================
    # 知识记忆操作 - 委托给 KnowledgeService
    # ============================================

    async def store_knowledge(self, knowledge: Knowledge) -> str:
        """存储知识。

        如果存在 vector_retriever 且 knowledge.embedding 存在，
        同步写入向量索引。

        Args:
            knowledge: 知识实例

        Returns:
            存储的条目 ID
        """
        entry_id = await self._knowledge_service.store_knowledge(knowledge)

        # 同步写向量索引
        if (
            self._vector_retriever
            and knowledge.embedding
            and hasattr(self._vector_retriever, "save_index")
        ):
            try:
                await self._vector_retriever.save_index(
                    entry_id=entry_id,
                    embedding=knowledge.embedding,
                    user_id=knowledge.user_id,
                    memory_type="semantic",
                )
            except Exception as e:
                logger.warning("[MemoryService] 写入知识向量索引失败 | id=%s | error=%s", entry_id, e)

        return entry_id

    async def create_knowledge(
        self,
        user_id: str,
        content: str,
        source_type: str,
        extra_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """创建知识。

        Args:
            user_id: 用户 ID
            content: 知识内容
            source_type: 来源类型
            extra_data: 额外数据

        Returns:
            创建的知识字典
        """
        return await self._knowledge_service.create_knowledge(
            user_id=user_id, content=content,
            source_type=source_type, extra_data=extra_data,
        )

    async def list_semantic_memory(self, user_id: str) -> dict[str, Any]:
        """获取语义记忆列表。

        Args:
            user_id: 用户 ID

        Returns:
            语义记忆列表字典
        """
        return await self._knowledge_service.list_semantic_memory(user_id=user_id)

    async def delete_knowledge(self, knowledge_id: str, user_id: str) -> bool:
        """删除知识。

        如果存在 vector_retriever，同步删除向量索引。

        Args:
            knowledge_id: 知识 ID
            user_id: 用户 ID

        Returns:
            是否删除成功
        """
        success = await self._knowledge_service.delete_knowledge(
            knowledge_id=knowledge_id, user_id=user_id,
        )

        # 同步删向量索引
        if success and self._vector_retriever and hasattr(self._vector_retriever, "delete_index"):
            try:
                await self._vector_retriever.delete_index(
                    entry_id=knowledge_id,
                    memory_type="semantic",
                )
            except Exception as e:
                logger.warning("[MemoryService] 删除知识向量索引失败 | id=%s | error=%s", knowledge_id, e)

        return success

    # ============================================
    # 统一检索接口 - 三层决策模型
    # ============================================

    async def retrieve(
        self,
        user_id: str | None = None,
        filter: dict[str, Any] | None = None,
        inject_type: str = "retrieval",
        retrieval_method: str = "vector",
        query: str | None = None,
        query_vector: list[float] | None = None,
        top_k: int = 5,
    ) -> list[SearchResult]:
        """统一检索入口 - 三层决策模型。

        Args:
            user_id: 用户 ID
            filter: 筛选条件
            inject_type: 注入方式 (full/retrieval/summary)
            retrieval_method: 检索方法 (vector/keyword/tagwave)
            query: 查询文本
            query_vector: 查询向量
            top_k: 返回数量

        Returns:
            搜索结果列表
        """
        filter = filter or {}

        inject_type_enum = InjectType(inject_type)
        retrieval_method_enum = RetrievalMethod(retrieval_method)

        if inject_type_enum == InjectType.FULL:
            return await self._retrieve_full(user_id, filter, top_k)
        elif inject_type_enum == InjectType.SUMMARY:
            return await self._retrieve_summary(user_id, filter, query, top_k)
        else:
            return await self._retrieve_by_method(
                user_id, filter, retrieval_method_enum, query, top_k,
            )

    async def _retrieve_full(
        self,
        user_id: str | None,
        filter: dict[str, Any],
        top_k: int,
    ) -> list[SearchResult]:
        """全量注入 - 直接返回筛选后的所有结果。"""
        retriever = self._retrievers.get("vector")
        if not retriever:
            return []

        memory_type = filter.get("memory_type", "semantic")
        return await retriever.retrieve(
            query="",
            user_id=user_id,
            top_k=top_k,
            memory_type=memory_type,
            filters=filter,
        )

    async def _retrieve_summary(
        self,
        user_id: str | None,
        filter: dict[str, Any],
        query: str | None,
        top_k: int,
    ) -> list[SearchResult]:
        """摘要注入 - 先检索再生成摘要。"""
        results = await self._retrieve_by_method(
            user_id, filter, RetrievalMethod.VECTOR, query, top_k,
        )

        # 摘要生成需要 embedding_service，当前 MVP 直接返回检索结果
        return results

    async def _retrieve_by_method(
        self,
        user_id: str | None,
        filter: dict[str, Any],
        retrieval_method: RetrievalMethod,
        query: str | None,
        top_k: int,
    ) -> list[SearchResult]:
        """按检索方法执行检索（第三层决策）。"""
        method_name = retrieval_method.value
        retriever = self._retrievers.get(method_name)

        if not retriever or not query:
            return []

        memory_type = filter.get("memory_type", "semantic")
        try:
            return await retriever.retrieve(
                query=query,
                user_id=user_id,
                top_k=top_k,
                memory_type=memory_type,
                filters=filter,
            )
        except Exception as e:
            logger.warning("[MemoryService] 检索失败 | method=%s | error=%s", method_name, e)
            return []

    async def search(
        self,
        user_id: str,
        query: str,
        memory_types: list[str] | None = None,
        top_k: int = 10,
        min_score: float | None = None,
    ) -> dict[str, Any]:
        """搜索记忆。

        Args:
            user_id: 用户 ID
            query: 查询文本
            memory_types: 记忆类型列表
            top_k: 返回数量
            min_score: 最小得分阈值

        Returns:
            搜索结果字典
        """
        if min_score is None:
            min_score = Retrieval.MIN_SCORE

        items: list[dict[str, Any]] = []

        if not memory_types or "episode" in memory_types:
            episode_results = await self.retrieve(
                user_id=user_id,
                filter={"memory_type": "episode"},
                query=query,
                top_k=top_k,
            )
            for result in episode_results:
                if result.score >= min_score:
                    items.append(result.to_dict())

        if not memory_types or "semantic" in memory_types:
            knowledge_results = await self.retrieve(
                user_id=user_id,
                filter={"memory_type": "semantic"},
                query=query,
                top_k=top_k,
            )
            for result in knowledge_results:
                if result.score >= min_score:
                    items.append(result.to_dict())

        items.sort(key=lambda x: x.get("score", 0), reverse=True)

        return {"items": items[:top_k], "total": len(items), "query": query}

    async def consolidate(self, user_id: str) -> dict[str, Any]:
        """记忆整合。

        Args:
            user_id: 用户 ID

        Returns:
            整合结果字典
        """
        return {"success": True, "message": "记忆整合完成", "consolidated_count": 0}

    async def get_stats(self, user_id: str) -> dict[str, Any]:
        """获取记忆统计。

        Args:
            user_id: 用户 ID

        Returns:
            统计信息字典
        """
        episode_list = await self._episode_service.list_episodes(user_id, page_size=10000)
        knowledge_count = await self._knowledge_service.get_knowledge_count(user_id)

        episode_count = episode_list.get("total", 0)

        return {
            "episode_count": episode_count,
            "knowledge_count": knowledge_count,
            "total_count": episode_count + knowledge_count,
            "last_updated": datetime.now(UTC).isoformat(),
        }

    async def get_embedding(self, text: str) -> list[float] | None:
        """获取文本的嵌入向量。

        Args:
            text: 文本内容

        Returns:
            嵌入向量，服务不可用时返回 None
        """
        if self._embedding_service:
            if hasattr(self._embedding_service, "embed_text"):
                return await self._embedding_service.embed_text(text)
            elif hasattr(self._embedding_service, "embed"):
                return await self._embedding_service.embed(text)
        return None

    async def store(
        self,
        user_id: str,
        session_id: str,
        category: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """通用存储方法（供 MemoryWritePlugin 调用）。

        Args:
            user_id: 用户 ID
            session_id: 会话 ID
            category: 内容类别
            content: 内容文本
            metadata: 元数据

        Returns:
            存储的条目 ID
        """
        metadata = metadata or {}
        tags = metadata.get("tags", [category])

        episode = Episode(
            user_id=user_id,
            session_id=session_id,
            intent_text=content[:200],
            execution_summary=content,
            tags=tags,
        )
        return await self.store_episode(episode)

    # ============================================
    # 压缩块操作 - 委托给 ChunkService
    # ============================================

    async def store_chunk(self, chunk_data: ChunkData) -> str:
        """存储压缩块。

        Args:
            chunk_data: 压缩块数据

        Returns:
            存储的压缩块 ID
        """
        if self._chunk_service:
            return await self._chunk_service.save(chunk_data)

        logger.warning("[MemoryService] ChunkService 未注入，无法存储压缩块")
        return chunk_data.id

    async def get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        """获取压缩块。

        Args:
            chunk_id: 压缩块 ID

        Returns:
            压缩块字典
        """
        if self._chunk_service:
            chunk = await self._chunk_service.load(chunk_id)
            if chunk:
                return chunk.to_dict()
        return None

    async def delete_chunk(self, chunk_id: str) -> bool:
        """删除压缩块。

        Args:
            chunk_id: 压缩块 ID

        Returns:
            是否删除成功
        """
        if self._chunk_service:
            return await self._chunk_service.delete(chunk_id)

        logger.warning("[MemoryService] ChunkService 未注入，无法删除压缩块")
        return False
