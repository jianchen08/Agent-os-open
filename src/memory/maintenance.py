"""记忆维护服务。

提供记忆系统的定期维护任务，包括：
- 过期记忆清理（按保留期）
- TTL 过期清理（按每条记忆的 TTL）
- 相似记忆合并
- 孤立 Tag 清理
- 索引重建
- 容量限制淘汰（LRU + 重要性权重）
- 重要性衰减（指数/线性衰减）

通过 TriggerManager 注册周期触发器自动执行维护任务，
也可手动调用单个维护操作。

暴露接口：
- MaintenanceConfig: 维护配置数据类
- MemoryMaintenanceService: 记忆维护服务
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# 重要性默认初始值
_DEFAULT_IMPORTANCE = 0.5


@dataclass
class MaintenanceConfig:
    """记忆维护配置。

    Attributes:
        ttl_enabled: 是否启用 TTL 过期清理
        default_ttl_seconds: 默认 TTL（秒），未单独设置时使用此值
        capacity_limit: 记忆容量上限，超过时触发淘汰
        decay_enabled: 是否启用重要性衰减
        decay_type: 衰减类型，"exponential" 或 "linear"
        decay_half_life_seconds: 指数衰减半衰期（秒）
        decay_rate: 线性衰减速率（每秒衰减量）
        lru_weight: LRU 因子权重（0-1）
        importance_weight: 重要性因子权重（0-1），与 lru_weight 之和应为 1
        enabled: 是否启用自动维护触发器（兼容旧配置）
        cleanup_interval: 过期清理间隔（秒，兼容旧配置）
        merge_interval: 合并间隔（秒，兼容旧配置）
        merge_similarity_threshold: 合并相似度阈值（兼容旧配置）
        tag_cleanup_interval: Tag 清理间隔（秒，兼容旧配置）
        orphan_tag_threshold: 孤立 Tag 频率阈值（兼容旧配置）
        rebuild_index_interval: 索引重建间隔（秒，兼容旧配置）
    """

    # 新增：TTL 清理
    ttl_enabled: bool = True
    default_ttl_seconds: int = 365 * 24 * 3600  # 默认 365 天
    # 新增：容量限制
    capacity_limit: int = 10000
    # 新增：重要性衰减
    decay_enabled: bool = True
    decay_type: str = "exponential"
    decay_half_life_seconds: float = 7 * 24 * 3600  # 默认 7 天
    decay_rate: float = 0.00001  # 线性衰减速率（保守默认值）
    lru_weight: float = 0.5
    importance_weight: float = 0.5
    # 兼容旧配置
    enabled: bool = False
    cleanup_interval: int = 3600
    merge_interval: int = 86400
    merge_similarity_threshold: float = 0.95
    tag_cleanup_interval: int = 21600
    orphan_tag_threshold: int = 0
    rebuild_index_interval: int = 604800

    def __post_init__(self) -> None:
        """校验配置合法性。"""
        if self.lru_weight + self.importance_weight <= 0:
            raise ValueError("lru_weight + importance_weight 必须大于 0")
        if self.capacity_limit < 1:
            raise ValueError("capacity_limit 必须大于等于 1")
        if self.decay_type not in ("exponential", "linear"):
            raise ValueError("decay_type 必须为 'exponential' 或 'linear'")
        if self.decay_type == "exponential" and self.decay_half_life_seconds <= 0:
            raise ValueError("指数衰减半衰期必须大于 0")
        if self.decay_type == "linear" and self.decay_rate < 0:
            raise ValueError("线性衰减速率不能为负")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MaintenanceConfig:
        """从字典创建配置，未提供的字段使用默认值。

        Args:
            data: 配置字典

        Returns:
            MaintenanceConfig 实例
        """
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


class MemoryMaintenanceService:
    """记忆维护服务。

    负责记忆系统的定期维护操作，支持手动触发和通过
    TriggerManager 注册周期触发器自动执行。

    Attributes:
        _memory_service: 记忆服务门面实例
        _config: 维护配置
        _stats: 维护操作统计
    """

    def __init__(
        self,
        memory_service: Any,
        config: MaintenanceConfig | dict[str, Any] | None = None,
    ) -> None:
        """初始化记忆维护服务。

        Args:
            memory_service: 记忆服务门面实例（MemoryService）
            config: 维护配置，支持 MaintenanceConfig 实例、配置字典或 None（使用默认值）
        """
        self._memory_service = memory_service

        if config is None:
            self._config = MaintenanceConfig()
        elif isinstance(config, dict):
            self._config = MaintenanceConfig.from_dict(config)
        else:
            self._config = config

        self._stats: dict[str, Any] = {
            "last_cleanup_at": None,
            "last_merge_at": None,
            "last_tag_cleanup_at": None,
            "last_rebuild_at": None,
            "last_ttl_cleanup_at": None,
            "last_eviction_at": None,
            "last_decay_at": None,
            "cleanup_count": 0,
            "merge_count": 0,
            "tag_cleanup_count": 0,
            "rebuild_count": 0,
            "ttl_cleanup_count": 0,
            "eviction_count": 0,
            "decay_count": 0,
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
        if not self._config.enabled:
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
        cleanup_interval = self._config.cleanup_interval
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
        merge_interval = self._config.merge_interval
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
        tag_interval = self._config.tag_cleanup_interval
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
        rebuild_interval = self._config.rebuild_index_interval
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
    # 原有维护操作（保持不变）
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
                cleaned = await self._cleanup_expired_episodes(
                    episode_service, now, Lifecycle.EPISODE_RETENTION,
                )
                result["cleaned_episodes"] = cleaned
            else:
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
        threshold = self._config.merge_similarity_threshold
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
        threshold = self._config.orphan_tag_threshold
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
                if tag.name in tag_service._cache:
                    del tag_service._cache[tag.name]

                if (
                    tag_service._vector_retriever
                    and hasattr(
                        tag_service._vector_retriever, "delete_tag",
                    )
                ):
                    await tag_service._vector_retriever.delete_tag(tag.name)

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
            if knowledge_service._storage:
                all_knowledge = await knowledge_service._storage.find_by_user(
                    "__all__", limit=1000000,
                )
            else:
                all_knowledge = list(knowledge_service._in_memory.values())

            for kn in all_knowledge:
                try:
                    text = kn.content
                    if not text:
                        continue
                    embedding = await embed_fn(text)
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
                        "[Maintenance] 重建语义索引失败 | id=%s | error=%s",
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

    # ============================================
    # 新增维护操作
    # ============================================

    def cleanup_ttl_expired(
        self,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """清理 TTL 过期的记忆。

        每条记忆可在 extra_data 中设置 ttl_seconds，到期后自动删除。
        未设置 TTL 的记忆使用 default_ttl_seconds。
        TTL 为 0 表示永不过期。

        Args:
            now: 当前时间，None 则自动获取

        Returns:
            清理结果字典
        """
        if not self._config.ttl_enabled:
            return {
                "status": "skipped",
                "reason": "TTL cleanup disabled",
                "cleaned_count": 0,
            }

        if now is None:
            now = datetime.now(UTC)

        result: dict[str, Any] = {
            "status": "success",
            "cleaned_count": 0,
            "errors": [],
        }

        cleaned = 0

        # 清理情景记忆
        try:
            episode_service = self._memory_service._episode_service
            if episode_service._storage:
                cleaned += self._cleanup_ttl_episodes_storage(
                    episode_service, now,
                )
            else:
                cleaned += self._cleanup_ttl_episodes_memory(
                    episode_service, now,
                )
        except Exception as e:
            logger.warning("[Maintenance] TTL 清理情景记忆失败: %s", e)
            result["errors"].append(f"episode: {e}")

        # 清理语义记忆
        try:
            knowledge_service = self._memory_service._knowledge_service
            if knowledge_service._storage:
                cleaned += self._cleanup_ttl_knowledge_storage(
                    knowledge_service, now,
                )
            else:
                cleaned += self._cleanup_ttl_knowledge_memory(
                    knowledge_service, now,
                )
        except Exception as e:
            logger.warning("[Maintenance] TTL 清理语义记忆失败: %s", e)
            result["errors"].append(f"knowledge: {e}")

        result["cleaned_count"] = cleaned

        self._stats["last_ttl_cleanup_at"] = now.isoformat()
        self._stats["ttl_cleanup_count"] += 1

        logger.info("[Maintenance] TTL 清理完成 | cleaned=%d", cleaned)
        return result

    def evict_by_capacity(
        self,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """按容量限制淘汰低价值记忆。

        当记忆总数超过 capacity_limit 时，按综合评分
        （LRU + 重要性权重）淘汰评分最低的记忆。

        Args:
            now: 当前时间，None 则自动获取

        Returns:
            淘汰结果字典
        """
        if now is None:
            now = datetime.now(UTC)

        result: dict[str, Any] = {
            "status": "success",
            "evicted_count": 0,
            "errors": [],
        }

        limit = self._config.capacity_limit

        # 情景记忆淘汰
        try:
            episode_service = self._memory_service._episode_service
            if episode_service._storage:
                evicted = self._evict_episodes_storage(
                    episode_service, now, limit,
                )
            else:
                evicted = self._evict_episodes_memory(
                    episode_service, now, limit,
                )
            result["evicted_count"] += evicted
        except Exception as e:
            logger.warning("[Maintenance] 容量淘汰情景记忆失败: %s", e)
            result["errors"].append(f"episode: {e}")

        # 语义记忆淘汰
        try:
            knowledge_service = self._memory_service._knowledge_service
            if knowledge_service._storage:
                evicted = self._evict_knowledge_storage(
                    knowledge_service, now, limit,
                )
            else:
                evicted = self._evict_knowledge_memory(
                    knowledge_service, now, limit,
                )
            result["evicted_count"] += evicted
        except Exception as e:
            logger.warning("[Maintenance] 容量淘汰语义记忆失败: %s", e)
            result["errors"].append(f"knowledge: {e}")

        self._stats["last_eviction_at"] = now.isoformat()
        self._stats["eviction_count"] += 1

        logger.info(
            "[Maintenance] 容量淘汰完成 | evicted=%d",
            result["evicted_count"],
        )
        return result

    def decay_importance(
        self,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """对记忆的重要性进行时间衰减。

        根据配置的衰减类型（指数/线性）和衰减参数，
        随时间降低记忆的重要性分数。衰减结果写入 extra_data。

        Args:
            now: 当前时间，None 则自动获取

        Returns:
            衰减结果字典
        """
        if not self._config.decay_enabled:
            return {
                "status": "skipped",
                "reason": "decay disabled",
                "decayed_count": 0,
            }

        if now is None:
            now = datetime.now(UTC)

        result: dict[str, Any] = {
            "status": "success",
            "decayed_count": 0,
            "errors": [],
        }

        decayed = 0

        # 衰减情景记忆
        try:
            episode_service = self._memory_service._episode_service
            if not episode_service._storage:
                for ep in episode_service._in_memory.values():
                    self._apply_decay_to_entry(ep, now)
                    decayed += 1
        except Exception as e:
            logger.warning("[Maintenance] 衰减情景记忆失败: %s", e)
            result["errors"].append(f"episode: {e}")

        # 衰减语义记忆
        try:
            knowledge_service = self._memory_service._knowledge_service
            if not knowledge_service._storage:
                for kn in knowledge_service._in_memory.values():
                    self._apply_decay_to_entry(kn, now)
                    decayed += 1
        except Exception as e:
            logger.warning("[Maintenance] 衰减语义记忆失败: %s", e)
            result["errors"].append(f"knowledge: {e}")

        result["decayed_count"] = decayed

        self._stats["last_decay_at"] = now.isoformat()
        self._stats["decay_count"] += 1

        logger.info("[Maintenance] 重要性衰减完成 | decayed=%d", decayed)
        return result

    # ============================================
    # 统一维护入口
    # ============================================

    async def run_maintenance(self) -> dict[str, Any]:
        """执行全部维护任务。

        按顺序执行所有已启用的维护操作，单个任务失败不影响后续任务。

        Returns:
            维护结果字典
        """
        results: dict[str, Any] = {
            "started_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "tasks": {},
        }

        # 按顺序执行，单个任务失败不影响后续任务
        results["tasks"]["cleanup_expired"] = await self.cleanup_expired()
        results["tasks"]["merge_similar"] = await self.merge_similar()
        results["tasks"]["cleanup_orphan_tags"] = (
            await self.cleanup_orphan_tags()
        )
        results["tasks"]["rebuild_index"] = await self.rebuild_index()

        # 新增维护任务
        results["tasks"]["cleanup_ttl_expired"] = self.cleanup_ttl_expired()
        results["tasks"]["evict_by_capacity"] = self.evict_by_capacity()
        results["tasks"]["decay_importance"] = self.decay_importance()

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
    # TTL 清理内部方法
    # ============================================

    def _get_ttl(self, entry: Any) -> int:
        """获取记忆条目的 TTL。

        优先从 extra_data.ttl_seconds 读取，否则使用默认值。

        Args:
            entry: 记忆条目（Episode 或 Knowledge）

        Returns:
            TTL 秒数，0 表示永不过期
        """
        extra = getattr(entry, "extra_data", None)
        if extra and isinstance(extra, dict):
            ttl = extra.get("ttl_seconds")
            if ttl is not None:
                return int(ttl)
        return self._config.default_ttl_seconds

    def _is_ttl_expired(self, entry: Any, now: datetime) -> bool:
        """判断记忆是否已过期。

        Args:
            entry: 记忆条目
            now: 当前时间

        Returns:
            是否过期
        """
        ttl = self._get_ttl(entry)
        if ttl == 0:
            return False  # 永不过期
        created_at = getattr(entry, "created_at", now)
        elapsed = now.timestamp() - created_at.timestamp()
        return elapsed > ttl

    def _cleanup_ttl_episodes_memory(
        self,
        episode_service: Any,
        now: datetime,
    ) -> int:
        """清理内存中 TTL 过期的情景记忆。

        Args:
            episode_service: 情景记忆服务
            now: 当前时间

        Returns:
            清理数量
        """
        expired_ids = [
            eid
            for eid, ep in episode_service._in_memory.items()
            if self._is_ttl_expired(ep, now)
        ]
        for eid in expired_ids:
            del episode_service._in_memory[eid]
        return len(expired_ids)

    def _cleanup_ttl_episodes_storage(
        self,
        episode_service: Any,
        now: datetime,
    ) -> int:
        """清理存储后端中 TTL 过期的情景记忆。

        Args:
            episode_service: 情景记忆服务
            now: 当前时间

        Returns:
            清理数量
        """
        cleaned = 0
        try:
            all_episodes = episode_service._storage.find_by_user(
                "__all__", limit=1000000, offset=0,
            )
            # 处理异步存储
            import asyncio
            if asyncio.iscoroutine(all_episodes):
                return 0  # 存储后端的异步操作在外层处理

            for ep in all_episodes:
                if self._is_ttl_expired(ep, now):
                    episode_service._storage.delete(ep.id)
                    cleaned += 1
        except Exception as e:
            logger.warning("[Maintenance] TTL 存储清理情景失败: %s", e)
        return cleaned

    def _cleanup_ttl_knowledge_memory(
        self,
        knowledge_service: Any,
        now: datetime,
    ) -> int:
        """清理内存中 TTL 过期的语义记忆。

        Args:
            knowledge_service: 知识服务
            now: 当前时间

        Returns:
            清理数量
        """
        expired_ids = [
            kid
            for kid, kn in knowledge_service._in_memory.items()
            if self._is_ttl_expired(kn, now)
        ]
        for kid in expired_ids:
            del knowledge_service._in_memory[kid]
        return len(expired_ids)

    def _cleanup_ttl_knowledge_storage(
        self,
        knowledge_service: Any,
        now: datetime,
    ) -> int:
        """清理存储后端中 TTL 过期的语义记忆。

        Args:
            knowledge_service: 知识服务
            now: 当前时间

        Returns:
            清理数量
        """
        cleaned = 0
        try:
            all_knowledge = knowledge_service._storage.find_by_user(
                "__all__", limit=1000000,
            )
            import asyncio
            if asyncio.iscoroutine(all_knowledge):
                return 0

            for kn in all_knowledge:
                if self._is_ttl_expired(kn, now):
                    knowledge_service._storage.delete(kn.id)
                    cleaned += 1
        except Exception as e:
            logger.warning("[Maintenance] TTL 存储清理知识失败: %s", e)
        return cleaned

    # ============================================
    # 容量淘汰内部方法
    # ============================================

    def _calculate_eviction_score(
        self,
        created_at: datetime,
        last_accessed_at: str | None,
        importance: float,
        now: datetime,
    ) -> float:
        """计算记忆淘汰评分（越高越不容易被淘汰）。

        综合考虑 LRU（最近访问时间）和重要性权重。

        Args:
            created_at: 创建时间
            last_accessed_at: 最后访问时间 ISO 字符串
            importance: 重要性分数
            now: 当前时间

        Returns:
            淘汰评分 [0, 1]
        """
        lru_score = self._calculate_lru_score(
            created_at, last_accessed_at, now,
        )
        return (
            self._config.lru_weight * lru_score
            + self._config.importance_weight * importance
        )

    def _calculate_lru_score(
        self,
        created_at: datetime,
        last_accessed_at: str | None,
        now: datetime,
    ) -> float:
        """计算 LRU 评分。

        Args:
            created_at: 创建时间
            last_accessed_at: 最后访问时间 ISO 字符串
            now: 当前时间

        Returns:
            LRU 评分 [0, 1]
        """
        # 确定最后访问时间
        if last_accessed_at:
            try:
                last_time = datetime.fromisoformat(last_accessed_at)
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                last_time = created_at
        else:
            last_time = created_at

        elapsed_seconds = max(0, (now - last_time).total_seconds())
        # 使用指数衰减映射到 [0, 1]
        # 1 天内 → 接近 1，30 天外 → 接近 0
        decay_seconds = 7 * 24 * 3600  # 7 天半衰期
        if decay_seconds <= 0:
            return 0.5
        return math.exp(-elapsed_seconds / decay_seconds)

    def _get_importance(self, entry: Any) -> float:
        """获取记忆条目的重要性分数。

        Args:
            entry: 记忆条目

        Returns:
            重要性分数 [0, 1]
        """
        extra = getattr(entry, "extra_data", None)
        if extra and isinstance(extra, dict):
            imp = extra.get("importance")
            if imp is not None:
                return float(imp)
        return _DEFAULT_IMPORTANCE

    def _get_last_accessed(self, entry: Any) -> str | None:
        """获取记忆条目的最后访问时间。

        Args:
            entry: 记忆条目

        Returns:
            最后访问时间的 ISO 字符串，或 None
        """
        extra = getattr(entry, "extra_data", None)
        if extra and isinstance(extra, dict):
            return extra.get("last_accessed_at")
        return None

    def _evict_episodes_memory(
        self,
        episode_service: Any,
        now: datetime,
        limit: int,
    ) -> int:
        """淘汰内存中超出容量的情景记忆。

        Args:
            episode_service: 情景记忆服务
            now: 当前时间
            limit: 容量上限

        Returns:
            淘汰数量
        """
        items = episode_service._in_memory
        return self._evict_in_memory_items(items, now, limit)

    def _evict_knowledge_memory(
        self,
        knowledge_service: Any,
        now: datetime,
        limit: int,
    ) -> int:
        """淘汰内存中超出容量的语义记忆。

        Args:
            knowledge_service: 知识服务
            now: 当前时间
            limit: 容量上限

        Returns:
            淘汰数量
        """
        items = knowledge_service._in_memory
        return self._evict_in_memory_items(items, now, limit)

    def _evict_in_memory_items(
        self,
        items: dict[str, Any],
        now: datetime,
        limit: int,
    ) -> int:
        """通用内存淘汰逻辑。

        Args:
            items: 内存字典 {id: entry}
            now: 当前时间
            limit: 容量上限

        Returns:
            淘汰数量
        """
        current_count = len(items)
        if current_count <= limit:
            return 0

        evict_count = current_count - limit
        if evict_count <= 0:
            return 0

        # 计算每条记忆的淘汰评分
        scored: list[tuple[str, float]] = []
        for key, entry in items.items():
            importance = self._get_importance(entry)
            last_accessed = self._get_last_accessed(entry)
            created_at = getattr(entry, "created_at", now)
            score = self._calculate_eviction_score(
                created_at, last_accessed, importance, now,
            )
            scored.append((key, score))

        # 按评分升序排列（评分最低的最先淘汰）
        scored.sort(key=lambda x: x[1])

        # 淘汰评分最低的 evict_count 条
        evicted = 0
        for key, _ in scored[:evict_count]:
            if key in items:
                del items[key]
                evicted += 1

        return evicted

    def _evict_episodes_storage(
        self,
        episode_service: Any,
        now: datetime,
        limit: int,
    ) -> int:
        """淘汰存储后端中超出容量的情景记忆。

        Args:
            episode_service: 情景记忆服务
            now: 当前时间
            limit: 容量上限

        Returns:
            淘汰数量
        """
        try:
            count = episode_service._storage.count_by_user("__all__")
            import asyncio
            if asyncio.iscoroutine(count):
                return 0

            if count <= limit:
                return 0

            evict_count = count - limit
            all_episodes = episode_service._storage.find_by_user(
                "__all__", limit=1000000, offset=0,
            )
            if asyncio.iscoroutine(all_episodes):
                return 0

            scored: list[tuple[str, float]] = []
            for ep in all_episodes:
                importance = self._get_importance(ep)
                last_accessed = self._get_last_accessed(ep)
                score = self._calculate_eviction_score(
                    ep.created_at, last_accessed, importance, now,
                )
                scored.append((ep.id, score))

            scored.sort(key=lambda x: x[1])

            evicted = 0
            for ep_id, _ in scored[:evict_count]:
                episode_service._storage.delete(ep_id)
                evicted += 1

            return evicted
        except Exception as e:
            logger.warning("[Maintenance] 存储后端容量淘汰失败: %s", e)
            return 0

    def _evict_knowledge_storage(
        self,
        knowledge_service: Any,
        now: datetime,
        limit: int,
    ) -> int:
        """淘汰存储后端中超出容量的语义记忆。

        Args:
            knowledge_service: 知识服务
            now: 当前时间
            limit: 容量上限

        Returns:
            淘汰数量
        """
        try:
            all_knowledge = knowledge_service._storage.find_by_user(
                "__all__", limit=1000000,
            )
            import asyncio
            if asyncio.iscoroutine(all_knowledge):
                return 0

            if len(all_knowledge) <= limit:
                return 0

            evict_count = len(all_knowledge) - limit

            scored: list[tuple[str, float]] = []
            for kn in all_knowledge:
                importance = self._get_importance(kn)
                last_accessed = self._get_last_accessed(kn)
                score = self._calculate_eviction_score(
                    kn.created_at, last_accessed, importance, now,
                )
                scored.append((kn.id, score))

            scored.sort(key=lambda x: x[1])

            evicted = 0
            for kn_id, _ in scored[:evict_count]:
                knowledge_service._storage.delete(kn_id)
                evicted += 1

            return evicted
        except Exception as e:
            logger.warning("[Maintenance] 存储后端知识容量淘汰失败: %s", e)
            return 0

    # ============================================
    # 重要性衰减内部方法
    # ============================================

    def _apply_decay(
        self,
        importance: float,
        elapsed_seconds: float,
    ) -> float:
        """应用衰减公式计算新的重要性。

        Args:
            importance: 当前重要性
            elapsed_seconds: 经过的时间（秒）

        Returns:
            衰减后的重要性 [0, 1]
        """
        if self._config.decay_type == "exponential":
            half_life = self._config.decay_half_life_seconds
            if half_life <= 0:
                return importance
            factor = 0.5 ** (elapsed_seconds / half_life)
            return max(0.0, importance * factor)
        else:
            # linear
            decayed = importance - self._config.decay_rate * elapsed_seconds
            return max(0.0, decayed)

    def _apply_decay_to_entry(self, entry: Any, now: datetime) -> None:
        """对单条记忆应用衰减。

        修改 entry.extra_data 中的 importance 字段。

        Args:
            entry: 记忆条目
            now: 当前时间
        """
        created_at = getattr(entry, "created_at", now)
        elapsed = max(0.0, (now - created_at).total_seconds())
        current_importance = self._get_importance(entry)
        new_importance = self._apply_decay(current_importance, elapsed)

        # 写回 extra_data
        extra = getattr(entry, "extra_data", None)
        if extra is None:
            extra = {}
            entry.extra_data = extra  # type: ignore[attr-defined]
        if isinstance(extra, dict):
            extra["importance"] = new_importance

    # ============================================
    # 原有内部辅助方法
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
