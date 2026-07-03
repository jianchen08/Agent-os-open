"""清理引擎 —— 决策矩阵、分层删除、索引重建、容量计算。

负责清理周期的全部逻辑：
1. 获取所有管道 summary，计算容量压力
2. 逐条判断：复盘状态 x 年龄 x 容量压力 → 决策
3. 分层删除：L0 YAML → L1 压缩块 → Episode
4. 如有删除，重建向量索引
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class CleanupEngine:
    """清理引擎，根据复盘状态、年龄和容量压力分层清理数据。

    Attributes:
        _storage: 执行记录存储（ExecutionRecordStorage）
        _chunk_db: 压缩块服务（ChunkService）
        _memory_service: 记忆服务门面（用于索引重建和 Episode 清理）
        _config: 维护配置（MaintenanceConfig）
    """

    def __init__(
        self,
        storage: Any,
        chunk_db: Any,
        memory_service: Any | None = None,
        config: Any = None,
    ) -> None:
        """初始化清理引擎。

        Args:
            storage: 执行记录存储实例（ExecutionRecordStorage）
            chunk_db: 压缩块服务实例（ChunkService）
            memory_service: 记忆服务门面实例（用于索引重建等操作）
            config: 维护配置实例（MaintenanceConfig）
        """
        self._storage = storage
        self._chunk_db = chunk_db
        self._memory_service = memory_service
        self._config = config

    # ============================================
    # 清理主流程
    # ============================================

    async def cleanup_by_age_and_capacity(  # noqa: PLR0912
        self,
        review_engine: Any | None = None,  # noqa: ARG002  # 保留签名兼容 service 调用
    ) -> dict[str, Any]:
        """根据年龄和容量清理数据。

        清理决策 = 复盘状态 x 年龄 x 容量压力：
        - 已复盘 + age > 30天 → 删除
        - 已复盘 + age > 7天 + 容量紧张 → 删除
        - 未复盘 + age > 30天 → 直接删除（A 路径删除后不再"先复盘再清理"）
        - 其他 → 不动

        清理层级（优先删大的）：
        1. L0 YAML 文件（最大）
        2. L1 压缩块（容量紧张时）
        3. Episode（已沉淀为 Knowledge 的）
        4. Knowledge 永不删除

        Args:
            review_engine: 历史参数，保留签名兼容，当前不再使用
                （清理不再触发复盘，A 路径已删除）。

        Returns:
            清理结果字典
        """
        now = datetime.now(UTC)
        result: dict[str, Any] = {
            "status": "success",
            "l0_deleted": 0,
            "l1_deleted": 0,
            "episodes_deleted": 0,
            "errors": [],
        }

        # Phase 1：获取所有管道 summary
        summaries = self._storage.list_all_summaries()
        if not summaries:
            result["status"] = "skipped"
            result["reason"] = "no pipelines found"
            result["cleaned_at"] = now.isoformat()
            return result

        # Phase 2：计算容量压力
        capacity_pressure = self._get_capacity_pressure()

        deleted_any = False

        # Phase 3：逐条判断
        for summary in summaries:
            try:
                age_days = self._get_pipeline_age_days(summary, now)
                review_status = await self._get_review_status(summary.run_id)

                should_delete_l0 = False
                should_delete_l1 = False

                if review_status == "completed":
                    # 已复盘：根据年龄和容量决定
                    if age_days > self._config.cleanup_min_age_days:
                        should_delete_l0 = True
                        should_delete_l1 = True  # 很老了，L0/L1 一起删
                    elif (
                        age_days > self._config.cleanup_early_age_days
                        and capacity_pressure > self._config.cleanup_capacity_threshold
                    ):
                        should_delete_l0 = True
                        # 容量紧张但不算很老，只删 L0

                elif review_status == "pending":  # noqa: SIM102
                    # 未复盘：A 路径删除后不再"先复盘再清理"，直接按年龄判断。
                    # 很老（>cleanup_min_age_days）还没复盘，视为无价值，直接删 L0+L1。
                    if age_days > self._config.cleanup_min_age_days:
                        should_delete_l0 = True
                        should_delete_l1 = True

                if should_delete_l0:
                    delete_result = await self._delete_pipeline_data(
                        summary.run_id,
                        delete_l1=should_delete_l1,
                    )
                    result["l0_deleted"] += delete_result.get("l0_deleted", 0)
                    result["l1_deleted"] += delete_result.get("l1_deleted", 0)
                    result["episodes_deleted"] += delete_result.get("episodes_deleted", 0)
                    deleted_any = True

            except Exception as e:
                logger.warning(
                    "[Maintenance] 清理管道失败 | pipeline=%s | error=%s",
                    summary.run_id[:12],
                    e,
                )
                result["errors"].append(f"pipeline_{summary.run_id[:12]}: {e}")

        # Phase 5：如有删除，重建向量索引
        if deleted_any:
            try:
                rebuild_result = await self.rebuild_index()
                result["rebuild"] = rebuild_result
            except Exception as e:
                logger.warning("[Maintenance] 索引重建失败: %s", e)
                result["errors"].append(f"rebuild_index: {e}")

        result["cleaned_at"] = now.isoformat()

        logger.info(
            "[Maintenance] 清理完成 | L0=%d | L1=%d | episodes=%d | pressure=%.2f",
            result["l0_deleted"],
            result["l1_deleted"],
            result["episodes_deleted"],
            capacity_pressure,
        )
        return result

    # ============================================
    # 容量与年龄计算
    # ============================================

    def _get_capacity_pressure(self) -> float:
        """获取当前存储容量压力（0.0 ~ 1.0）。

        基于管道数据目录的大小估算。

        Returns:
            容量使用率 [0.0, 1.0]
        """
        try:
            data_dir = self._storage._data_dir
            if data_dir and data_dir.exists():
                total_size = sum(f.stat().st_size for f in data_dir.rglob("*.yaml"))
                # 假设 1GB 为容量上限
                max_bytes = 1024 * 1024 * 1024
                return min(1.0, total_size / max_bytes)
        except Exception:
            pass
        return 0.0

    def _get_pipeline_age_days(self, summary: Any, now: datetime) -> float:
        """计算管道的年龄（天）。

        Args:
            summary: PipelineRunSummary
            now: 当前时间

        Returns:
            年龄（天）
        """
        try:
            created = datetime.fromisoformat(summary.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            return (now - created).total_seconds() / 86400
        except (ValueError, TypeError):
            return 0.0

    # ============================================
    # 复盘状态双源读取
    # ============================================

    async def _get_review_status(self, pipeline_id: str) -> str:
        """双源读取复盘状态。

        L0 summary 优先，L0 不存在则读 L1 块元数据。

        Args:
            pipeline_id: 管道运行 ID

        Returns:
            复盘状态："pending"、"completed" 或 "deleted"
        """
        # 优先从 L0 summary 读取
        summary = self._storage.get_summary(pipeline_id)
        if summary is not None:
            return getattr(summary, "review_status", "pending") or "pending"

        # L0 已删，从 L1 块读取
        try:
            chunks = await self._chunk_db.find_by_pipeline(pipeline_id)
            if chunks:
                extra = getattr(chunks[0], "extra_data", None)
                if isinstance(extra, dict):
                    return extra.get("review_status", "pending")
                return "pending"
        except Exception:
            pass

        # L0 和 L1 都不存在
        return "deleted"

    # ============================================
    # 分层删除
    # ============================================

    async def _delete_pipeline_data(
        self,
        pipeline_id: str,
        delete_l1: bool = False,
    ) -> dict[str, Any]:
        """分层删除管道数据。

        第一步：必删 L0（最大，最优先）
        第二步：看条件删 L1
        第三步：处理 Episode

        Args:
            pipeline_id: 管道运行 ID
            delete_l1: 是否同时删除 L1 压缩块

        Returns:
            删除结果字典
        """
        result: dict[str, Any] = {
            "l0_deleted": 0,
            "l1_deleted": 0,
            "episodes_deleted": 0,
        }

        # 第一步：删 L0 YAML 文件
        l0_count = self._storage.delete_by_session(pipeline_id)
        result["l0_deleted"] = l0_count

        # 第二步：按条件删 L1
        if delete_l1:
            try:
                chunks = await self._chunk_db.find_by_pipeline(pipeline_id)
                for chunk in chunks:
                    if chunk.layer == "L1":
                        await self._chunk_db.delete(chunk.id)
                        result["l1_deleted"] += 1
            except Exception as e:
                logger.warning(
                    "[Maintenance] 删除 L1 块失败 | pipeline=%s | error=%s",
                    pipeline_id[:12],
                    e,
                )

        # 第三步：处理 Episode（如果有 memory_service）
        if self._memory_service:
            try:
                episode_service = self._memory_service._episode_service
                if episode_service:
                    # 简单处理：删除与该管道关联的 Episode
                    if hasattr(episode_service, "_storage") and episode_service._storage:
                        episodes = await episode_service._storage.find_by_user(
                            "__all__",
                            limit=1000000,
                            offset=0,
                        )
                        for ep in episodes:
                            if getattr(ep, "session_id", "") == pipeline_id:
                                await episode_service._storage.delete(ep.id)
                                result["episodes_deleted"] += 1
                    elif hasattr(episode_service, "_in_memory"):
                        to_delete = [
                            eid
                            for eid, ep in episode_service._in_memory.items()
                            if getattr(ep, "session_id", "") == pipeline_id
                        ]
                        for eid in to_delete:
                            del episode_service._in_memory[eid]
                            result["episodes_deleted"] += 1
            except Exception as e:
                logger.debug(
                    "[Maintenance] Episode 清理失败（非致命）: %s",
                    e,
                )

        logger.debug(
            "[Maintenance] 删除管道数据 | pipeline=%s | L0=%d | L1=%d | episodes=%d",
            pipeline_id[:12],
            result["l0_deleted"],
            result["l1_deleted"],
            result["episodes_deleted"],
        )
        return result

    # ============================================
    # 索引重建
    # ============================================

    async def rebuild_index(self) -> dict[str, Any]:  # noqa: PLR0912,PLR0915
        """重建向量索引。

        从内容存储中读取所有记忆条目，重新生成嵌入向量
        并写入向量索引表。清理后按需调用。

        Returns:
            重建结果字典
        """
        datetime.now(UTC)
        result: dict[str, Any] = {
            "status": "success",
            "reindexed_episodes": 0,
            "reindexed_knowledge": 0,
            "errors": [],
        }

        if not self._memory_service:
            result["status"] = "skipped"
            result["reason"] = "memory_service not available"
            return result

        embedding_service = self._memory_service._embedding_service
        vector_retriever = self._memory_service._vector_retriever

        if not embedding_service:
            result["status"] = "skipped"
            result["reason"] = "embedding_service not available"
            return result

        if not vector_retriever or not hasattr(vector_retriever, "save_index"):
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
                    "__all__",
                    limit=1000000,
                    offset=0,
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
                    "__all__",
                    limit=1000000,
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

        logger.info(
            "[Maintenance] 索引重建完成 | episodes=%d | knowledge=%d",
            result["reindexed_episodes"],
            result["reindexed_knowledge"],
        )
        return result
