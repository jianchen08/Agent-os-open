"""
使用 pgvector 的优化检索器

提供基于 HNSW 索引的高性能向量检索
"""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.memory.types import MemoryType, RetrievalConfig, SearchResult


class PGVectorRetriever:
    """
    基于 pgvector 的检索器

    使用 PostgreSQL 的 HNSW 索引进行高性能向量检索
    """

    def __init__(
        self,
        session: AsyncSession,
        default_config: RetrievalConfig | None = None,
    ):
        self.session = session
        self.default_config = default_config or RetrievalConfig()

    async def search_episodes(
        self,
        user_id: uuid.UUID,
        query: str,
        query_vector: list[float],
        filters: dict[str, Any] | None = None,
        config: RetrievalConfig | None = None,
    ) -> list[SearchResult]:
        """
        使用 pgvector 搜索情景记忆

        利用 HNSW 索引在数据库层完成向量相似度计算
        """
        cfg = config or self.default_config
        user_id_str = str(user_id)

        # 提取过滤条件
        session_id = filters.get("session_id") if filters else None
        task_id = filters.get("task_id") if filters else None
        min_final_score = filters.get("min_score", 0.5) if filters else 0.5

        # 使用 pgvector 的 <=> 操作符（余弦距离）
        # 1 - <=> 得到余弦相似度
        sql = text("""
            SELECT
                id,
                intent_text,
                session_id,
                task_id,
                tags,
                final_score,
                1 - (intent_vector <=> :query_vector) as similarity
            FROM episodes_memory
            WHERE
                user_id = :user_id
                AND intent_vector IS NOT NULL
                AND final_score >= :min_final_score
                AND 1 - (intent_vector <=> :query_vector) >= :min_similarity
                {session_filter}
                {task_filter}
            ORDER BY intent_vector <=> :query_vector
            LIMIT :limit
        """.format(
            session_filter="AND session_id = :session_id" if session_id else "",
            task_filter="AND task_id = :task_id" if task_id else "",
        ))

        params = {
            'user_id': user_id_str,
            'query_vector': str(query_vector),  # pgvector 接受数组字符串格式
            'min_final_score': min_final_score,
            'min_similarity': cfg.min_score,
            'limit': cfg.top_k,
        }

        if session_id:
            params['session_id'] = session_id
        if task_id:
            params['task_id'] = task_id

        result = await self.session.execute(sql, params)
        rows = result.fetchall()

        return [
            SearchResult(
                id=row.id,
                content=row.intent_text,
                score=float(row.similarity),
                memory_type=MemoryType.EPISODE,
                metadata={
                    "session_id": str(row.session_id) if row.session_id else None,
                    "task_id": str(row.task_id) if row.task_id else None,
                    "tags": row.tags,
                    "final_score": row.final_score,
                },
            )
            for row in rows
        ]

    async def search_knowledge(
        self,
        user_id: uuid.UUID,
        query: str,
        query_vector: list[float],
        domain: str | None = None,
        config: RetrievalConfig | None = None,
    ) -> list[SearchResult]:
        """
        使用 pgvector 搜索语义记忆/知识
        """
        cfg = config or self.default_config
        user_id_str = str(user_id)

        # 构建 SQL
        sql = text("""
            SELECT
                id,
                content,
                source_type,
                extra_data,
                1 - (embedding <=> :query_vector) as similarity
            FROM semantic_memory
            WHERE
                user_id = :user_id
                AND embedding IS NOT NULL
                AND 1 - (embedding <=> :query_vector) >= :min_similarity
                {domain_filter}
            ORDER BY embedding <=> :query_vector
            LIMIT :limit
        """.format(
            domain_filter="AND extra_data->>'domain' = :domain" if domain else "",
        ))

        params = {
            'user_id': user_id_str,
            'query_vector': str(query_vector),
            'min_similarity': cfg.min_score,
            'limit': cfg.top_k,
        }

        if domain:
            params['domain'] = domain

        result = await self.session.execute(sql, params)
        rows = result.fetchall()

        return [
            SearchResult(
                id=row.id,
                content=row.content,
                score=float(row.similarity),
                memory_type=MemoryType.SEMANTIC,
                metadata={
                    "source_type": row.source_type,
                    "extra_data": row.extra_data,
                },
            )
            for row in rows
        ]

    async def search_similar_with_tag_network(
        self,
        user_id: uuid.UUID,
        query_vector: list[float],
        enhanced_vector: list[float],
        config: RetrievalConfig | None = None,
    ) -> list[SearchResult]:
        """
        使用 Tag 网络增强的向量检索

        先用增强向量检索，再合并原始向量检索结果
        """
        cfg = config or self.default_config
        user_id_str = str(user_id)

        # 使用增强向量进行检索
        sql = text("""
            SELECT
                id,
                intent_text,
                session_id,
                task_id,
                tags,
                final_score,
                1 - (intent_vector <=> :enhanced_vector) as similarity
            FROM episodes_memory
            WHERE
                user_id = :user_id
                AND intent_vector IS NOT NULL
                AND 1 - (intent_vector <=> :enhanced_vector) >= :min_similarity
            ORDER BY intent_vector <=> :enhanced_vector
            LIMIT :limit
        """)

        result = await self.session.execute(sql, {
            'user_id': user_id_str,
            'enhanced_vector': str(enhanced_vector),
            'min_similarity': cfg.min_score,
            'limit': cfg.top_k,
        })
        rows = result.fetchall()

        return [
            SearchResult(
                id=row.id,
                content=row.intent_text,
                score=float(row.similarity),
                memory_type=MemoryType.EPISODE,
                metadata={
                    "session_id": str(row.session_id) if row.session_id else None,
                    "task_id": str(row.task_id) if row.task_id else None,
                    "tags": row.tags,
                    "final_score": row.final_score,
                    "enhanced": True,
                },
            )
            for row in rows
        ]
