"""PgVectorStore 向量存储（含自动降级）。

按需启用的可选向量存储能力，作为 IMemoryStore 的增强实现。

核心设计：
- 内部组合 JsonMemoryStore 作为基础存储（始终可用）
- pgvector 仅在 search 时触发，store/delete 不依赖 pgvector
- 支持配置项控制是否启用（默认关闭）
- 当环境中没有 pgvector 扩展时，自动降级到 JsonMemoryStore
- 降级过程有日志提示，方便排查

暴露接口：
- PgVectorConfig: 配置数据类
- PgVectorStore: 向量存储实现
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from memory.ports import IMemoryStore
from memory.storage.json_store import JsonMemoryStore
from memory.types import Episode, Knowledge, SearchResult

logger = logging.getLogger(__name__)


# ============================================================
# 嵌入服务协议
# ============================================================


class EmbeddingService(Protocol):
    """嵌入服务协议，用于生成文本向量。"""

    async def embed_text(self, text: str) -> list[float]: ...


# ============================================================
# 配置
# ============================================================


@dataclass
class PgVectorConfig:
    """PgVector 存储配置。

    Attributes:
        enabled: 是否启用 pgvector（默认关闭）
        connection_string: PostgreSQL 连接字符串
        data_dir: JSON 基础存储的数据目录
        embedding_service: 嵌入服务实例
        vector_dimension: 向量维度（默认 1536，OpenAI text-embedding-ada-002）
    """

    enabled: bool = False
    connection_string: str = ""
    data_dir: str = "data/memory"
    embedding_service: EmbeddingService | None = None
    vector_dimension: int = 1536


# ============================================================
# PgVectorStore 实现
# ============================================================


class PgVectorStore(IMemoryStore):
    """PgVector 向量存储（含自动降级）。

    基于 PostgreSQL + pgvector 扩展的向量存储实现，
    内部组合 JsonMemoryStore 作为基础存储和降级后备。

    store / delete 始终通过 JsonMemoryStore 完成，不依赖 pgvector。
    search 在 pgvector 可用时使用向量检索，不可用时降级到关键词搜索。

    Attributes:
        _config: 配置实例
        _fallback: JsonMemoryStore 基础存储
        _engine: SQLAlchemy 异步引擎（可选）
        _session: SQLAlchemy 异班会话（可选）
        _pg_available: pgvector 是否可用
        _initialized: 是否已尝试初始化
    """

    def __init__(self, config: PgVectorConfig | None = None) -> None:
        """初始化 PgVectorStore。

        Args:
            config: 配置实例，为 None 时使用默认配置（pgvector 关闭）
        """
        self._config = config or PgVectorConfig()
        self._fallback = JsonMemoryStore(data_dir=self._config.data_dir)
        self._engine: Any | None = None
        self._session: Any | None = None
        self._pg_available: bool = False
        self._initialized: bool = False

    @property
    def pg_available(self) -> bool:
        """pgvector 是否可用。"""
        return self._pg_available

    # ============================================
    # 初始化 pgvector 连接（惰性）
    # ============================================

    async def _try_initialize(self) -> None:
        """惰性初始化 pgvector 连接。

        仅在第一次 search 时触发。初始化失败则降级到 JSON，
        后续不再重试。
        """
        if self._initialized:
            return
        self._initialized = True

        if not self._config.enabled:
            logger.info("[PgVectorStore] pgvector 未启用，使用 JSON 存储")
            return

        if not self._config.connection_string:
            logger.info(
                "[PgVectorStore] pgvector 已启用但未配置连接字符串，降级到 JSON 存储"
            )
            return

        try:
            self._engine = await self._create_pg_engine()
            self._pg_available = True
            logger.info("[PgVectorStore] pgvector 初始化成功")
        except Exception as e:
            self._pg_available = False
            logger.warning(
                "[PgVectorStore] pgvector 初始化失败，降级到 JSON 存储: %s", e
            )

    async def _create_pg_engine(self) -> Any:
        """创建 pgvector 异步引擎并验证扩展。

        Returns:
            SQLAlchemy 异步引擎

        Raises:
            Exception: 创建失败时抛出
        """
        try:
            from sqlalchemy.ext.asyncio import (
                AsyncSession,
                async_sessionmaker,
                create_async_engine,
            )
        except ImportError as exc:
            raise ImportError(
                "PgVectorStore 需要 sqlalchemy 和 asyncpg。"
                "请安装: pip install sqlalchemy asyncpg"
            ) from exc

        engine = create_async_engine(self._config.connection_string)

        # 验证 pgvector 扩展是否已安装
        try:
            from sqlalchemy import text

            async with engine.begin() as conn:
                result = await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_extension WHERE extname = 'vector'"
                    )
                )
                row = result.fetchone()
                if row is None or row[0] == 0:
                    raise RuntimeError(
                        "PostgreSQL 中未安装 pgvector 扩展。"
                        "请在数据库中执行: CREATE EXTENSION vector;"
                    )
        except Exception:
            await engine.dispose()
            raise

        # 创建会话工厂
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        self._session = session_factory()

        return engine

    # ============================================
    # IMemoryStore 接口实现
    # ============================================

    async def save(
        self, entry: Episode | Knowledge, memory_type: str = "episode"
    ) -> str:
        """保存记忆条目。

        始终通过 JsonMemoryStore 完成，不依赖 pgvector。
        pgvector 可用时，异步同步到 pgvector（失败不影响主流程）。

        Args:
            entry: 记忆条目（Episode 或 Knowledge）
            memory_type: 记忆类型

        Returns:
            保存的条目 ID
        """
        # 始终先保存到 fallback
        entry_id = await self._fallback.save(entry, memory_type)

        # 可选：同步到 pgvector（失败不影响主流程）
        if self._pg_available and self._session is not None:
            try:
                await self._sync_to_pgvector(entry, memory_type)
            except Exception as e:
                logger.debug(
                    "[PgVectorStore] 同步到 pgvector 失败（不影响主存储）: %s", e
                )

        return entry_id

    async def load(
        self, entry_id: str, memory_type: str = "episode"
    ) -> Episode | Knowledge | None:
        """加载记忆条目。

        始终通过 JsonMemoryStore 完成，不依赖 pgvector。

        Args:
            entry_id: 条目 ID
            memory_type: 记忆类型

        Returns:
            记忆条目，不存在则返回 None
        """
        return await self._fallback.load(entry_id, memory_type)

    async def delete(self, entry_id: str, memory_type: str = "episode") -> bool:
        """删除记忆条目。

        始终通过 JsonMemoryStore 完成，不依赖 pgvector。
        pgvector 可用时，同步从 pgvector 删除（失败不影响主流程）。

        Args:
            entry_id: 条目 ID
            memory_type: 记忆类型

        Returns:
            是否删除成功
        """
        # 始终先从 fallback 删除
        result = await self._fallback.delete(entry_id, memory_type)

        # 可选：从 pgvector 删除（失败不影响主流程）
        if result and self._pg_available and self._session is not None:
            try:
                await self._delete_from_pgvector(entry_id, memory_type)
            except Exception as e:
                logger.debug(
                    "[PgVectorStore] 从 pgvector 删除失败（不影响主存储）: %s", e
                )

        return result

    async def search(
        self,
        query: str,
        user_id: str | None = None,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """搜索记忆。

        pgvector 可用时使用向量检索，不可用时降级到关键词搜索。
        首次调用时触发 pgvector 连接初始化。

        Args:
            query: 搜索查询
            user_id: 用户 ID
            limit: 返回数量上限
            filters: 过滤条件

        Returns:
            搜索结果列表
        """
        # 惰性初始化 pgvector
        await self._try_initialize()

        # pgvector 可用时尝试向量检索
        if self._pg_available and self._session is not None:
            try:
                return await self._vector_search(query, user_id, limit, filters)
            except Exception as e:
                logger.warning(
                    "[PgVectorStore] 向量检索失败，降级到关键词搜索: %s", e
                )

        # 降级到关键词搜索
        return await self._fallback.search(query, user_id, limit, filters)

    # ============================================
    # pgvector 操作（私有方法）
    # ============================================

    async def _sync_to_pgvector(
        self, entry: Episode | Knowledge, memory_type: str
    ) -> None:
        """同步条目到 pgvector。

        Args:
            entry: 记忆条目
            memory_type: 记忆类型
        """
        from sqlalchemy import text

        if isinstance(entry, Episode):
            await self._session.execute(
                text(
                    """INSERT INTO episodes_memory
                    (id, user_id, session_id, intent_text, intent_vector,
                     plan_dag, execution_summary, evaluation_report, final_score, tags)
                    VALUES (:id, :user_id, :session_id, :intent_text, :intent_vector,
                     :plan_dag, :execution_summary, :evaluation_report, :final_score, :tags)
                    ON CONFLICT (id) DO UPDATE SET
                    intent_text = EXCLUDED.intent_text,
                    execution_summary = EXCLUDED.execution_summary
                    """
                ),
                {
                    "id": entry.id,
                    "user_id": entry.user_id,
                    "session_id": entry.session_id,
                    "intent_text": entry.intent_text,
                    "intent_vector": str(entry.intent_vector)
                    if entry.intent_vector
                    else None,
                    "plan_dag": str(entry.plan_dag) if entry.plan_dag else None,
                    "execution_summary": entry.execution_summary,
                    "evaluation_report": str(entry.evaluation_report)
                    if entry.evaluation_report
                    else None,
                    "final_score": entry.final_score,
                    "tags": entry.tags,
                },
            )
            await self._session.flush()
        elif isinstance(entry, Knowledge):
            await self._session.execute(
                text(
                    """INSERT INTO semantic_memory
                    (id, user_id, source_type, source_id, content, embedding, memory_metadata)
                    VALUES (:id, :user_id, :source_type, :source_id, :content, :embedding, :metadata)
                    ON CONFLICT (id) DO UPDATE SET
                    content = EXCLUDED.content,
                    embedding = EXCLUDED.embedding
                    """
                ),
                {
                    "id": entry.id,
                    "user_id": entry.user_id,
                    "source_type": entry.source_type,
                    "source_id": entry.source_id,
                    "content": entry.content,
                    "embedding": str(entry.embedding) if entry.embedding else None,
                    "metadata": str(entry.extra_data) if entry.extra_data else None,
                },
            )
            await self._session.flush()

    async def _delete_from_pgvector(
        self, entry_id: str, memory_type: str
    ) -> None:
        """从 pgvector 删除条目。

        Args:
            entry_id: 条目 ID
            memory_type: 记忆类型
        """
        from sqlalchemy import text

        if memory_type == "episode":
            await self._session.execute(
                text("DELETE FROM episodes_memory WHERE id = :id"),
                {"id": entry_id},
            )
        else:
            await self._session.execute(
                text("DELETE FROM semantic_memory WHERE id = :id"),
                {"id": entry_id},
            )
        await self._session.flush()

    async def _vector_search(
        self,
        query: str,
        user_id: str | None,
        limit: int,
        filters: dict[str, Any] | None,
    ) -> list[SearchResult]:
        """使用 pgvector 进行向量检索。

        Args:
            query: 搜索查询
            user_id: 用户 ID
            limit: 返回数量上限
            filters: 过滤条件

        Returns:
            搜索结果列表
        """

        if not query.strip():
            return await self._fallback.search(query, user_id, limit, filters)

        # 生成查询向量
        embedding_service = self._config.embedding_service
        if embedding_service is None:
            logger.debug(
                "[PgVectorStore] 无嵌入服务，降级到关键词搜索"
            )
            return await self._fallback.search(query, user_id, limit, filters)

        query_vector = await embedding_service.embed_text(query)

        filters = filters or {}
        memory_type = filters.get("memory_type", "all")
        results: list[SearchResult] = []

        # 搜索情景记忆
        if memory_type in ("all", "episode"):
            ep_results = await self._search_episodes(
                query_vector, user_id, limit, filters
            )
            results.extend(ep_results)

        # 搜索知识
        if memory_type in ("all", "semantic"):
            kn_results = await self._search_knowledge(
                query_vector, user_id, limit, filters
            )
            results.extend(kn_results)

        # 按得分排序
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:limit]

    async def _search_episodes(
        self,
        query_vector: list[float],
        user_id: str | None,
        limit: int,
        filters: dict[str, Any],
    ) -> list[SearchResult]:
        """使用 pgvector 搜索情景记忆。

        Args:
            query_vector: 查询向量
            user_id: 用户 ID
            limit: 返回数量上限
            filters: 过滤条件

        Returns:
            搜索结果列表
        """
        from memory.types import MemoryType
        from sqlalchemy import text

        params: dict[str, Any] = {
            "query_vector": str(query_vector),
            "min_similarity": filters.get("min_score", 0.5),
            "limit": limit,
        }

        where_clauses = ["intent_vector IS NOT NULL"]
        if user_id:
            where_clauses.append("user_id = :user_id")
            params["user_id"] = user_id

        where_sql = " AND ".join(where_clauses)

        sql = text(
            f"""SELECT
                id, intent_text, execution_summary,
                1 - (intent_vector <=> :query_vector) as similarity
            FROM episodes_memory
            WHERE {where_sql}
                AND 1 - (intent_vector <=> :query_vector) >= :min_similarity
            ORDER BY intent_vector <=> :query_vector
            LIMIT :limit
            """
        )

        result = await self._session.execute(sql, params)
        rows = result.fetchall()

        return [
            SearchResult(
                id=str(row.id),
                content=row.execution_summary or row.intent_text,
                score=float(row.similarity),
                memory_type=MemoryType.EPISODE,
            )
            for row in rows
        ]

    async def _search_knowledge(
        self,
        query_vector: list[float],
        user_id: str | None,
        limit: int,
        filters: dict[str, Any],
    ) -> list[SearchResult]:
        """使用 pgvector 搜索知识。

        Args:
            query_vector: 查询向量
            user_id: 用户 ID
            limit: 返回数量上限
            filters: 过滤条件

        Returns:
            搜索结果列表
        """
        from memory.types import MemoryType
        from sqlalchemy import text

        params: dict[str, Any] = {
            "query_vector": str(query_vector),
            "min_similarity": filters.get("min_score", 0.5),
            "limit": limit,
        }

        where_clauses = ["embedding IS NOT NULL"]
        if user_id:
            where_clauses.append("user_id = :user_id")
            params["user_id"] = user_id

        domain = filters.get("domain")
        if domain:
            where_clauses.append("extra_data->>'domain' = :domain")
            params["domain"] = domain

        where_sql = " AND ".join(where_clauses)

        sql = text(
            f"""SELECT
                id, content, source_type, extra_data,
                1 - (embedding <=> :query_vector) as similarity
            FROM semantic_memory
            WHERE {where_sql}
                AND 1 - (embedding <=> :query_vector) >= :min_similarity
            ORDER BY embedding <=> :query_vector
            LIMIT :limit
            """
        )

        result = await self._session.execute(sql, params)
        rows = result.fetchall()

        return [
            SearchResult(
                id=str(row.id),
                content=row.content,
                score=float(row.similarity),
                memory_type=MemoryType.SEMANTIC,
                metadata={"source_type": row.source_type, "extra_data": row.extra_data},
            )
            for row in rows
        ]
