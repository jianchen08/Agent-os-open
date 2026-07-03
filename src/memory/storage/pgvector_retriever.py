"""pgvector 纯向量检索器。

从 PgVectorStore（全文+向量双存）改造为纯向量检索器，
只实现 IRetriever 接口，配合 JsonMemoryStore 做内容补充。

需要 sqlalchemy + psycopg2 + pgvector 扩展。
如果依赖未安装，import 此模块会抛出 ImportError。

暴露接口：
- PgVectorRetriever: pgvector 向量检索器
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from memory.ports import IRetriever
from memory.types import MemoryType, SearchResult

try:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession
except ImportError as exc:
    raise ImportError(
        "PgVectorRetriever 需要 sqlalchemy 和 psycopg2。请安装: pip install sqlalchemy psycopg2-binary"
    ) from exc

logger = logging.getLogger(__name__)


class PgVectorRetriever(IRetriever):
    """pgvector 纯向量检索器。

    只实现 IRetriever 接口，PG 表仅存储 ID + 向量索引，
    全文内容由 content_store（JsonMemoryStore）提供。

    Attributes:
        _session: SQLAlchemy 异步会话
        _content_store: 内容存储（JsonMemoryStore），用于取全文
        _embedding_fn: 异步嵌入函数，文本→向量
    """

    def __init__(
        self,
        session: AsyncSession,
        content_store: Any,
        embedding_fn: Callable[[str], Coroutine[Any, Any, list[float]]],
    ) -> None:
        """初始化 pgvector 向量检索器。

        Args:
            session: SQLAlchemy 异步会话
            content_store: 内容存储实例（JsonMemoryStore），用于根据 ID 取全文
            embedding_fn: 异步嵌入函数，接收文本返回向量
        """
        self._session = session
        self._content_store = content_store
        self._embedding_fn = embedding_fn

    async def ensure_tables(self) -> None:
        """创建 PG 向量索引表（如不存在）。

        创建五张表：
        - episodes_memory: id, user_id, intent_vector
        - semantic_memory: id, user_id, embedding
        - memory_chunks: 压缩块向量索引
        - tags: Tag 向量索引
        - tag_cooccurrences: Tag 共现关系
        """
        async with self._session.begin():
            await self._session.execute(
                text("""
                CREATE TABLE IF NOT EXISTS episodes_memory (
                    id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    intent_vector VECTOR(1536)
                )
            """)
            )

            await self._session.execute(
                text("""
                CREATE TABLE IF NOT EXISTS semantic_memory (
                    id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    embedding VECTOR(1536)
                )
            """)
            )

            await self._session.execute(
                text("""
                CREATE TABLE IF NOT EXISTS memory_chunks (
                    id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(64),
                    session_id VARCHAR(64),
                    layer VARCHAR(10),
                    embedding VECTOR(1536)
                )
            """)
            )

            await self._session.execute(
                text("""
                CREATE TABLE IF NOT EXISTS tags (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) UNIQUE,
                    vector VECTOR(1536),
                    frequency INTEGER DEFAULT 0
                )
            """)
            )

            await self._session.execute(
                text("""
                CREATE TABLE IF NOT EXISTS tag_cooccurrences (
                    tag1_id INTEGER REFERENCES tags(id),
                    tag2_id INTEGER REFERENCES tags(id),
                    cooccurrence_count INTEGER DEFAULT 1,
                    PRIMARY KEY (tag1_id, tag2_id)
                )
            """)
            )

        logger.info("[PgVectorRetriever] 向量索引表已创建")

    async def save_index(
        self,
        entry_id: str,
        embedding: list[float],
        user_id: str,
        memory_type: str = "semantic",
    ) -> str:
        """写入向量索引。

        Args:
            entry_id: 条目 ID
            embedding: 向量嵌入
            user_id: 用户 ID
            memory_type: 记忆类型 ("semantic" 或 "episode")

        Returns:
            写入的条目 ID
        """
        if memory_type == "episode":
            query = text(
                "INSERT INTO episodes_memory (id, user_id, intent_vector) "
                "VALUES (:id, :user_id, :embedding) "
                "ON CONFLICT (id) DO UPDATE SET intent_vector = :embedding, user_id = :user_id"
            )
        else:
            query = text(
                "INSERT INTO semantic_memory (id, user_id, embedding) "
                "VALUES (:id, :user_id, :embedding) "
                "ON CONFLICT (id) DO UPDATE SET embedding = :embedding, user_id = :user_id"
            )

        await self._session.execute(
            query,
            {
                "id": entry_id,
                "user_id": user_id,
                "embedding": str(embedding),
            },
        )
        await self._session.flush()
        return entry_id

    async def delete_index(
        self,
        entry_id: str,
        memory_type: str = "semantic",
    ) -> bool:
        """删除向量索引。

        Args:
            entry_id: 条目 ID
            memory_type: 记忆类型 ("semantic" 或 "episode")

        Returns:
            是否删除成功
        """
        if memory_type == "episode":
            query = text("DELETE FROM episodes_memory WHERE id = :id")
        else:
            query = text("DELETE FROM semantic_memory WHERE id = :id")

        result = await self._session.execute(query, {"id": entry_id})
        await self._session.flush()
        return result.rowcount > 0

    async def retrieve(
        self,
        query: str,
        user_id: str | None = None,
        top_k: int = 5,
        memory_type: str = "semantic",
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """向量检索相关记忆。

        将 query 文本转为向量后在 PG 中做余弦相似度检索，
        取回 ID 列表后通过 content_store 批量读取全文拼成 SearchResult。

        Args:
            query: 查询文本
            user_id: 用户 ID
            top_k: 返回数量
            memory_type: 记忆类型 ("semantic" 或 "episode")
            filters: 额外过滤条件（当前未使用）

        Returns:
            搜索结果列表
        """
        if not query:
            return []

        # 将查询文本转为向量
        try:
            query_vector = await self._embedding_fn(query)
        except Exception as e:
            logger.warning("[PgVectorRetriever] 生成查询向量失败: %s", e)
            return []

        # 根据记忆类型选择表和字段
        if memory_type == "episode":
            table = "episodes_memory"
            vector_col = "intent_vector"
        else:
            table = "semantic_memory"
            vector_col = "embedding"

        # 构造向量检索 SQL（余弦距离）
        where_clause = ""
        params: dict[str, Any] = {
            "embedding": str(query_vector),
            "top_k": top_k,
        }
        if user_id:
            where_clause = "AND user_id = :user_id"
            params["user_id"] = user_id

        sql = text(
            f"SELECT id, 1 - ({vector_col} <=> :embedding) AS score "
            f"FROM {table} "
            f"WHERE {vector_col} IS NOT NULL {where_clause} "
            f"ORDER BY {vector_col} <=> :embedding "
            f"LIMIT :top_k"
        )

        try:
            result = await self._session.execute(sql, params)
            rows = result.fetchall()
        except Exception as e:
            logger.warning("[PgVectorRetriever] 向量检索失败: %s", e)
            return []

        if not rows:
            return []

        # 通过 content_store 批量读取全文
        entry_ids = [str(row.id) for row in rows]
        score_map = {str(row.id): float(row.score) for row in rows}

        results: list[SearchResult] = []
        for entry_id in entry_ids:
            try:
                entry = await self._content_store.load(entry_id, memory_type=memory_type)
                if entry is None:
                    continue

                # 根据类型提取内容
                if memory_type == "episode":
                    content = getattr(entry, "execution_summary", None) or getattr(entry, "intent_text", "")
                    metadata = {"tags": getattr(entry, "tags", [])}
                else:
                    content = getattr(entry, "content", "")
                    metadata = getattr(entry, "extra_data", None)

                results.append(
                    SearchResult(
                        id=entry_id,
                        content=content or "",
                        score=score_map.get(entry_id, 0.0),
                        memory_type=MemoryType.EPISODE if memory_type == "episode" else MemoryType.SEMANTIC,
                        metadata=metadata,
                    )
                )
            except Exception as e:
                logger.warning("[PgVectorRetriever] 读取全文失败 | id=%s | error=%s", entry_id, e)

        return results

    # ============================================
    # 压缩块索引操作
    # ============================================

    async def save_chunk_index(
        self,
        chunk_id: str,
        user_id: str,
        session_id: str,
        layer: str,
        embedding: list[float],
    ) -> str:
        """写入压缩块向量索引。

        Args:
            chunk_id: 压缩块 ID
            user_id: 用户 ID
            session_id: 会话 ID
            layer: 分层标识
            embedding: 向量嵌入

        Returns:
            写入的压缩块 ID
        """
        query = text(
            "INSERT INTO memory_chunks (id, user_id, session_id, layer, embedding) "
            "VALUES (:id, :user_id, :session_id, :layer, :embedding) "
            "ON CONFLICT (id) DO UPDATE SET "
            "embedding = :embedding, user_id = :user_id, "
            "session_id = :session_id, layer = :layer"
        )
        await self._session.execute(
            query,
            {
                "id": chunk_id,
                "user_id": user_id,
                "session_id": session_id,
                "layer": layer,
                "embedding": str(embedding),
            },
        )
        await self._session.flush()
        return chunk_id

    async def delete_chunk_index(self, chunk_id: str) -> bool:
        """删除压缩块向量索引。

        Args:
            chunk_id: 压缩块 ID

        Returns:
            是否删除成功
        """
        query = text("DELETE FROM memory_chunks WHERE id = :id")
        result = await self._session.execute(query, {"id": chunk_id})
        await self._session.flush()
        return result.rowcount > 0

    async def retrieve_chunks(
        self,
        query_vector: list[float],
        user_id: str | None = None,
        session_id: str | None = None,
        layer: str | None = None,
        top_k: int = 5,
    ) -> list[SearchResult]:
        """检索压缩块。

        Args:
            query_vector: 查询向量
            user_id: 用户 ID（可选过滤）
            session_id: 会话 ID（可选过滤）
            layer: 分层标识（可选过滤）
            top_k: 返回数量

        Returns:
            搜索结果列表
        """
        where_clauses = ["embedding IS NOT NULL"]
        params: dict[str, Any] = {
            "embedding": str(query_vector),
            "top_k": top_k,
        }

        if user_id:
            where_clauses.append("user_id = :user_id")
            params["user_id"] = user_id
        if session_id:
            where_clauses.append("session_id = :session_id")
            params["session_id"] = session_id
        if layer:
            where_clauses.append("layer = :layer")
            params["layer"] = layer

        where_str = " AND ".join(where_clauses)

        sql = text(
            f"SELECT id, 1 - (embedding <=> :embedding) AS score "
            f"FROM memory_chunks "
            f"WHERE {where_str} "
            f"ORDER BY embedding <=> :embedding "
            f"LIMIT :top_k"
        )

        try:
            result = await self._session.execute(sql, params)
            rows = result.fetchall()
        except Exception as e:
            logger.warning("[PgVectorRetriever] 压缩块检索失败: %s", e)
            return []

        results: list[SearchResult] = []
        for row in rows:
            results.append(
                SearchResult(
                    id=str(row.id),
                    content="",
                    score=float(row.score),
                    memory_type=MemoryType.SEMANTIC,
                    metadata={"source": "chunk"},
                )
            )

        return results

    # ============================================
    # Tag 索引操作
    # ============================================

    async def save_tag(
        self,
        name: str,
        vector: list[float],
        frequency: int = 1,
    ) -> int:
        """写入或更新 Tag。

        Args:
            name: Tag 名称
            vector: Tag 向量
            frequency: 频率

        Returns:
            Tag ID
        """
        query = text(
            "INSERT INTO tags (name, vector, frequency) "
            "VALUES (:name, :vector, :frequency) "
            "ON CONFLICT (name) DO UPDATE SET "
            "vector = :vector, frequency = :frequency "
            "RETURNING id"
        )
        result = await self._session.execute(
            query,
            {
                "name": name,
                "vector": str(vector) if vector else None,
                "frequency": frequency,
            },
        )
        row = result.fetchone()
        await self._session.flush()
        return int(row.id) if row else 0

    async def delete_tag(self, name: str) -> bool:
        """删除 Tag。

        Args:
            name: Tag 名称

        Returns:
            是否删除成功
        """
        query = text("DELETE FROM tags WHERE name = :name")
        result = await self._session.execute(query, {"name": name})
        await self._session.flush()
        return result.rowcount > 0

    async def find_tags_by_vector(
        self,
        query_vector: list[float],
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """透镜阶段向量检索 Tag。

        Args:
            query_vector: 查询向量
            top_k: 返回数量

        Returns:
            Tag 列表 [{"id": int, "name": str, "score": float}, ...]
        """
        sql = text(
            "SELECT id, name, 1 - (vector <=> :embedding) AS score "
            "FROM tags "
            "WHERE vector IS NOT NULL "
            "ORDER BY vector <=> :embedding "
            "LIMIT :top_k"
        )
        try:
            result = await self._session.execute(
                sql,
                {
                    "embedding": str(query_vector),
                    "top_k": top_k,
                },
            )
            rows = result.fetchall()
        except Exception as e:
            logger.warning("[PgVectorRetriever] Tag 向量检索失败: %s", e)
            return []

        return [{"id": int(row.id), "name": str(row.name), "score": float(row.score)} for row in rows]

    async def update_cooccurrence(self, tag1_id: int, tag2_id: int) -> None:
        """更新 Tag 共现关系（UPSERT）。

        Args:
            tag1_id: Tag 1 ID
            tag2_id: Tag 2 ID
        """
        query = text(
            "INSERT INTO tag_cooccurrences (tag1_id, tag2_id, cooccurrence_count) "
            "VALUES (:tag1_id, :tag2_id, 1) "
            "ON CONFLICT (tag1_id, tag2_id) DO UPDATE SET "
            "cooccurrence_count = tag_cooccurrences.cooccurrence_count + 1"
        )
        await self._session.execute(
            query,
            {
                "tag1_id": tag1_id,
                "tag2_id": tag2_id,
            },
        )
        await self._session.flush()

    async def load_all_tags(self) -> list[dict[str, Any]]:
        """加载所有 Tag。

        Returns:
            Tag 列表 [{"id": int, "name": str, "vector": list, "frequency": int}, ...]
        """
        sql = text("SELECT id, name, vector, frequency FROM tags")
        try:
            result = await self._session.execute(sql)
            rows = result.fetchall()
        except Exception as e:
            logger.warning("[PgVectorRetriever] 加载 Tag 失败: %s", e)
            return []

        tags = []
        for row in rows:
            tag_data: dict[str, Any] = {
                "id": int(row.id),
                "name": str(row.name),
                "frequency": int(row.frequency),
            }
            if row.vector is not None:
                vec_str = str(row.vector)
                try:
                    vec_str = vec_str.strip("[]")
                    tag_data["vector"] = [float(v) for v in vec_str.split(",") if v.strip()]
                except (ValueError, AttributeError):
                    tag_data["vector"] = None
            else:
                tag_data["vector"] = None
            tags.append(tag_data)

        return tags

    async def load_cooccurrences(self) -> list[tuple[int, int, int]]:
        """加载所有共现关系。

        Returns:
            共现关系列表 [(tag1_id, tag2_id, count), ...]
        """
        sql = text("SELECT tag1_id, tag2_id, cooccurrence_count FROM tag_cooccurrences")
        try:
            result = await self._session.execute(sql)
            rows = result.fetchall()
        except Exception as e:
            logger.warning("[PgVectorRetriever] 加载共现关系失败: %s", e)
            return []

        return [(int(r.tag1_id), int(r.tag2_id), int(r.cooccurrence_count)) for r in rows]
