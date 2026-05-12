"""
数据库存储适配器

实现记忆模块的数据库存储，将领域模型转换为数据库模型
"""

import uuid
from typing import Any

from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import EpisodesMemory, SemanticMemory
from src.memory.ports import (
    EpisodeNotFoundError,
    IEpisodeStorage,
    ISemanticStorage,
    KnowledgeNotFoundError,
    StorageConnectionError,
    StorageError,
)
from src.memory.types import Episode, Knowledge, SearchResult


class DBEpisodeStorage(IEpisodeStorage):
    """
    情景记忆数据库存储实现

    使用 SQLAlchemy 异步会话实现存储接口
    """

    def __init__(self, session_factory):
        """
        初始化数据库存储

        Args:
            session_factory: 数据库会话工厂（可以是 AsyncSession 或工厂函数）
        """
        # 支持两种初始化方式：
        # 1. 直接传入 AsyncSession 实例
        # 2. 传入会话工厂函数
        if isinstance(session_factory, AsyncSession):
            self._session = session_factory
            self._session_factory = None
        else:
            self._session = None
            self._session_factory = session_factory

    async def _get_session(self) -> AsyncSession:
        """获取数据库会话"""
        if self._session:
            return self._session
        if self._session_factory:
            if callable(self._session_factory):
                return self._session_factory()
            return self._session_factory
        raise StorageConnectionError("无法获取数据库会话", {"reason": "未配置会话工厂"})

    def _to_domain_model(self, db_episode: EpisodesMemory) -> Episode:
        """
        数据库模型 → 领域模型

        Args:
            db_episode: 数据库情景记忆模型

        Returns:
            领域情景记忆对象
        """
        return Episode(
            id=uuid.UUID(db_episode.id)
            if isinstance(db_episode.id, str)
            else db_episode.id,
            user_id=uuid.UUID(db_episode.user_id)
            if isinstance(db_episode.user_id, str)
            else db_episode.user_id,
            session_id=uuid.UUID(db_episode.session_id)
            if db_episode.session_id
            else None,
            intent_text=db_episode.intent_text,
            intent_vector=db_episode.intent_vector,
            plan_dag=db_episode.plan_dag,
            execution_summary=db_episode.execution_summary,
            evaluation_report=db_episode.evaluation_report,
            final_score=db_episode.final_score,
            tags=db_episode.tags or [],
            created_at=db_episode.created_at,
        )

    def _to_db_model(self, episode: Episode) -> EpisodesMemory:
        """
        领域模型 → 数据库模型

        Args:
            episode: 领域情景记忆对象

        Returns:
            数据库情景记忆模型
        """
        return EpisodesMemory(
            id=str(episode.id),
            user_id=str(episode.user_id),
            session_id=str(episode.session_id) if episode.session_id else None,
            task_id=None,  # 从 episode 的 metadata 中提取
            intent_text=episode.intent_text,
            intent_vector=episode.intent_vector,
            plan_dag=episode.plan_dag,
            execution_summary=episode.execution_summary,
            evaluation_report=episode.evaluation_report,
            final_score=episode.final_score,
            tags=episode.tags,
        )

    async def save(self, episode: Episode) -> str:
        """
        保存情景记忆

        Args:
            episode: 情景记忆对象

        Returns:
            记忆 ID
        """
        try:
            session = await self._get_session()
            db_episode = self._to_db_model(episode)
            session.add(db_episode)
            await session.flush()
            return str(episode.id)
        except Exception as e:
            raise StorageError(
                f"保存情景记忆失败: {str(e)}", {"episode_id": str(episode.id)}
            )

    async def get(self, episode_id: uuid.UUID) -> Episode | None:
        """
        获取情景记忆

        Args:
            episode_id: 情景记忆 ID

        Returns:
            情景记忆对象，不存在时返回 None
        """
        try:
            session = await self._get_session()
            result = await session.execute(
                select(EpisodesMemory).where(EpisodesMemory.id == str(episode_id))
            )
            db_episode = result.scalar_one_or_none()

            if not db_episode:
                return None

            return self._to_domain_model(db_episode)
        except Exception as e:
            raise StorageError(
                f"获取情景记忆失败: {str(e)}", {"episode_id": str(episode_id)}
            )

    async def find_by_user(
        self,
        user_id: uuid.UUID,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Episode]:
        """
        按用户查找情景记忆

        Args:
            user_id: 用户 ID
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            情景记忆列表
        """
        try:
            session = await self._get_session()
            stmt = (
                select(EpisodesMemory)
                .where(EpisodesMemory.user_id == str(user_id))
                .order_by(EpisodesMemory.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            db_episodes = result.scalars().all()

            return [self._to_domain_model(ep) for ep in db_episodes]
        except Exception as e:
            raise StorageError(
                f"查询用户情景记忆失败: {str(e)}", {"user_id": str(user_id)}
            )

    async def search(
        self,
        query: str,
        user_id: uuid.UUID,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """
        搜索情景记忆

        Args:
            query: 搜索查询
            user_id: 用户 ID
            limit: 返回数量限制
            filters: 过滤条件

        Returns:
            搜索结果列表
        """
        try:
            session = await self._get_session()
            stmt = select(EpisodesMemory).where(EpisodesMemory.user_id == str(user_id))

            # 分词搜索
            keywords = query.split()
            if keywords:
                keyword_conditions = []
                for kw in keywords:
                    keyword_conditions.append(
                        EpisodesMemory.intent_text.ilike(f"%{kw}%")
                    )
                    keyword_conditions.append(
                        EpisodesMemory.execution_summary.ilike(f"%{kw}%")
                    )
                stmt = stmt.where(or_(*keyword_conditions))

            # 应用过滤条件
            if filters:
                if "tags" in filters:
                    for tag in filters["tags"]:
                        stmt = stmt.where(
                            cast(EpisodesMemory.tags, String).like(f'%"{tag}"%')
                        )
                if "min_score" in filters:
                    stmt = stmt.where(
                        EpisodesMemory.final_score >= filters["min_score"]
                    )
                if "session_id" in filters and filters["session_id"]:
                    stmt = stmt.where(
                        EpisodesMemory.session_id == filters["session_id"]
                    )

            stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            episodes = result.scalars().all()

            return [
                SearchResult(
                    id=ep.id,
                    content=ep.intent_text,
                    score=ep.final_score or 0.5,
                    memory_type="episode",
                    metadata={
                        "session_id": ep.session_id,
                        "tags": ep.tags,
                        "created_at": ep.created_at.isoformat()
                        if ep.created_at
                        else None,
                    },
                )
                for ep in episodes
            ]
        except Exception as e:
            raise StorageError(f"搜索情景记忆失败: {str(e)}", {"query": query})

    async def update(
        self,
        episode_id: uuid.UUID,
        **kwargs,
    ) -> bool:
        """
        更新情景记忆

        Args:
            episode_id: 情景记忆 ID
            **kwargs: 要更新的字段

        Returns:
            是否成功
        """
        try:
            session = await self._get_session()
            result = await session.execute(
                select(EpisodesMemory).where(EpisodesMemory.id == str(episode_id))
            )
            db_episode = result.scalar_one_or_none()

            if not db_episode:
                raise EpisodeNotFoundError(f"情景记忆不存在: {episode_id}")

            # 更新字段
            for key, value in kwargs.items():
                if hasattr(db_episode, key):
                    setattr(db_episode, key, value)

            await session.flush()
            return True
        except EpisodeNotFoundError:
            raise
        except Exception as e:
            raise StorageError(
                f"更新情景记忆失败: {str(e)}", {"episode_id": str(episode_id)}
            )

    async def delete(self, episode_id: uuid.UUID) -> bool:
        """
        删除情景记忆

        Args:
            episode_id: 情景记忆 ID

        Returns:
            是否成功
        """
        try:
            session = await self._get_session()
            result = await session.execute(
                select(EpisodesMemory).where(EpisodesMemory.id == str(episode_id))
            )
            db_episode = result.scalar_one_or_none()

            if not db_episode:
                return False

            await session.delete(db_episode)
            await session.flush()
            return True
        except Exception as e:
            raise StorageError(
                f"删除情景记忆失败: {str(e)}", {"episode_id": str(episode_id)}
            )

    async def count_by_user(self, user_id: uuid.UUID) -> int:
        """
        统计用户的情景记忆数量

        Args:
            user_id: 用户 ID

        Returns:
            记忆数量
        """
        try:
            session = await self._get_session()
            from sqlalchemy import func

            stmt = select(func.count(EpisodesMemory.id)).where(
                EpisodesMemory.user_id == str(user_id)
            )
            result = await session.execute(stmt)
            return result.scalar() or 0
        except Exception as e:
            raise StorageError(
                f"统计情景记忆数量失败: {str(e)}", {"user_id": str(user_id)}
            )


class DBSemanticStorage(ISemanticStorage):
    """
    语义记忆数据库存储实现

    使用 SQLAlchemy 异步会话实现存储接口
    """

    def __init__(self, session_factory):
        """
        初始化数据库存储

        Args:
            session_factory: 数据库会话工厂
        """
        if isinstance(session_factory, AsyncSession):
            self._session = session_factory
            self._session_factory = None
        else:
            self._session = None
            self._session_factory = session_factory

    async def _get_session(self) -> AsyncSession:
        """获取数据库会话"""
        if self._session:
            return self._session
        if self._session_factory:
            if callable(self._session_factory):
                return self._session_factory()
            return self._session_factory
        raise StorageConnectionError("无法获取数据库会话", {"reason": "未配置会话工厂"})

    def _to_domain_model(self, db_knowledge: SemanticMemory) -> Knowledge:
        """
        数据库模型 → 领域模型

        Args:
            db_knowledge: 数据库语义记忆模型

        Returns:
            领域知识对象
        """
        return Knowledge(
            id=uuid.UUID(db_knowledge.id)
            if isinstance(db_knowledge.id, str)
            else db_knowledge.id,
            user_id=uuid.UUID(db_knowledge.user_id)
            if isinstance(db_knowledge.user_id, str)
            else db_knowledge.user_id,
            source_type=db_knowledge.source_type,
            source_id=uuid.UUID(db_knowledge.source_id)
            if db_knowledge.source_id
            else None,
            content=db_knowledge.content,
            embedding=db_knowledge.embedding,
            extra_data=db_knowledge.memory_metadata,
            created_at=db_knowledge.created_at,
            updated_at=db_knowledge.updated_at,
        )

    def _to_db_model(self, knowledge: Knowledge) -> SemanticMemory:
        """
        领域模型 → 数据库模型

        Args:
            knowledge: 领域知识对象

        Returns:
            数据库语义记忆模型
        """
        return SemanticMemory(
            id=str(knowledge.id),
            user_id=str(knowledge.user_id),
            source_type=knowledge.source_type,
            source_id=str(knowledge.source_id) if knowledge.source_id else None,
            content=knowledge.content,
            embedding=knowledge.embedding,
            memory_metadata=knowledge.extra_data,
        )

    async def save(self, knowledge: Knowledge) -> str:
        """
        保存知识

        Args:
            knowledge: 知识对象

        Returns:
            知识 ID
        """
        try:
            session = await self._get_session()
            db_knowledge = self._to_db_model(knowledge)
            session.add(db_knowledge)
            await session.flush()
            return str(knowledge.id)
        except Exception as e:
            raise StorageError(
                f"保存知识失败: {str(e)}", {"knowledge_id": str(knowledge.id)}
            )

    async def get(self, knowledge_id: uuid.UUID) -> Knowledge | None:
        """
        获取知识

        Args:
            knowledge_id: 知识 ID

        Returns:
            知识对象，不存在时返回 None
        """
        try:
            session = await self._get_session()
            result = await session.execute(
                select(SemanticMemory).where(SemanticMemory.id == str(knowledge_id))
            )
            db_knowledge = result.scalar_one_or_none()

            if not db_knowledge:
                return None

            return self._to_domain_model(db_knowledge)
        except Exception as e:
            raise StorageError(
                f"获取知识失败: {str(e)}", {"knowledge_id": str(knowledge_id)}
            )

    async def find_by_user(
        self,
        user_id: uuid.UUID,
        limit: int = 20,
    ) -> list[Knowledge]:
        """
        按用户查找知识

        Args:
            user_id: 用户 ID
            limit: 返回数量限制

        Returns:
            知识列表
        """
        try:
            session = await self._get_session()
            stmt = (
                select(SemanticMemory)
                .where(SemanticMemory.user_id == str(user_id))
                .order_by(SemanticMemory.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            db_knowledges = result.scalars().all()

            return [self._to_domain_model(k) for k in db_knowledges]
        except Exception as e:
            raise StorageError(f"查询用户知识失败: {str(e)}", {"user_id": str(user_id)})

    async def search(
        self,
        query: str,
        user_id: uuid.UUID,
        limit: int = 10,
        domain: str | None = None,
    ) -> list[SearchResult]:
        """
        搜索知识

        Args:
            query: 搜索查询
            user_id: 用户 ID
            limit: 返回数量限制
            domain: 领域过滤

        Returns:
            搜索结果列表
        """
        try:
            session = await self._get_session()
            stmt = select(SemanticMemory).where(SemanticMemory.user_id == str(user_id))

            # 分词搜索
            keywords = query.split()
            if keywords:
                keyword_conditions = [
                    SemanticMemory.content.ilike(f"%{kw}%") for kw in keywords
                ]
                stmt = stmt.where(or_(*keyword_conditions))

            # 领域过滤
            if domain:
                stmt = stmt.where(
                    cast(SemanticMemory.memory_metadata["domain"], String) == domain
                )

            stmt = stmt.limit(limit)
            result = await session.execute(stmt)
            knowledges = result.scalars().all()

            return [
                SearchResult(
                    id=k.id,
                    content=k.content,
                    score=0.7,  # 关键词匹配默认得分
                    memory_type="semantic",
                    metadata={
                        "source_type": k.source_type,
                        "extra_data": k.memory_metadata,
                        "created_at": k.created_at.isoformat()
                        if k.created_at
                        else None,
                    },
                )
                for k in knowledges
            ]
        except Exception as e:
            raise StorageError(f"搜索知识失败: {str(e)}", {"query": query})

    async def update_embedding(
        self,
        knowledge_id: uuid.UUID,
        embedding: list[float],
    ) -> bool:
        """
        更新知识的向量嵌入

        Args:
            knowledge_id: 知识 ID
            embedding: 向量嵌入

        Returns:
            是否成功
        """
        try:
            session = await self._get_session()
            result = await session.execute(
                select(SemanticMemory).where(SemanticMemory.id == str(knowledge_id))
            )
            db_knowledge = result.scalar_one_or_none()

            if not db_knowledge:
                raise KnowledgeNotFoundError(f"知识不存在: {knowledge_id}")

            db_knowledge.embedding = embedding
            await session.flush()
            return True
        except KnowledgeNotFoundError:
            raise
        except Exception as e:
            raise StorageError(
                f"更新知识向量失败: {str(e)}", {"knowledge_id": str(knowledge_id)}
            )

    async def delete(self, knowledge_id: uuid.UUID) -> bool:
        """
        删除知识

        Args:
            knowledge_id: 知识 ID

        Returns:
            是否成功
        """
        try:
            session = await self._get_session()
            result = await session.execute(
                select(SemanticMemory).where(SemanticMemory.id == str(knowledge_id))
            )
            db_knowledge = result.scalar_one_or_none()

            if not db_knowledge:
                return False

            await session.delete(db_knowledge)
            await session.flush()
            return True
        except Exception as e:
            raise StorageError(
                f"删除知识失败: {str(e)}", {"knowledge_id": str(knowledge_id)}
            )
