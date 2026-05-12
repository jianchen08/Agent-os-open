"""
Tag 关联器模块

提供将 Tag 关联到不同类型记忆（MemoryChunk、Episode、SemanticMemory）的功能。
支持 Tag 的查找或创建，并维护 memory_tags 关联表。
"""

import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.memory import (
    EpisodesMemory,
    MemoryTag,
    SemanticMemory,
    Tag,
)

logger = logging.getLogger(__name__)


class TagLinker:
    """
    Tag 关联器

    负责将关键词（keywords）关联到不同类型的记忆实体：
    - MemoryChunk: 记忆分块
    - EpisodesMemory: 情景记忆
    - SemanticMemory: 语义记忆（知识）

    核心功能：
    1. 查找或创建 Tag（基于关键词）
    2. 插入 memory_tags 关联表
    3. 更新记忆实体的 tags JSON 字段（如需要）
    """

    def __init__(self, session: AsyncSession):
        """
        初始化 TagLinker

        Args:
            session: SQLAlchemy 异步数据库会话
        """
        self.session = session

    async def _get_or_create_tag(self, keyword: str) -> Tag:
        """
        查找或创建 Tag

        根据关键词查找现有 Tag，如果不存在则创建新 Tag。
        Tag 名称统一存储为小写，确保一致性。

        Args:
            keyword: Tag 关键词

        Returns:
            Tag 对象（已存在或新创建）
        """
        # 统一使用小写进行查找和存储
        normalized_keyword = keyword.lower().strip()

        # 查找现有 Tag
        stmt = select(Tag).where(Tag.name == normalized_keyword)
        result = await self.session.execute(stmt)
        tag = result.scalar_one_or_none()

        if tag:
            # 更新频率统计
            tag.frequency += 1
            logger.debug(f"[TagLinker] 找到现有 Tag | name={normalized_keyword} | frequency={tag.frequency}")
            return tag

        # 创建新 Tag
        tag = Tag(
            name=normalized_keyword,
            tag_type="auto",
            frequency=1,
        )
        self.session.add(tag)
        await self.session.flush()  # 确保 tag.id 被生成

        logger.info(f"[TagLinker] 创建新 Tag | name={normalized_keyword} | id={tag.id}")
        return tag

    async def _create_memory_tag_association(
        self, memory_id: str, memory_type: str, tag: Tag, weight: float = 1.0
    ) -> MemoryTag:
        """
        创建 memory_tags 关联记录

        Args:
            memory_id: 记忆实体 ID
            memory_type: 记忆类型（chunk/episode/semantic）
            tag: Tag 对象
            weight: 关联权重，默认 1.0

        Returns:
            MemoryTag 对象
        """
        memory_tag = MemoryTag(
            memory_id=memory_id,
            memory_type=memory_type,
            tag_id=tag.id,
            weight=weight,
        )
        self.session.add(memory_tag)
        logger.debug(
            f"[TagLinker] 创建关联 | memory_id={memory_id} | "
            f"memory_type={memory_type} | tag_id={tag.id} | tag_name={tag.name}"
        )
        return memory_tag

    async def link_chunk_tags(self, chunk_id: str, keywords: list[str]) -> list[MemoryTag]:
        """
        关联 Tag 到 MemoryChunk

        将关键词列表关联到指定的记忆分块。
        仅插入 memory_tags 关联表，不更新 MemoryChunk 的 tags 字段
        （MemoryChunk 没有 tags JSON 字段）。

        Args:
            chunk_id: MemoryChunk ID
            keywords: Tag 关键词列表

        Returns:
            创建的 MemoryTag 关联列表

        Example:
            >>> linker = TagLinker(session)
            >>> associations = await linker.link_chunk_tags(
            ...     chunk_id="uuid-123",
            ...     keywords=["python", "async", "sqlalchemy"]
            ... )
        """
        if not keywords:
            logger.debug(f"[TagLinker] 无关键词需要关联 | chunk_id={chunk_id}")
            return []

        associations: list[MemoryTag] = []

        for keyword in keywords:
            if not keyword or not keyword.strip():
                continue

            # 查找或创建 Tag
            tag = await self._get_or_create_tag(keyword)

            # 创建关联
            association = await self._create_memory_tag_association(
                memory_id=chunk_id,
                memory_type="chunk",
                tag=tag,
            )
            associations.append(association)

        await self.session.flush()

        logger.info(
            f"[TagLinker] MemoryChunk Tag 关联完成 | chunk_id={chunk_id} | "
            f"关联数量={len(associations)}"
        )
        return associations

    async def link_episode_tags(self, episode_id: str, keywords: list[str]) -> list[MemoryTag]:
        """
        关联 Tag 到 Episode

        将关键词列表关联到指定的情景记忆。
        同时执行以下操作：
        1. 插入 memory_tags 关联表
        2. 更新 episodes_memory.tags JSON 字段

        Args:
            episode_id: EpisodesMemory ID
            keywords: Tag 关键词列表

        Returns:
            创建的 MemoryTag 关联列表

        Example:
            >>> linker = TagLinker(session)
            >>> associations = await linker.link_episode_tags(
            ...     episode_id="uuid-456",
            ...     keywords=["任务完成", "成功", "优化"]
            ... )
        """
        if not keywords:
            logger.debug(f"[TagLinker] 无关键词需要关联 | episode_id={episode_id}")
            return []

        associations: list[MemoryTag] = []
        normalized_keywords: list[str] = []

        for keyword in keywords:
            if not keyword or not keyword.strip():
                continue

            normalized = keyword.lower().strip()
            normalized_keywords.append(normalized)

            # 查找或创建 Tag
            tag = await self._get_or_create_tag(keyword)

            # 创建关联
            association = await self._create_memory_tag_association(
                memory_id=episode_id,
                memory_type="episode",
                tag=tag,
            )
            associations.append(association)

        # 更新 episodes_memory.tags JSON 字段
        if normalized_keywords:
            await self._update_episode_tags_field(episode_id, normalized_keywords)

        await self.session.flush()

        logger.info(
            f"[TagLinker] Episode Tag 关联完成 | episode_id={episode_id} | "
            f"关联数量={len(associations)}"
        )
        return associations

    async def _update_episode_tags_field(self, episode_id: str, new_tags: list[str]) -> None:
        """
        更新 EpisodesMemory 的 tags JSON 字段

        将新标签合并到现有标签列表中，避免重复。

        Args:
            episode_id: EpisodesMemory ID
            new_tags: 新标签列表（已规范化）
        """
        # 查询现有标签
        stmt = select(EpisodesMemory.tags).where(EpisodesMemory.id == episode_id)
        result = await self.session.execute(stmt)
        existing_tags = result.scalar_one_or_none() or []

        # 合并标签（去重）
        merged_tags = list(set(existing_tags + new_tags))

        # 更新字段
        stmt = (
            update(EpisodesMemory)
            .where(EpisodesMemory.id == episode_id)
            .values(tags=merged_tags)
        )
        await self.session.execute(stmt)

        logger.debug(
            f"[TagLinker] 更新 Episode tags 字段 | episode_id={episode_id} | "
            f"原有={len(existing_tags)} | 新增={len(new_tags)} | 合并后={len(merged_tags)}"
        )

    async def link_knowledge_tags(self, memory_id: str, keywords: list[str]) -> list[MemoryTag]:
        """
        关联 Tag 到知识（SemanticMemory）

        将关键词列表关联到指定的语义记忆（知识）。
        同时执行以下操作：
        1. 插入 memory_tags 关联表，memory_type="semantic"
        2. 更新 semantic_memory.tags JSON 字段

        Args:
            memory_id: SemanticMemory ID
            keywords: Tag 关键词列表

        Returns:
            创建的 MemoryTag 关联列表

        Example:
            >>> linker = TagLinker(session)
            >>> associations = await linker.link_knowledge_tags(
            ...     memory_id="uuid-789",
            ...     keywords=["API", "文档", "参考"]
            ... )
        """
        if not keywords:
            logger.debug(f"[TagLinker] 无关键词需要关联 | memory_id={memory_id}")
            return []

        associations: list[MemoryTag] = []
        normalized_keywords: list[str] = []

        for keyword in keywords:
            if not keyword or not keyword.strip():
                continue

            normalized = keyword.lower().strip()
            normalized_keywords.append(normalized)

            # 查找或创建 Tag
            tag = await self._get_or_create_tag(keyword)

            # 创建关联，memory_type 使用 "semantic"
            association = await self._create_memory_tag_association(
                memory_id=memory_id,
                memory_type="semantic",
                tag=tag,
            )
            associations.append(association)

        # 更新 semantic_memory.tags JSON 字段
        if normalized_keywords:
            await self._update_semantic_tags_field(memory_id, normalized_keywords)

        await self.session.flush()

        logger.info(
            f"[TagLinker] Knowledge Tag 关联完成 | memory_id={memory_id} | "
            f"关联数量={len(associations)}"
        )
        return associations

    async def _update_semantic_tags_field(self, memory_id: str, new_tags: list[str]) -> None:
        """
        更新 SemanticMemory 的 tags JSON 字段

        将新标签合并到现有标签列表中，避免重复。

        Args:
            memory_id: SemanticMemory ID
            new_tags: 新标签列表（已规范化）
        """
        # 查询现有标签
        stmt = select(SemanticMemory.tags).where(SemanticMemory.id == memory_id)
        result = await self.session.execute(stmt)
        existing_tags = result.scalar_one_or_none() or []

        # 合并标签（去重）
        merged_tags = list(set(existing_tags + new_tags))

        # 更新字段
        stmt = (
            update(SemanticMemory)
            .where(SemanticMemory.id == memory_id)
            .values(tags=merged_tags)
        )
        await self.session.execute(stmt)

        logger.debug(
            f"[TagLinker] 更新 SemanticMemory tags 字段 | memory_id={memory_id} | "
            f"原有={len(existing_tags)} | 新增={len(new_tags)} | 合并后={len(merged_tags)}"
        )

    async def unlink_tags(self, memory_id: str, memory_type: str) -> int:
        """
        解除指定记忆实体的所有 Tag 关联

        从 memory_tags 表中删除指定记忆实体的所有关联记录。
        注意：此方法不会更新记忆实体的 tags JSON 字段。

        Args:
            memory_id: 记忆实体 ID
            memory_type: 记忆类型（chunk/episode/semantic）

        Returns:
            删除的关联记录数量
        """
        from sqlalchemy import delete

        stmt = (
            delete(MemoryTag)
            .where(MemoryTag.memory_id == memory_id)
            .where(MemoryTag.memory_type == memory_type)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()

        deleted_count = result.rowcount
        logger.info(
            f"[TagLinker] 解除 Tag 关联 | memory_id={memory_id} | "
            f"memory_type={memory_type} | 删除数量={deleted_count}"
        )
        return deleted_count
