"""情景记忆存储服务。

从旧代码 src/memory/episode_service.py 搬迁。
移除 SQLAlchemy 硬依赖，通过 IEpisodeStorage 接口操作存储。
没有 storage 时降级到内存字典。

暴露接口：
- EpisodeService: 情景记忆存储服务
"""

from __future__ import annotations

import logging
from typing import Any

from memory.ports import IEpisodeStorage
from memory.types import Episode

logger = logging.getLogger(__name__)


class EpisodeService:
    """情景记忆存储服务。

    职责（仅存储操作）：
    - 创建和存储情景记忆
    - 更新情景记忆
    - 删除情景记忆
    - 列出情景记忆

    检索操作请使用 MemoryService.retrieve(memory_type="episode", ...)。

    Attributes:
        _storage: 情景记忆存储接口
        _in_memory: 内存降级存储
    """

    def __init__(
        self,
        episode_storage: IEpisodeStorage | None = None,
    ) -> None:
        """初始化情景记忆存储服务。

        Args:
            episode_storage: 情景记忆存储接口，None 时降级到内存
        """
        self._storage = episode_storage
        self._in_memory: dict[str, Episode] = {}

    async def store_episode(self, episode: Episode) -> str:
        """存储情景记忆。

        Args:
            episode: 情景记忆实例

        Returns:
            存储的条目 ID
        """
        if self._storage:
            return await self._storage.save(episode)

        # 内存降级
        self._in_memory[episode.id] = episode
        logger.debug("[EpisodeService] 内存存储 | id=%s", episode.id)
        return episode.id

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
        episode = Episode(
            user_id=user_id,
            intent_text=intent_text,
            plan_dag=plan_dag,
            execution_summary=execution_summary,
            evaluation_report=evaluation_report,
            final_score=final_score,
            tags=tags or [],
        )

        await self.store_episode(episode)

        return episode.to_dict()

    async def get_episode(
        self,
        episode_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """获取情景记忆。

        Args:
            episode_id: 情景记忆 ID
            user_id: 用户 ID（用于权限校验）

        Returns:
            情景记忆字典，不存在或不属于该用户则返回 None
        """
        if self._storage:
            episode = await self._storage.get(episode_id)
        else:
            episode = self._in_memory.get(episode_id)

        if not episode:
            return None

        if episode.user_id != user_id:
            return None

        return episode.to_dict()

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
        if self._storage:
            all_episodes = await self._storage.find_by_user(
                user_id,
                limit=page_size * page + page_size,
                offset=0,
            )
        else:
            all_episodes = [ep for ep in self._in_memory.values() if ep.user_id == user_id]
            all_episodes.sort(key=lambda x: x.created_at, reverse=True)

        total = len(all_episodes)
        start = (page - 1) * page_size
        end = start + page_size
        page_items = all_episodes[start:end]

        items = [ep.to_dict() for ep in page_items]

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def consolidate_episode(
        self,
        episode_id: str,
        summary: str,
    ) -> bool:
        """整理情景记忆（更新执行摘要）。

        Args:
            episode_id: 情景记忆 ID
            summary: 执行摘要

        Returns:
            是否更新成功
        """
        if self._storage:
            return await self._storage.update(episode_id, execution_summary=summary)

        # 内存降级
        episode = self._in_memory.get(episode_id)
        if not episode:
            return False

        episode.execution_summary = summary
        return True

    async def delete_episode(
        self,
        episode_id: str,
        user_id: str,
    ) -> bool:
        """删除情景记忆。

        Args:
            episode_id: 情景记忆 ID
            user_id: 用户 ID（用于权限校验）

        Returns:
            是否删除成功
        """
        if self._storage:
            # 先验证用户权限
            episode = await self._storage.get(episode_id)
            if not episode or episode.user_id != user_id:
                return False
            return await self._storage.delete(episode_id)

        # 内存降级
        episode = self._in_memory.get(episode_id)
        if not episode or episode.user_id != user_id:
            return False

        del self._in_memory[episode_id]
        return True
