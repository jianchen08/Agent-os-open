"""经验桥梁 — 将成功的 Episode 转化为 Knowledge。

实现经验沉淀的核心环节：总结 → 存储。
当任务完成且评估通过时，自动将执行经验转化为可检索的知识。

暴露接口：
- ExperienceBridge: 经验桥梁服务
- KnowledgeResult: 经验沉淀结果
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from memory.episode_service import EpisodeService
from memory.knowledge_service import KnowledgeService

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeResult:
    """经验沉淀结果。

    Attributes:
        episode_id: 来源情景记忆 ID
        success: 是否沉淀成功
        knowledge_id: 生成的知识 ID（成功时有值）
        summary: 沉淀摘要描述
        error: 错误信息（失败时有值）
    """

    episode_id: str = ""
    success: bool = False
    knowledge_id: str = ""
    summary: str = ""
    error: str | None = None


class ExperienceBridge:
    """经验桥梁 — 将成功的 Episode 转化为 Knowledge。

    实现经验沉淀的核心环节：总结 → 存储。
    当任务完成且评估通过时，自动将执行经验转化为可检索的知识。

    Attributes:
        _episode_service: 情景记忆服务
        _knowledge_service: 知识服务
    """

    def __init__(
        self,
        episode_service: EpisodeService,
        knowledge_service: KnowledgeService,
    ) -> None:
        """初始化经验桥梁。

        Args:
            episode_service: 情景记忆服务实例
            knowledge_service: 知识服务实例
        """
        self._episode_service = episode_service
        self._knowledge_service = knowledge_service

    async def consolidate_to_knowledge(
        self,
        episode_id: str,
        user_id: str,
        *,
        force: bool = False,
    ) -> KnowledgeResult:
        """将 Episode 整理为 Knowledge。

        流程：
        1. 获取 Episode
        2. 检查是否已完成（有 execution_summary 且 final_score > 0），force=True 跳过检查
        3. 从 Episode 提取关键信息，生成知识内容
        4. 存储为 Knowledge（source_type="experience"）
        5. 更新 Episode 标记已沉淀

        Args:
            episode_id: 情景记忆 ID
            user_id: 用户 ID
            force: 强制沉淀（跳过完成状态检查）

        Returns:
            KnowledgeResult 包含是否成功、知识 ID、摘要
        """
        # 1. 获取 Episode
        episode = await self._episode_service.get_episode(episode_id, user_id)
        if episode is None:
            return KnowledgeResult(
                episode_id=episode_id,
                success=False,
                error=f"Episode 不存在: {episode_id}",
            )

        # 2. 检查完成状态
        if not force:
            execution_summary = episode.get("execution_summary")
            final_score = episode.get("final_score")
            if not execution_summary:
                return KnowledgeResult(
                    episode_id=episode_id,
                    success=False,
                    error="Episode 未完成：缺少 execution_summary",
                )
            if final_score is None or final_score <= 0:
                return KnowledgeResult(
                    episode_id=episode_id,
                    success=False,
                    error=f"Episode 评分不足：final_score={final_score}",
                )

        # 3. 生成知识内容
        content = self._generate_knowledge_content(episode)
        if not content.strip():
            return KnowledgeResult(
                episode_id=episode_id,
                success=False,
                error="Episode 内容为空，无法生成知识",
            )

        # 4. 存储为 Knowledge
        try:
            knowledge_dict = await self._knowledge_service.create_knowledge(
                user_id=user_id,
                content=content,
                source_type="experience",
                extra_data={
                    "source_episode_id": episode_id,
                    "final_score": episode.get("final_score"),
                },
            )
            knowledge_id = knowledge_dict.get("id", "")
        except Exception as e:
            logger.warning(
                "[ExperienceBridge] 知识存储失败 | episode_id=%s | error=%s",
                episode_id, e,
            )
            return KnowledgeResult(
                episode_id=episode_id,
                success=False,
                error=f"知识存储失败: {e}",
            )

        # 5. 更新 Episode 标记已沉淀
        try:
            tags = episode.get("tags", [])
            if "consolidated" not in tags:
                tags.append("consolidated")
                await self._episode_service.consolidate_episode(
                    episode_id,
                    summary=episode.get("execution_summary", "") + " [已沉淀为知识]",
                )
        except Exception as e:
            logger.debug(
                "[ExperienceBridge] Episode 标记更新失败（不影响沉淀） | error=%s", e,
            )

        summary = f"已将 Episode {episode_id} 沉淀为知识 {knowledge_id}"
        logger.info("[ExperienceBridge] %s", summary)

        return KnowledgeResult(
            episode_id=episode_id,
            success=True,
            knowledge_id=knowledge_id,
            summary=summary,
        )

    async def batch_consolidate(
        self,
        user_id: str,
        *,
        min_score: float = 60.0,
        limit: int = 10,
    ) -> list[KnowledgeResult]:
        """批量沉淀高分 Episode。

        Args:
            user_id: 用户 ID
            min_score: 最低评分阈值
            limit: 最大处理数量

        Returns:
            沉淀结果列表
        """
        # 获取用户所有 Episode
        episodes_data = await self._episode_service.list_episodes(
            user_id, page_size=10000,
        )
        items = episodes_data.get("items", [])

        # 筛选高分且未沉淀的 Episode
        candidates: list[dict[str, Any]] = []
        for item in items:
            final_score = item.get("final_score")
            tags = item.get("tags", [])
            # 跳过已沉淀的
            if "consolidated" in tags:
                continue
            # 检查评分
            if final_score is not None and final_score >= min_score:
                candidates.append(item)

        # 按评分降序排列
        candidates.sort(key=lambda x: x.get("final_score", 0), reverse=True)
        candidates = candidates[:limit]

        results: list[KnowledgeResult] = []
        for candidate in candidates:
            episode_id = candidate.get("id", "")
            result = await self.consolidate_to_knowledge(
                episode_id=episode_id,
                user_id=user_id,
            )
            results.append(result)

        logger.info(
            "[ExperienceBridge] 批量沉淀完成 | candidates=%d | results=%d",
            len(candidates), len(results),
        )
        return results

    @staticmethod
    def _generate_knowledge_content(episode: dict[str, Any]) -> str:
        """从 Episode 生成知识内容。

        Args:
            episode: 情景记忆字典

        Returns:
            拼接后的知识内容字符串
        """
        parts: list[str] = []
        if episode.get("intent_text"):
            parts.append(f"意图: {episode['intent_text']}")
        if episode.get("execution_summary"):
            parts.append(f"执行摘要: {episode['execution_summary']}")
        if episode.get("plan_dag"):
            parts.append(f"执行计划: {json.dumps(episode['plan_dag'], ensure_ascii=False)}")
        if episode.get("final_score") is not None:
            parts.append(f"评分: {episode['final_score']}")
        return "\n".join(parts)
