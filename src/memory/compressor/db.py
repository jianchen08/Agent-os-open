"""
数据库操作模块

包含与数据库相关的操作，特别是 memory_chunks 表的操作
"""

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.tokenizer import get_token_counter


class MemoryChunkDB:
    """
    内存块数据库操作

    负责 memory_chunks 表的增删改查操作
    """

    def __init__(self):
        self.token_counter = get_token_counter()

    async def save_chunk(
        self,
        session: AsyncSession,
        user_id: str,
        session_id: str,
        layer: str,
        content: str,
        embedding: list[float] | None = None,
        message_count: int = 0,
        executor_type: str | None = None,
        executor_id: str | None = None,
        executor_name: str | None = None,
    ) -> str:
        """
        保存分块到数据库

        Args:
            session: 数据库会话
            user_id: 用户 ID
            session_id: 会话 ID
            layer: 层级 (L1/L2/L3)
            content: 压缩后的内容
            embedding: 向量（可选）
            message_count: 原始消息数量
            executor_type: 执行者类型
            executor_id: 执行者 ID
            executor_name: 执行者名称

        Returns:
            创建的 chunk_id
        """
        chunk_id = str(uuid.uuid4())
        token_count = self.token_counter.count_tokens(content)

        # 插入记录（graduated 默认为 FALSE）
        query = text(
            """
            INSERT INTO memory_chunks
            (id, user_id, session_id, executor_type, executor_id, executor_name,
             layer, content, embedding, token_count, message_count, graduated, created_at)
            VALUES (:id, :user_id, :session_id, :executor_type, :executor_id, :executor_name,
                    :layer, :content, :embedding, :token_count, :message_count, FALSE, NOW())
        """
        )

        await session.execute(
            query,
            {
                "id": chunk_id,
                "user_id": user_id,
                "session_id": session_id,
                "executor_type": executor_type,
                "executor_id": executor_id,
                "executor_name": executor_name,
                "layer": layer,
                "content": content,
                "embedding": json.dumps(embedding) if embedding else None,
                "token_count": token_count,
                "message_count": message_count,
            },
        )

        await session.commit()
        return chunk_id

    async def load_chunks_by_session(
        self,
        session: AsyncSession,
        session_id: str,
        executor_id: str | None = None,
    ) -> dict[str, list[str]]:
        """
        加载指定会话的所有压缩块

        Args:
            session: 数据库会话
            session_id: 会话 ID
            executor_id: 执行者 ID（可选）

        Returns:
            按层级组织的内容列表
        """
        # 只加载当前执行者的压缩块（上下文隔离）
        query = text(
            """
            SELECT layer, content, embedding, token_count
            FROM memory_chunks
            WHERE session_id = :session_id
              AND (executor_id = :executor_id OR executor_id IS NULL)
            ORDER BY layer, created_at ASC
        """
        )

        result = await session.execute(
            query, {"session_id": session_id, "executor_id": executor_id}
        )
        rows = result.fetchall()

        # 按层级组织内容
        layer_contents = {"L1": [], "L2": [], "L3": []}
        for row in rows:
            layer = row.layer
            if layer in layer_contents:
                layer_contents[layer].append(row.content)

        return layer_contents

    async def load_ungraduated_l2_chunks(
        self,
        session: AsyncSession,
        session_id: str,
        executor_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        加载未毕业的 L2 chunks

        Args:
            session: 数据库会话
            session_id: 会话 ID
            executor_id: 执行者 ID（可选）

        Returns:
            chunk 列表，每个包含 id, content, embedding
        """
        query = text(
            """
            SELECT id, content, embedding
            FROM memory_chunks
            WHERE session_id = :session_id
              AND layer = 'L2'
              AND graduated = FALSE
              AND (executor_id = :executor_id OR executor_id IS NULL)
            ORDER BY created_at ASC
        """
        )
        result = await session.execute(
            query, {"session_id": session_id, "executor_id": executor_id}
        )
        chunks = result.fetchall()

        return [
            {
                "id": chunk.id,
                "content": chunk.content,
                "embedding": chunk.embedding
            }
            for chunk in chunks
        ]

    async def load_ungraduated_l1_chunks(
        self,
        session: AsyncSession,
        session_id: str,
        executor_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        加载未毕业的 L1 chunks

        Args:
            session: 数据库会话
            session_id: 会话 ID
            executor_id: 执行者 ID（可选）

        Returns:
            chunk 列表，每个包含 id, content, embedding
        """
        query = text(
            """
            SELECT id, content, embedding
            FROM memory_chunks
            WHERE session_id = :session_id
              AND layer = 'L1'
              AND graduated = FALSE
              AND (executor_id = :executor_id OR executor_id IS NULL)
            ORDER BY created_at ASC
        """
        )
        result = await session.execute(
            query, {"session_id": session_id, "executor_id": executor_id}
        )
        chunks = result.fetchall()

        return [
            {
                "id": chunk.id,
                "content": chunk.content,
                "embedding": chunk.embedding
            }
            for chunk in chunks
        ]

    async def mark_chunks_as_graduated(
        self,
        session: AsyncSession,
        chunk_ids: list[str],
        episode_id: str,
    ) -> None:
        """
        标记 chunks 为已毕业

        Args:
            session: 数据库会话
            chunk_ids: 要标记的 chunk ID 列表
            episode_id: 关联的 episode ID
        """
        if not chunk_ids:
            return

        update_query = text(
            """
            UPDATE memory_chunks
            SET graduated = TRUE, episode_id = :episode_id
            WHERE id = ANY(:chunk_ids)
        """
        )

        await session.execute(
            update_query,
            {
                "episode_id": episode_id,
                "chunk_ids": chunk_ids,
            },
        )

        await session.commit()

    async def delete_temporary_chunks(
        self,
        session: AsyncSession,
        session_id: str,
        executor_id: str | None = None,
    ) -> None:
        """
        删除临时数据（未毕业的 L1 和 L3）

        Args:
            session: 数据库会话
            session_id: 会话 ID
            executor_id: 执行者 ID（可选）
        """
        # 删除未毕业的 L1 和 L3（L2 已毕业到 episode）
        # 只删除当前执行者的临时数据
        delete_query = text(
            """
            DELETE FROM memory_chunks
            WHERE session_id = :session_id
              AND graduated = FALSE
              AND layer IN ('L1', 'L3')
              AND (executor_id = :executor_id OR executor_id IS NULL)
        """
        )

        await session.execute(
            delete_query,
            {"session_id": session_id, "executor_id": executor_id},
        )
        await session.commit()

    async def load_embeddings_for_retrieval(
        self,
        session: AsyncSession,
        user_id: str,
        executor_id: str | None = None,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """
        加载用于检索的向量

        Args:
            session: 数据库会话
            user_id: 用户 ID
            executor_id: 执行者 ID（可选）
            limit: 限制数量

        Returns:
            包含 content 和 embedding 的列表
        """
        query_sql = text(
            f"""
            SELECT content, embedding
            FROM memory_chunks
            WHERE user_id = :user_id
              AND embedding IS NOT NULL
              AND (executor_id = :executor_id OR executor_id IS NULL)
            ORDER BY created_at DESC
            LIMIT {limit}
        """
        )
        result = await session.execute(
            query_sql,
            {"user_id": user_id, "executor_id": executor_id},
        )

        items = []
        for row in result.fetchall():
            if row.embedding:
                try:
                    embedding = (
                        json.loads(row.embedding)
                        if isinstance(row.embedding, str)
                        else row.embedding
                    )
                    items.append(
                        {
                            "content": row.content,
                            "embedding": embedding,
                        }
                    )
                except (json.JSONDecodeError, TypeError):
                    # 跳过无效的向量
                    continue

        return items

    async def load_chunks_by_layer_with_tokens(
        self,
        session: AsyncSession,
        session_id: str,
        layer: str,
        executor_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        加载指定层的所有块（按创建时间排序，包含 token 数）

        Args:
            session: 数据库会话
            session_id: 会话 ID
            layer: 层级 (L1/L2/L3)
            executor_id: 执行者 ID（可选）

        Returns:
            chunk 列表，每个包含 id, content, token_count
        """
        query = text(
            """
            SELECT id, content, token_count
            FROM memory_chunks
            WHERE session_id = :session_id
              AND layer = :layer
              AND (executor_id = :executor_id OR executor_id IS NULL)
            ORDER BY created_at ASC
        """
        )

        result = await session.execute(
            query,
            {
                "session_id": session_id,
                "layer": layer,
                "executor_id": executor_id,
            },
        )
        chunks = result.fetchall()

        return [
            {
                "id": chunk.id,
                "content": chunk.content,
                "token_count": chunk.token_count,
            }
            for chunk in chunks
        ]

    async def delete_chunks_by_ids(
        self,
        session: AsyncSession,
        chunk_ids: list[str],
    ) -> None:
        """
        根据 ID 列表删除 chunks

        Args:
            session: 数据库会话
            chunk_ids: 要删除的 chunk ID 列表
        """
        if not chunk_ids:
            return

        delete_query = text(
            """
            DELETE FROM memory_chunks
            WHERE id = ANY(:chunk_ids)
        """
        )

        await session.execute(
            delete_query,
            {"chunk_ids": chunk_ids},
        )
        await session.commit()

    async def load_chunk_by_id(
        self,
        session: AsyncSession,
        chunk_id: str,
    ) -> dict[str, Any] | None:
        """
        根据 ID 加载单个 chunk

        Args:
            session: 数据库会话
            chunk_id: chunk ID

        Returns:
            chunk 数据，包含 id, content, layer 等，如果不存在则返回 None
        """
        query = text(
            """
            SELECT id, layer, content, token_count, message_count, created_at
            FROM memory_chunks
            WHERE id = :chunk_id
        """
        )

        result = await session.execute(query, {"chunk_id": chunk_id})
        row = result.fetchone()

        if not row:
            return None

        return {
            "id": row.id,
            "layer": row.layer,
            "content": row.content,
            "token_count": row.token_count,
            "message_count": row.message_count,
            "created_at": row.created_at,
        }
