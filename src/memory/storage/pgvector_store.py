"""pgvector 存储实现（可选依赖）。

需要 sqlalchemy + psycopg2 + pgvector 扩展。
如果依赖未安装，import 此模块会抛出 ImportError。

暴露接口：
- PgVectorStore: pgvector 记忆存储
"""

from __future__ import annotations

import logging
from typing import Any

from memory.ports import IEpisodeStorage, ISemanticStorage
from memory.types import Episode, Knowledge

try:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession
except ImportError as exc:
    raise ImportError(
        "PgVectorStore 需要 sqlalchemy 和 psycopg2。"
        "请安装: pip install sqlalchemy psycopg2-binary"
    ) from exc

logger = logging.getLogger(__name__)


class PgVectorStore(IEpisodeStorage, ISemanticStorage):
    """pgvector 记忆存储。

    基于 PostgreSQL + pgvector 扩展的记忆存储实现。
    支持向量检索和关键词检索。

    Attributes:
        _session: SQLAlchemy 异步会话
    """

    def __init__(self, session: AsyncSession) -> None:
        """初始化 pgvector 存储。

        Args:
            session: SQLAlchemy 异步会话
        """
        self._session = session

    # ============================================
    # IEpisodeStorage 接口实现
    # ============================================

    async def save(self, episode: Episode) -> str:
        """保存情景记忆。

        Args:
            episode: 情景记忆实例

        Returns:
            条目 ID
        """
        query = text(
            """INSERT INTO episodes_memory
            (id, user_id, session_id, intent_text, intent_vector,
             plan_dag, execution_summary, evaluation_report, final_score, tags)
            VALUES (:id, :user_id, :session_id, :intent_text, :intent_vector,
             :plan_dag, :execution_summary, :evaluation_report, :final_score, :tags)
            """
        )
        await self._session.execute(query, {
            "id": episode.id,
            "user_id": episode.user_id,
            "session_id": episode.session_id,
            "intent_text": episode.intent_text,
            "intent_vector": episode.intent_vector,
            "plan_dag": episode.plan_dag,
            "execution_summary": episode.execution_summary,
            "evaluation_report": episode.evaluation_report,
            "final_score": episode.final_score,
            "tags": episode.tags,
        })
        await self._session.flush()
        return episode.id

    async def get(self, episode_id: str) -> Episode | None:
        """获取情景记忆。

        Args:
            episode_id: 情景记忆 ID

        Returns:
            情景记忆实例
        """
        query = text(
            "SELECT * FROM episodes_memory WHERE id = :id"
        )
        result = await self._session.execute(query, {"id": episode_id})
        row = result.fetchone()
        if not row:
            return None

        return Episode(
            id=str(row.id),
            user_id=str(row.user_id),
            session_id=str(row.session_id) if row.session_id else None,
            intent_text=row.intent_text or "",
            intent_vector=row.intent_vector,
            plan_dag=row.plan_dag,
            execution_summary=row.execution_summary,
            evaluation_report=row.evaluation_report,
            final_score=row.final_score,
            tags=row.tags or [],
        )

    async def find_by_user(
        self, user_id: str, limit: int = 20, offset: int = 0,
    ) -> list[Episode]:
        """按用户查找情景记忆。

        Args:
            user_id: 用户 ID
            limit: 返回数量上限
            offset: 偏移量

        Returns:
            情景记忆列表
        """
        query = text(
            """SELECT * FROM episodes_memory
            WHERE user_id = :user_id
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
            """
        )
        result = await self._session.execute(query, {
            "user_id": user_id, "limit": limit, "offset": offset,
        })
        rows = result.fetchall()

        return [
            Episode(
                id=str(row.id),
                user_id=str(row.user_id),
                session_id=str(row.session_id) if row.session_id else None,
                intent_text=row.intent_text or "",
                execution_summary=row.execution_summary,
                tags=row.tags or [],
            )
            for row in rows
        ]

    async def update(self, episode_id: str, **kwargs: Any) -> bool:
        """更新情景记忆。

        Args:
            episode_id: 情景记忆 ID
            **kwargs: 要更新的字段

        Returns:
            是否更新成功
        """
        if not kwargs:
            return False

        set_clauses = ", ".join(f"{key} = :{key}" for key in kwargs)
        query = text(
            f"UPDATE episodes_memory SET {set_clauses} WHERE id = :id"
        )
        kwargs["id"] = episode_id
        result = await self._session.execute(query, kwargs)
        await self._session.flush()
        return result.rowcount > 0

    async def delete(self, episode_id: str) -> bool:
        """删除情景记忆。

        Args:
            episode_id: 情景记忆 ID

        Returns:
            是否删除成功
        """
        query = text("DELETE FROM episodes_memory WHERE id = :id")
        result = await self._session.execute(query, {"id": episode_id})
        await self._session.flush()
        return result.rowcount > 0

    async def count_by_user(self, user_id: str) -> int:
        """统计用户的情景记忆数量。

        Args:
            user_id: 用户 ID

        Returns:
            记忆数量
        """
        query = text(
            "SELECT COUNT(*) FROM episodes_memory WHERE user_id = :user_id"
        )
        result = await self._session.execute(query, {"user_id": user_id})
        return result.scalar() or 0

    # ============================================
    # ISemanticStorage 接口实现
    # ============================================

    async def save_knowledge(self, knowledge: Knowledge) -> str:
        """保存知识。

        Args:
            knowledge: 知识实例

        Returns:
            条目 ID
        """
        query = text(
            """INSERT INTO semantic_memory
            (id, user_id, source_type, source_id, content, embedding, memory_metadata)
            VALUES (:id, :user_id, :source_type, :source_id, :content, :embedding, :metadata)
            """
        )
        await self._session.execute(query, {
            "id": knowledge.id,
            "user_id": knowledge.user_id,
            "source_type": knowledge.source_type,
            "source_id": knowledge.source_id,
            "content": knowledge.content,
            "embedding": knowledge.embedding,
            "metadata": knowledge.extra_data,
        })
        await self._session.flush()
        return knowledge.id

    async def get_knowledge(self, knowledge_id: str) -> Knowledge | None:
        """获取知识。

        Args:
            knowledge_id: 知识 ID

        Returns:
            知识实例
        """
        query = text("SELECT * FROM semantic_memory WHERE id = :id")
        result = await self._session.execute(query, {"id": knowledge_id})
        row = result.fetchone()
        if not row:
            return None

        return Knowledge(
            id=str(row.id),
            user_id=str(row.user_id),
            source_type=row.source_type or "",
            source_id=row.source_id,
            content=row.content or "",
            embedding=row.embedding,
            extra_data=row.memory_metadata,
        )

    async def find_by_user(self, user_id: str, limit: int = 20) -> list[Knowledge]:  # noqa: F811
        """按用户查找知识。

        Args:
            user_id: 用户 ID
            limit: 返回数量上限

        Returns:
            知识列表
        """
        query = text(
            """SELECT * FROM semantic_memory
            WHERE user_id = :user_id
            ORDER BY created_at DESC
            LIMIT :limit
            """
        )
        result = await self._session.execute(query, {
            "user_id": user_id, "limit": limit,
        })
        rows = result.fetchall()

        return [
            Knowledge(
                id=str(row.id),
                user_id=str(row.user_id),
                source_type=row.source_type or "",
                content=row.content or "",
                extra_data=row.memory_metadata,
            )
            for row in rows
        ]

    async def update_embedding(
        self, knowledge_id: str, embedding: list[float],
    ) -> bool:
        """更新知识的向量嵌入。

        Args:
            knowledge_id: 知识 ID
            embedding: 向量嵌入

        Returns:
            是否更新成功
        """
        query = text(
            "UPDATE semantic_memory SET embedding = :embedding WHERE id = :id"
        )
        result = await self._session.execute(query, {
            "embedding": embedding, "id": knowledge_id,
        })
        await self._session.flush()
        return result.rowcount > 0

    async def delete_knowledge_by_id(self, knowledge_id: str) -> bool:
        """删除知识。

        Args:
            knowledge_id: 知识 ID

        Returns:
            是否删除成功
        """
        query = text("DELETE FROM semantic_memory WHERE id = :id")
        result = await self._session.execute(query, {"id": knowledge_id})
        await self._session.flush()
        return result.rowcount > 0
