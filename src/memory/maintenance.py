"""记忆维护服务。

提供记忆系统的定期维护任务，包括：
- 过期记忆清理
- 相似记忆合并
- 孤立 Tag 清理
- 索引重建

通过 TriggerManager 注册周期触发器自动执行维护任务，
也可手动调用单个维护操作。

暴露接口：
- MemoryMaintenanceService: 记忆维护服务
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class MemoryMaintenanceService:
    """记忆维护服务。

    负责记忆系统的定期维护操作，支持手动触发和通过
    TriggerManager 注册周期触发器自动执行。

    Attributes:
        _memory_service: 记忆服务门面实例
        _config: 维护配置字典
        _stats: 维护操作统计
    """

    def __init__(
        self,
        memory_service: Any,
        config: dict[str, Any] | None = None,
    ) -> None:
        """初始化记忆维护服务。

        Args:
            memory_service: 记忆服务门面实例（MemoryService）
            config: 维护配置字典，支持以下键：
                - cleanup_interval: 过期清理间隔（秒）
                - merge_interval: 合并间隔（秒）
                - tag_cleanup_interval: Tag 清理间隔（秒）
                - rebuild_index_interval: 索引重建间隔（秒）
                - merge_similarity_threshold: 合并相似度阈值
                - orphan_tag_threshold: 孤立 Tag 频率阈值
        """
        self._memory_service = memory_service
        self._config = config or {}
        self._stats: dict[str, Any] = {
            "last_cleanup_at": None,
            "last_merge_at": None,
            "last_tag_cleanup_at": None,
            "last_rebuild_at": None,
            "cleanup_count": 0,
            "merge_count": 0,
            "tag_cleanup_count": 0,
            "rebuild_count": 0,
        }

    # ============================================
    # 自动注册维护触发器
    # ============================================

    def register_triggers(self) -> list[str]:
        """向 TriggerManager 注册所有维护触发器。

        根据配置的间隔时间，为每个维护任务注册 INTERVAL 类型触发器。
        触发时执行对应的维护操作。

        Returns:
            注册的触发器 ID 列表
        """
        if not self._config.get("enabled", False):
            logger.info("[Maintenance] 自动维护未启用，跳过触发器注册")
            return []

        try:
            from triggers import TriggerManager, TriggerConfig
            from triggers.types import TriggerType
        except ImportError:
            logger.warning(
                "[Maintenance] TriggerManager 不可用，"
                "无法注册自动维护触发器"
            )
            return []

        trigger_manager: TriggerManager = _get_trigger_manager_safe()
        if trigger_manager is None:
            return []

        registered: list[str] = []

        # 注册过期清理触发器
        cleanup_interval = self._config.get("cleanup_interval", 3600)
        if cleanup_interval > 0:
            cleanup_id = "memory_maintenance_cleanup"
            trigger_manager.register(TriggerConfig(
                trigger_id=cleanup_id,
                name="记忆过期清理",
                trigger_type=TriggerType.INTERVAL,
                interval_seconds=cleanup_interval,
                action="memory_maintenance.cleanup_expired",
                max_fires=0,
                metadata={"maintenance_type": "cleanup"},
            ))
            registered.append(cleanup_id)

        # 注册相似合并触发器
        merge_interval = self._config.get("merge_interval", 86400)
        if merge_interval > 0:
            merge_id = "memory_maintenance_merge"
            trigger_manager.register(TriggerConfig(
                trigger_id=merge_id,
                name="相似记忆合并",
                trigger_type=TriggerType.INTERVAL,
                interval_seconds=merge_interval,
                action="memory_maintenance.merge_similar",
                max_fires=0,
                metadata={"maintenance_type": "merge"},
            ))
            registered.append(merge_id)

        # 注册孤立 Tag 清理触发器
        tag_interval = self._config.get("tag_cleanup_interval", 21600)
        if tag_interval > 0:
            tag_id = "memory_maintenance_tag_cleanup"
            trigger_manager.register(TriggerConfig(
                trigger_id=tag_id,
                name="孤立 Tag 清理",
                trigger_type=TriggerType.INTERVAL,
                interval_seconds=tag_interval,
                action="memory_maintenance.cleanup_orphan_tags",
                max_fires=0,
                metadata={"maintenance_type": "tag_cleanup"},
            ))
            registered.append(tag_id)

        # 注册索引重建触发器
        rebuild_interval = self._config.get(
            "rebuild_index_interval", 604800,
        )
        if rebuild_interval > 0:
            rebuild_id = "memory_maintenance_rebuild"
            trigger_manager.register(TriggerConfig(
                trigger_id=rebuild_id,
                name="记忆索引重建",
                trigger_type=TriggerType.INTERVAL,
                interval_seconds=rebuild_interval,
                action="memory_maintenance.rebuild_index",
                max_fires=0,
                metadata={"maintenance_type": "rebuild"},
            ))
            registered.append(rebuild_id)

        logger.info(
            "[Maintenance] 已注册 %d 个维护触发器: %s",
            len(registered),
            registered,
        )
        return registered

    # ============================================
    # 维护操作
    # ============================================

    async def cleanup_expired(self) -> dict[str, Any]:
        """清理过期记忆。

        根据 Lifecycle 常量中定义的保留时间，删除超过保留期的记忆条目。
        - 情景记忆：默认保留 30 天
        - 语义记忆：默认保留 365 天
        - 工作记忆：默认保留 1 天

        Returns:
            清理结果字典
        """
        from memory.constants import Lifecycle

        now = datetime.now(UTC)
        result: dict[str, Any] = {
            "status": "success",
            "cleaned_episodes": 0,
            "cleaned_knowledge": 0,
            "errors": [],
        }

        # 清理过期的情景记忆
        try:
            episode_service = self._memory_service._episode_service
            if episode_service._storage:
                # 存储后端支持批量删除时直接操作
                cleaned = await self._cleanup_expired_episodes(
                    episode_service, now, Lifecycle.EPISODE_RETENTION,
                )
                result["cleaned_episodes"] = cleaned
            else:
                # 内存降级：按 created_at 过滤
                cleaned = self._cleanup_in_memory_episodes(
                    episode_service, now, Lifecycle.EPISODE_RETENTION,
                )
                result["cleaned_episodes"] = cleaned
        except Exception as e:
            logger.warning("[Maintenance] 清理情景记忆失败: %s", e)
            result["errors"].append(f"episode: {e}")

        # 清理过期的语义记忆
        try:
            knowledge_service = self._memory_service._knowledge_service
            if knowledge_service._storage:
                cleaned = await self._cleanup_expired_knowledge(
                    knowledge_service, now, Lifecycle.SEMANTIC_RETENTION,
                )
                result["cleaned_knowledge"] = cleaned
            else:
                cleaned = self._cleanup_in_memory_knowledge(
                    knowledge_service, now, Lifecycle.SEMANTIC_RETENTION,
                )
                result["cleaned_knowledge"] = cleaned
        except Exception as e:
            logger.warning("[Maintenance] 清理知识失败: %s", e)
            result["errors"].append(f"knowledge: {e}")

        self._stats["last_cleanup_at"] = now.isoformat()
        self._stats["cleanup_count"] += 1

        logger.info(
            "[Maintenance] 过期清理完成 | episodes=%d | knowledge=%d",
            result["cleaned_episodes"],
            result["cleaned_knowledge"],
        )
        return result

    async def merge_similar(self) -> dict[str, Any]:
        """合并相似的语义记忆。

        对所有语义记忆进行两两比较（余弦相似度），
        超过阈值的合并为一条，保留内容更长的一方。

        注意：此操作计算量较大，建议在低峰时段执行。

        Returns:
            合并结果字典
        """
        threshold = self._config.get(
            "merge_similarity_threshold", 0.95,
        )
        now = datetime.now(UTC)
        result: dict[str, Any] = {
            "status": "success",
            "merged_count": 0,
            "checked_pairs": 0,
            "errors": [],
        }

        knowledge_service = self._memory_service._knowledge_service

        # 获取所有知识（不区分用户，全局合并）
        try:
            all_knowledge = await knowledge_service._get_all_knowledge("__all__")
        except Exception as e:
            result["status"] = "error"
            result["errors"].append(f"fetch_all: {e}")
            return result

        if len(all_knowledge) < 2:
            result["status"] = "skipped"
            result["reason"] = "insufficient memories for merging"
            return result

        # 简单的相似度比较（基于文本内容）
        merged_ids: set[str] = set()
        merged_count = 0
        checked_pairs = 0

        for i in range(len(all_knowledge)):
            if all_knowledge[i].id in merged_ids:
                continue
            for j in range(i + 1, len(all_knowledge)):
                if all_knowledge[j].id in merged_ids:
                    continue

                checked_pairs += 1
                similarity = self._text_similarity(
                    all_knowledge[i].content,
                    all_knowledge[j].content,
                )

                if similarity >= threshold:
                    # 保留内容更长的一方，删除较短的一方
                    if len(all_knowledge[i].content) >= len(
                        all_knowledge[j].content,
                    ):
                        remove_id = all_knowledge[j].id
                    else:
                        remove_id = all_knowledge[i].id

                    try:
                        await knowledge_service.delete_knowledge(
                            remove_id,
                            all_knowledge[j].user_id
                            if remove_id == all_knowledge[j].id
                            else all_knowledge[i].user_id,
                        )
                        merged_ids.add(remove_id)
                        merged_count += 1
                    except Exception as e:
                        logger.warning(
                            "[Maintenance] 合并删除失败 | id=%s | error=%s",
                            remove_id,
                            e,
                        )

        result["merged_count"] = merged_count
        result["checked_pairs"] = checked_pairs

        self._stats["last_merge_at"] = now.isoformat()
        self._stats["merge_count"] += 1

        logger.info(
            "[Maintenance] 相似合并完成 | pairs=%d | merged=%d",
            checked_pairs,
            merged_count,
        )
        return result

    async def cleanup_orphan_tags(self) -> dict[str, Any]:
        """清理孤立的 Tag。

        删除频率低于阈值的 Tag 及其关联数据。
        这些 Tag 通常是无意义的关键词或一次性使用后
        不再出现的标签。

        Returns:
            清理结果字典
        """
        threshold = self._config.get("orphan_tag_threshold", 0)
        now = datetime.now(UTC)
        result: dict[str, Any] = {
            "status": "success",
            "removed_tags": 0,
            "errors": [],
        }

        tag_service = self._memory_service._tag_service
        if not tag_service:
            result["status"] = "skipped"
            result["reason"] = "tag_service not available"
            return result

        # 获取所有 Tag
        try:
            all_tags = await tag_service.list_tags(limit=100000)
        except Exception as e:
            result["status"] = "error"
            result["errors"].append(f"list_tags: {e}")
            return result

        # 筛选低频 Tag
        orphan_tags = [
            tag for tag in all_tags
            if tag.frequency <= threshold
        ]

        # 删除孤立 Tag
        removed = 0
        for tag in orphan_tags:
            try:
                # 从缓存中移除
                if tag.name in tag_service._cache:
                    del tag_service._cache[tag.name]

                # 从 PG 中删除（如果有向量检索器）
                if (
                    tag_service._vector_retriever
                    and hasattr(
                        tag_service._vector_retriever, "delete_tag",
                    )
                ):
                    await tag_service._vector_retriever.delete_tag(tag.name)

                # 从磁盘删除
                from pathlib import Path
                safe_name = (
                    tag.name.replace("/", "_")
                    .replace("\\", "_")
                    .replace(":", "_")
                )
                file_path = tag_service._tags_dir / f"{safe_name}.json"
                if file_path.exists():
                    file_path.unlink()

                removed += 1
            except Exception as e:
                logger.warning(
                    "[Maintenance] 删除 Tag 失败 | name=%s | error=%s",
                    tag.name,
                    e,
                )

        result["removed_tags"] = removed

        self._stats["last_tag_cleanup_at"] = now.isoformat()
        self._stats["tag_cleanup_count"] += 1

        logger.info(
            "[Maintenance] Tag 清理完成 | total=%d | removed=%d",
            len(all_tags),
            removed,
        )
        return result

    async def rebuild_index(self) -> dict[str, Any]:
        """重建向量索引。

        从内容存储中读取所有记忆条目，重新生成嵌入向量
        并写入向量索引表。

        需要同时满足以下条件：
        - embedding_service 可用
        - vector_retriever 可用

        Returns:
            重建结果字典
        """
        now = datetime.now(UTC)
        result: dict[str, Any] = {
            "status": "success",
            "reindexed_episodes": 0,
            "reindexed_knowledge": 0,
            "errors": [],
        }

        embedding_service = self._memory_service._embedding_service
        vector_retriever = self._memory_service._vector_retriever

        if not embedding_service:
            result["status"] = "skipped"
            result["reason"] = "embedding_service not available"
            return result

        if not vector_retriever or not hasattr(
            vector_retriever, "save_index",
        ):
            result["status"] = "skipped"
            result["reason"] = "vector_retriever not available"
            return result

        # 获取嵌入函数
        embed_fn = None
        if hasattr(embedding_service, "embed_text"):
            embed_fn = embedding_service.embed_text
        elif hasattr(embedding_service, "embed"):
            embed_fn = embedding_service.embed

        if not embed_fn:
            result["status"] = "error"
            result["reason"] = "no embed function found"
            return result

        # 重建情景记忆索引
        episode_service = self._memory_service._episode_service
        try:
            if episode_service._storage:
                all_episodes = await episode_service._storage.find_by_user(
                    "__all__", limit=1000000, offset=0,
                )
            else:
                all_episodes = list(episode_service._in_memory.values())

            for ep in all_episodes:
                try:
                    text = ep.execution_summary or ep.intent_text
                    if not text:
                        continue
                    embedding = await embed_fn(text)
                    if embedding:
                        await vector_retriever.save_index(
                            entry_id=ep.id,
                            embedding=embedding,
                            user_id=ep.user_id,
                            memory_type="episode",
                        )
                        result["reindexed_episodes"] += 1
                except Exception as e:
                    logger.warning(
                        "[Maintenance] 重建情景索引失败 | id=%s | error=%s",
                        ep.id,
                        e,
                    )
        except Exception as e:
            result["errors"].append(f"episodes: {e}")

        # 重建语义记忆索引
        knowledge_service = self._memory_service._knowledge_service
        try:
            all_knowledge = await knowledge_service._get_all_knowledge(
                "__all__",
            )

            for kn in all_knowledge:
                try:
                    if not kn.content:
                        continue
                    embedding = await embed_fn(kn.content)
                    if embedding:
                        await vector_retriever.save_index(
                            entry_id=kn.id,
                            embedding=embedding,
                            user_id=kn.user_id,
                            memory_type="semantic",
                        )
                        result["reindexed_knowledge"] += 1
                except Exception as e:
                    logger.warning(
                        "[Maintenance] 重建知识索引失败 | id=%s | error=%s",
                        kn.id,
                        e,
                    )
        except Exception as e:
            result["errors"].append(f"knowledge: {e}")

        self._stats["last_rebuild_at"] = now.isoformat()
        self._stats["rebuild_count"] += 1

        logger.info(
            "[Maintenance] 索引重建完成 | episodes=%d | knowledge=%d",
            result["reindexed_episodes"],
            result["reindexed_knowledge"],
        )
        return result

    async def run_all(self) -> dict[str, Any]:
        """执行所有维护任务。

        按顺序执行：过期清理 -> 相似合并 -> Tag 清理 -> 索引重建

        Returns:
            所有任务的结果字典
        """
        logger.info("[Maintenance] 开始执行全部维护任务")

        results: dict[str, Any] = {
            "started_at": datetime.now(UTC).isoformat(),
            "tasks": {},
        }

        # 按顺序执行，单个任务失败不影响后续任务
        results["tasks"]["cleanup_expired"] = await self.cleanup_expired()
        results["tasks"]["merge_similar"] = await self.merge_similar()
        results["tasks"]["cleanup_orphan_tags"] = (
            await self.cleanup_orphan_tags()
        )
        results["tasks"]["rebuild_index"] = await self.rebuild_index()

        results["completed_at"] = datetime.now(UTC).isoformat()
        results["status"] = "completed"

        logger.info("[Maintenance] 全部维护任务执行完成")
        return results

    def get_stats(self) -> dict[str, Any]:
        """获取维护操作统计。

        Returns:
            维护统计字典
        """
        return self._stats.copy()

    # ============================================
    # 内部辅助方法
    # ============================================

    async def _cleanup_expired_episodes(
        self,
        episode_service: Any,
        now: datetime,
        retention_seconds: int,
    ) -> int:
        """清理过期的情景记忆。

        Args:
            episode_service: 情景记忆服务
            now: 当前时间
            retention_seconds: 保留时间（秒）

        Returns:
            清理的条目数量
        """
        cleaned = 0
        cutoff = now.timestamp() - retention_seconds

        try:
            all_episodes = await episode_service._storage.find_by_user(
                "__all__", limit=1000000, offset=0,
            )
            for ep in all_episodes:
                if ep.created_at.timestamp() < cutoff:
                    await episode_service._storage.delete(ep.id)
                    cleaned += 1
        except Exception as e:
            logger.warning(
                "[Maintenance] 存储后端过期清理失败: %s", e,
            )

        return cleaned

    def _cleanup_in_memory_episodes(
        self,
        episode_service: Any,
        now: datetime,
        retention_seconds: int,
    ) -> int:
        """清理内存中的过期情景记忆。

        Args:
            episode_service: 情景记忆服务
            now: 当前时间
            retention_seconds: 保留时间（秒）

        Returns:
            清理的条目数量
        """
        cutoff = now.timestamp() - retention_seconds
        expired_ids = [
            eid
            for eid, ep in episode_service._in_memory.items()
            if ep.created_at.timestamp() < cutoff
        ]

        for eid in expired_ids:
            del episode_service._in_memory[eid]

        return len(expired_ids)

    async def _cleanup_expired_knowledge(
        self,
        knowledge_service: Any,
        now: datetime,
        retention_seconds: int,
    ) -> int:
        """清理过期的语义记忆。

        Args:
            knowledge_service: 知识服务
            now: 当前时间
            retention_seconds: 保留时间（秒）

        Returns:
            清理的条目数量
        """
        cleaned = 0
        cutoff = now.timestamp() - retention_seconds

        try:
            all_knowledge = await knowledge_service._storage.find_by_user(
                "__all__", limit=1000000,
            )
            for kn in all_knowledge:
                if kn.created_at.timestamp() < cutoff:
                    await knowledge_service._storage.delete(kn.id)
                    cleaned += 1
        except Exception as e:
            logger.warning(
                "[Maintenance] 存储后端知识过期清理失败: %s", e,
            )

        return cleaned

    def _cleanup_in_memory_knowledge(
        self,
        knowledge_service: Any,
        now: datetime,
        retention_seconds: int,
    ) -> int:
        """清理内存中的过期语义记忆。

        Args:
            knowledge_service: 知识服务
            now: 当前时间
            retention_seconds: 保留时间（秒）

        Returns:
            清理的条目数量
        """
        cutoff = now.timestamp() - retention_seconds
        expired_ids = [
            kid
            for kid, kn in knowledge_service._in_memory.items()
            if kn.created_at.timestamp() < cutoff
        ]

        for kid in expired_ids:
            del knowledge_service._in_memory[kid]

        return len(expired_ids)

    @staticmethod
    def _text_similarity(text_a: str, text_b: str) -> float:
        """计算两个文本的简单相似度（Jaccard 系数）。

        基于字符级 bigram 集合的 Jaccard 系数，
        用于快速判断文本相似性。

        Args:
            text_a: 文本 A
            text_b: 文本 B

        Returns:
            相似度 [0, 1]
        """
        if not text_a or not text_b:
            return 0.0

        # 生成 bigram 集合
        set_a: set[str] = set()
        for i in range(len(text_a) - 1):
            set_a.add(text_a[i:i + 2])

        set_b: set[str] = set()
        for i in range(len(text_b) - 1):
            set_b.add(text_b[i:i + 2])

        if not set_a or not set_b:
            return 0.0

        intersection = len(set_a & set_b)
        union = len(set_a | set_b)

        if union == 0:
            return 0.0

        return intersection / union


def _get_trigger_manager_safe() -> Any:
    """安全获取 TriggerManager 单例。

    Returns:
        TriggerManager 实例，不可用时返回 None
    """
    try:
        from triggers import get_trigger_manager
        return get_trigger_manager()
    except ImportError:
        return None
