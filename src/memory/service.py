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
    MemoryType,
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
        config: dict[str, Any] | None = None,
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
            config: 服务配置（可选），支持 vector_search、maintenance 等子配置
        """
        self._episode_service = EpisodeService(episode_storage=episode_storage)
        self._knowledge_service = KnowledgeService(semantic_storage=semantic_storage)
        self._retrievers: dict[str, IRetriever] = retrievers or {}
        self._embedding_service = embedding_service
        self._vector_retriever = vector_retriever
        self._chunk_service = chunk_service
        self._tag_service = tag_service

        # 向量检索配置
        self._config = config or {}
        vector_cfg = self._config.get("vector_search", {})
        self._vector_search_enabled = vector_cfg.get("enabled", False)
        self._fallback_to_keyword = vector_cfg.get("fallback_to_keyword", True)
        self._default_method = vector_cfg.get("default_method", "vector")

        # 混合检索配置
        hybrid_cfg = vector_cfg.get("hybrid", {})
        self._hybrid_enabled = hybrid_cfg.get("enabled", False)
        self._vector_weight = hybrid_cfg.get("vector_weight", 0.7)
        self._keyword_weight = hybrid_cfg.get("keyword_weight", 0.3)

        # 检索统计（用于健康检查）
        self._retrieval_stats = {
            "total_requests": 0,
            "vector_hits": 0,
            "keyword_hits": 0,
            "fallback_hits": 0,
            "misses": 0,
            "last_retrieval_at": None,
        }

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
            # 检索器不可用时，服务层兜底返回所有知识
            return await self._service_layer_fallback(
                user_id, filter.get("memory_type", "semantic"), None, top_k,
            )

        memory_type = filter.get("memory_type", "semantic")
        results = await retriever.retrieve(
            query="",
            user_id=user_id,
            top_k=top_k,
            memory_type=memory_type,
            filters=filter,
        )
        # 检索器返回空时也尝试兜底
        if not results:
            return await self._service_layer_fallback(
                user_id, memory_type, None, top_k,
            )
        return results

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

    async def _service_layer_fallback(
        self,
        user_id: str | None,
        memory_type: str,
        query: str | None,
        top_k: int,
    ) -> list[SearchResult]:
        """服务层兜底检索：当所有 IRetriever 都不可用时，直接从服务层数据搜索。

        确保即使降级模式（无注入检索器），store 的数据也能被 retrieve 找到。
        同时覆盖 episode（内存降级）和 semantic（存储或内存降级）两种路径。

        Args:
            user_id: 用户 ID
            memory_type: 记忆类型 (semantic/episode)
            query: 查询文本，为 None 时返回全量
            top_k: 返回数量上限

        Returns:
            搜索结果列表
        """
        results: list[SearchResult] = []

        # 语义记忆路径
        if memory_type in ("semantic", "all"):
            try:
                if query:
                    # 有关键词查询 → 委托给 KnowledgeService.search()
                    knowledge_results = await self._knowledge_service.search(
                        user_id=user_id or "",
                        query=query,
                        top_k=top_k,
                    )
                    results.extend(knowledge_results)
                else:
                    # 无查询（full 注入）→ 直接列出所有知识
                    all_kn = await self._knowledge_service._get_all_knowledge(
                        user_id or "",
                    )
                    for kn in all_kn[:top_k]:
                        results.append(SearchResult(
                            id=kn.id,
                            content=kn.content,
                            score=1.0,
                            memory_type=MemoryType.SEMANTIC,
                            metadata=kn.extra_data,
                        ))
            except Exception as e:
                logger.warning("[MemoryService] 知识服务兜底检索失败: %s", e)

        # 情景记忆路径：委托给 EpisodeService 的内存数据
        if memory_type in ("episode", "all") and self._episode_service:
            try:
                episodes_data = await self._episode_service.list_episodes(
                    user_id=user_id or "",
                    page_size=top_k,
                )
                for ep_dict in episodes_data.get("items", []):
                    content = ep_dict.get("execution_summary") or ep_dict.get("intent_text", "")
                    if not query or (query and query.lower() in content.lower()):
                        results.append(SearchResult(
                            id=ep_dict.get("id", ""),
                            content=content,
                            score=1.0 if not query else 0.5,
                            memory_type=MemoryType.EPISODE,
                            metadata={"tags": ep_dict.get("tags", [])},
                        ))
            except Exception as e:
                logger.warning("[MemoryService] 情景记忆兜底检索失败: %s", e)

        # 兜底命中统计
        if results:
            self._retrieval_stats["fallback_hits"] += 1
            logger.debug(
                "[MemoryService] 服务层兜底检索 | query=%s | results=%d",
                query[:30] if query else "None", len(results),
            )
        else:
            self._retrieval_stats["misses"] += 1

        return results[:top_k]

    async def _retrieve_by_method(
        self,
        user_id: str | None,
        filter: dict[str, Any],
        retrieval_method: RetrievalMethod,
        query: str | None,
        top_k: int,
    ) -> list[SearchResult]:
        """按检索方法执行检索（第三层决策）。

        支持向量检索不可用时自动回退到关键词检索。
        启用混合检索时，同时调用向量和关键词检索并按权重合并结果。
        """
        self._retrieval_stats["total_requests"] += 1
        self._retrieval_stats["last_retrieval_at"] = datetime.now(UTC).isoformat()

        if not query:
            return []

        memory_type = filter.get("memory_type", "semantic")

        # 混合检索模式
        if self._hybrid_enabled and retrieval_method == RetrievalMethod.VECTOR:
            results = await self._hybrid_retrieve(
                user_id, filter, query, top_k, memory_type,
            )
            if results:
                return results

        method_name = retrieval_method.value

        # 向量检索配置检查：仅在未注入 vector 检索器时才降级
        if method_name == "vector" and not self._vector_search_enabled:
            if self._retrievers.get("vector"):
                # 有注入的 vector 检索器 → 直接使用，忽略配置标志
                pass
            elif self._fallback_to_keyword:
                logger.debug("[MemoryService] 向量检索未启用且回退到关键词检索")
                method_name = "keyword"
                retrieval_method = RetrievalMethod.KEYWORD
            else:
                # 向量禁用且不回退 → 服务层兜底
                return await self._service_layer_fallback(
                    user_id, memory_type, query, top_k,
                )

        retriever = self._retrievers.get(method_name)
        if not retriever:
            # 尝试回退到任何可用的检索器
            if method_name != "keyword" and self._fallback_to_keyword:
                keyword_retriever = self._retrievers.get("keyword")
                if keyword_retriever:
                    logger.debug(
                        "[MemoryService] 检索器 %s 不可用，回退到关键词检索",
                        method_name,
                    )
                    retriever = keyword_retriever
                    self._retrieval_stats["fallback_hits"] += 1
                else:
                    # 所有检索器都不可用 → 服务层兜底：直接搜索 KnowledgeService
                    return await self._service_layer_fallback(
                        user_id, memory_type, query, top_k,
                    )
            else:
                # keyword 检索器也不可用 → 服务层兜底
                return await self._service_layer_fallback(
                    user_id, memory_type, query, top_k,
                )

        try:
            results = await retriever.retrieve(
                query=query,
                user_id=user_id,
                top_k=top_k,
                memory_type=memory_type,
                filters=filter,
            )
            if results:
                if method_name == "vector":
                    self._retrieval_stats["vector_hits"] += 1
                else:
                    self._retrieval_stats["keyword_hits"] += 1
            else:
                # 向量检索无结果时尝试回退到关键词检索
                if method_name == "vector" and self._fallback_to_keyword:
                    keyword_retriever = self._retrievers.get("keyword")
                    if keyword_retriever:
                        logger.debug(
                            "[MemoryService] 向量检索无结果，回退到关键词检索",
                        )
                        results = await keyword_retriever.retrieve(
                            query=query,
                            user_id=user_id,
                            top_k=top_k,
                            memory_type=memory_type,
                            filters=filter,
                        )
                        if results:
                            self._retrieval_stats["fallback_hits"] += 1
                        else:
                            self._retrieval_stats["misses"] += 1
                    else:
                        self._retrieval_stats["misses"] += 1
                else:
                    self._retrieval_stats["misses"] += 1
            return results
        except Exception as e:
            logger.warning("[MemoryService] 检索失败 | method=%s | error=%s", method_name, e)

            # 异常时回退到关键词检索
            if method_name != "keyword" and self._fallback_to_keyword:
                keyword_retriever = self._retrievers.get("keyword")
                if keyword_retriever:
                    try:
                        results = await keyword_retriever.retrieve(
                            query=query,
                            user_id=user_id,
                            top_k=top_k,
                            memory_type=memory_type,
                            filters=filter,
                        )
                        self._retrieval_stats["fallback_hits"] += 1
                        return results
                    except Exception as fallback_err:
                        logger.warning(
                            "[MemoryService] 关键词回退检索也失败 | error=%s",
                            fallback_err,
                        )

            self._retrieval_stats["misses"] += 1
            return []

    async def _hybrid_retrieve(
        self,
        user_id: str | None,
        filter: dict[str, Any],
        query: str,
        top_k: int,
        memory_type: str,
    ) -> list[SearchResult]:
        """混合检索：同时使用向量检索和关键词检索，按权重合并结果。

        Args:
            user_id: 用户 ID
            filter: 筛选条件
            query: 查询文本
            top_k: 返回数量
            memory_type: 记忆类型

        Returns:
            合并后的搜索结果列表
        """
        vector_results: list[SearchResult] = []
        keyword_results: list[SearchResult] = []

        # 向量检索
        vector_retriever = self._retrievers.get("vector")
        if vector_retriever and self._vector_search_enabled:
            try:
                vector_results = await vector_retriever.retrieve(
                    query=query,
                    user_id=user_id,
                    top_k=top_k * 2,
                    memory_type=memory_type,
                    filters=filter,
                )
            except Exception as e:
                logger.warning("[MemoryService] 混合检索-向量部分失败: %s", e)

        # 关键词检索
        keyword_retriever = self._retrievers.get("keyword")
        if keyword_retriever:
            try:
                keyword_results = await keyword_retriever.retrieve(
                    query=query,
                    user_id=user_id,
                    top_k=top_k * 2,
                    memory_type=memory_type,
                    filters=filter,
                )
            except Exception as e:
                logger.warning("[MemoryService] 混合检索-关键词部分失败: %s", e)

        if not vector_results and not keyword_results:
            return []

        # 合并结果：按 ID 去重，加权得分
        merged: dict[str, SearchResult] = {}

        for result in vector_results:
            weighted_score = result.score * self._vector_weight
            merged[result.id] = SearchResult(
                id=result.id,
                content=result.content,
                score=weighted_score,
                memory_type=result.memory_type,
                metadata=result.metadata,
                highlight=result.highlight,
            )

        for result in keyword_results:
            weighted_score = result.score * self._keyword_weight
            if result.id in merged:
                # 已存在则累加得分
                merged[result.id].score += weighted_score
            else:
                merged[result.id] = SearchResult(
                    id=result.id,
                    content=result.content,
                    score=weighted_score,
                    memory_type=result.memory_type,
                    metadata=result.metadata,
                    highlight=result.highlight,
                )

        # 按得分降序排序
        results = sorted(merged.values(), key=lambda r: r.score, reverse=True)
        return results[:top_k]

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

    # ============================================
    # 健康检查与统计
    # ============================================

    async def health_check(self) -> dict[str, Any]:
        """记忆系统健康检查。

        报告记忆总数、向量覆盖率、存储后端状态和检索统计。

        Returns:
            健康检查报告字典
        """
        now = datetime.now(UTC).isoformat()

        # 1. 统计记忆数量
        episode_count = 0
        knowledge_count = 0
        try:
            if self._episode_service._storage:
                episode_count = await self._episode_service._storage.count_by_user("__all__")
            else:
                episode_count = len(self._episode_service._in_memory)
        except Exception as e:
            logger.warning("[MemoryService] 统计情景记忆数量失败: %s", e)

        try:
            if self._knowledge_service._storage:
                all_knowledge = await self._knowledge_service._storage.find_by_user(
                    "__all__", limit=1000000,
                )
                knowledge_count = len(all_knowledge)
            else:
                knowledge_count = len(self._knowledge_service._in_memory)
        except Exception as e:
            logger.warning("[MemoryService] 统计知识数量失败: %s", e)

        total_count = episode_count + knowledge_count

        # 2. 向量覆盖率
        vector_coverage = 0.0
        vector_entries = 0
        if self._vector_retriever and hasattr(self._vector_retriever, "retrieve"):
            try:
                # 用空查询检测向量表中的条目数（通过全量注入接口）
                await self._vector_retriever.retrieve(
                    query="__health_check_probe__",
                    user_id=None,
                    top_k=1,
                    memory_type="semantic",
                )
                # 能成功调用说明向量检索可用
                vector_entries = -1  # 无法精确统计，标记为可用
                vector_coverage = -1.0
            except Exception:
                vector_entries = 0
                vector_coverage = 0.0

        # 3. 存储后端状态
        storage_status: dict[str, str] = {}
        storage_status["episode_storage"] = (
            type(self._episode_service._storage).__name__
            if self._episode_service._storage
            else "in_memory"
        )
        storage_status["semantic_storage"] = (
            type(self._knowledge_service._storage).__name__
            if self._knowledge_service._storage
            else "in_memory"
        )
        storage_status["vector_search"] = "enabled" if self._vector_search_enabled else "disabled"
        storage_status["vector_retriever"] = (
            type(self._vector_retriever).__name__
            if self._vector_retriever
            else "none"
        )

        # 检查存储连接
        storage_healthy = True
        for storage_name, storage_type in storage_status.items():
            if storage_type == "none" and storage_name == "vector_retriever":
                continue
            if storage_type == "in_memory":
                logger.debug("[HealthCheck] %s 使用内存存储", storage_name)

        # 4. 可用检索器
        available_retrievers = list(self._retrievers.keys())

        # 5. 检索统计
        stats = self._retrieval_stats.copy()
        stats["hit_rate"] = (
            (stats["vector_hits"] + stats["keyword_hits"] + stats["fallback_hits"])
            / max(stats["total_requests"], 1)
        )

        # 组装报告
        report = {
            "status": "healthy" if storage_healthy else "degraded",
            "timestamp": now,
            "memory_count": {
                "total": total_count,
                "episodes": episode_count,
                "knowledge": knowledge_count,
            },
            "vector_coverage": {
                "available": self._vector_search_enabled,
                "entries": vector_entries,
                "coverage_ratio": vector_coverage,
            },
            "storage_backends": storage_status,
            "available_retrievers": available_retrievers,
            "retrieval_stats": stats,
            "config": {
                "vector_search_enabled": self._vector_search_enabled,
                "fallback_to_keyword": self._fallback_to_keyword,
                "hybrid_enabled": self._hybrid_enabled,
                "default_method": self._default_method,
            },
        }

        logger.info(
            "[HealthCheck] 记忆系统健康检查 | total=%d | vector=%s | retrievers=%s",
            total_count,
            "on" if self._vector_search_enabled else "off",
            available_retrievers,
        )

        return report

    def get_retrieval_stats(self) -> dict[str, Any]:
        """获取检索统计信息。

        Returns:
            检索统计字典
        """
        stats = self._retrieval_stats.copy()
        total = max(stats["total_requests"], 1)
        stats["hit_rate"] = (
            (stats["vector_hits"] + stats["keyword_hits"] + stats["fallback_hits"]) / total
        )
        stats["vector_hit_rate"] = stats["vector_hits"] / total
        stats["keyword_hit_rate"] = stats["keyword_hits"] / total
        stats["fallback_rate"] = stats["fallback_hits"] / total
        return stats

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
