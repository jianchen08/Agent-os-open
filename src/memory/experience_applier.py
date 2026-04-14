"""经验应用决策器。

在新任务开始时，检索历史经验并决定是否注入到上下文中。
实现经验沉淀闭环的"检索 → 应用"环节。

暴露接口：
- ExperienceApplier: 经验应用决策器
- ExperienceMatch: 经验匹配结果
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from memory.service import MemoryService

logger = logging.getLogger(__name__)


@dataclass
class ExperienceMatch:
    """经验匹配结果。

    Attributes:
        knowledge_id: 知识 ID
        content: 知识内容
        relevance: 相关度分数 (0-1)
        source_episode_id: 来源情景记忆 ID
    """

    knowledge_id: str = ""
    content: str = ""
    relevance: float = 0.0
    source_episode_id: str = ""


class ExperienceApplier:
    """经验应用决策器。

    在新任务开始时，检索历史经验并决定是否注入到上下文中。

    Attributes:
        _memory_service: 记忆服务门面
    """

    def __init__(
        self,
        memory_service: MemoryService,
    ) -> None:
        """初始化经验应用决策器。

        Args:
            memory_service: 记忆服务门面实例
        """
        self._memory_service = memory_service

    async def find_relevant_experience(
        self,
        intent: str,
        user_id: str,
        *,
        max_results: int = 3,
        min_score: float = 70.0,
    ) -> list[ExperienceMatch]:
        """查找与当前意图相关的历史经验。

        通过 MemoryService 检索语义记忆中 source_type="experience" 的知识，
        将相关度分数归一化到 0-1 范围后返回匹配列表。

        Args:
            intent: 当前意图文本
            user_id: 用户 ID
            max_results: 最大返回数量
            min_score: 最低相关度分数（0-100，与 SearchResult.score 映射）

        Returns:
            相关经验列表，按相关度降序排列
        """
        try:
            results = await self._memory_service.retrieve(
                user_id=user_id,
                filter={
                    "memory_type": "semantic",
                    "source_type": "experience",
                },
                query=intent,
                top_k=max_results,
            )
        except Exception as e:
            logger.warning(
                "[ExperienceApplier] 检索失败 | intent=%s | error=%s",
                intent[:50], e,
            )
            return []

        matches: list[ExperienceMatch] = []
        for result in results:
            # 归一化 score 到 0-100 范围（SearchResult.score 是 0-1）
            normalized_score = result.score * 100
            if normalized_score < min_score:
                continue

            metadata = result.metadata or {}
            source_episode_id = metadata.get("source_episode_id", "")

            matches.append(ExperienceMatch(
                knowledge_id=result.id,
                content=result.content,
                relevance=result.score,
                source_episode_id=source_episode_id,
            ))

        # 按相关度降序排列
        matches.sort(key=lambda m: m.relevance, reverse=True)
        return matches[:max_results]

    def should_apply(
        self,
        matches: list[ExperienceMatch],
        *,
        relevance_threshold: float = 0.6,
    ) -> bool:
        """判断是否应该将经验注入到上下文中。

        有高质量匹配（relevance > threshold）时才注入。

        Args:
            matches: 经验匹配列表
            relevance_threshold: 相关度阈值 (0-1)，超过此值视为高质量匹配

        Returns:
            是否应该注入经验到上下文
        """
        if not matches:
            return False

        return any(m.relevance > relevance_threshold for m in matches)
