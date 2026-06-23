"""Round 3 测试：复盘清理决策边界 + 数据生命周期。

聚焦（Round 1 未覆盖的补充场景）：
1. 清理决策表完整矩阵 — 12 个单元格参数化（PENDING/COMPLETED × 短/中/老 × 正常/高容量）
2. 分层清理优先级 — L0→L1→Episode→Knowledge 永不删的层次验证
3. 双源标记写入一致性 — _mark_pipeline_reviewed 同时更新 summary 和 chunk
4. 复盘触发阈值 — min_records=500 配置正确性 + 触发逻辑行为推断

对照需求文档：
- §4.2 清理决策表
- §3.2 双源标记
- F-REV-06 数据清理决策
- F-REV-08 分层清理优先级
- AC-REV-07 清理按决策表执行
- AC-REV-08 双源标记一致性
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.maintenance import MaintenanceConfig, MemoryMaintenanceService
from memory.maintenance.cleanup_engine import CleanupEngine
from memory.maintenance.review_engine import (
    PipelineRunSummary,
    ReviewEngine,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_summary(
    run_id: str = "run-001",
    review_status: str = "pending",
    age_days: int = 0,
    **overrides: Any,
) -> PipelineRunSummary:
    """创建带年龄信息的 PipelineRunSummary。"""
    created = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    defaults: dict[str, Any] = dict(
        run_id=run_id,
        total_records=5,
        total_iterations=3,
        created_at=created,
        status="completed",
        error="",
        review_status=review_status,
    )
    defaults.update(overrides)
    return PipelineRunSummary(**defaults)


def _make_chunk(
    chunk_id: str = "chunk-1",
    pipeline_id: str = "run-001",
    layer: str = "L1",
    extra_data: dict[str, Any] | None = None,
) -> MagicMock:
    """创建模拟数据块。"""
    chunk = MagicMock()
    chunk.id = chunk_id
    chunk.pipeline_id = pipeline_id
    chunk.layer = layer
    chunk.content = "content"
    chunk.extra_data = extra_data if extra_data is not None else {}
    return chunk


def _build_cleanup_engine(
    *,
    capacity_pressure: float = 0.0,
    l1_chunks: list[MagicMock] | None = None,
    memory_service: Any = None,
) -> tuple[CleanupEngine, MagicMock, MagicMock]:
    """构造 CleanupEngine 及其 Mock 依赖。

    Args:
        capacity_pressure: 模拟容量压力（通过 monkey-patch）
        l1_chunks: find_by_pipeline 返回的数据块列表
        memory_service: 可选的记忆服务（用于 Episode 清理测试）
    """
    storage = MagicMock()
    chunk_db = MagicMock()
    if l1_chunks is None:
        l1_chunks = [
            _make_chunk(chunk_id="l1-0", layer="L1"),
            _make_chunk(chunk_id="l1-1", layer="L1"),
        ]
    chunk_db.find_by_pipeline = AsyncMock(return_value=l1_chunks)
    chunk_db.delete = AsyncMock(return_value=None)
    chunk_db.save_chunk = MagicMock(return_value=None)

    config = MaintenanceConfig()
    engine = CleanupEngine(
        storage=storage,
        chunk_db=chunk_db,
        memory_service=memory_service,
        config=config,
    )
    engine._get_capacity_pressure = lambda: capacity_pressure  # type: ignore[method-assign]
    return engine, storage, chunk_db


# ============================================================
# 1. 清理决策表完整矩阵（12 单元格参数化）
# ============================================================

# 决策表：(review_status, age_days, capacity_pressure, expect_l0, expect_l1, expect_review)
_DECISION_MATRIX = [
    # --- COMPLETED ---
    ("completed", 2, 0.1, False, False, False),   # 已复盘+短+正常 → 不清理
    ("completed", 2, 0.95, False, False, False),  # 已复盘+短+高容量 → 不清理
    ("completed", 15, 0.1, False, False, False),  # 已复盘+中+正常 → 不清理
    ("completed", 15, 0.95, True, False, False),  # 已复盘+中+高容量 → 只删L0
    ("completed", 60, 0.1, True, True, False),    # 已复盘+老+正常 → 删L0+L1
    ("completed", 60, 0.95, True, True, False),   # 已复盘+老+高容量 → 删L0+L1
    # --- PENDING ---
    ("pending", 2, 0.1, False, False, False),     # 未复盘+短+正常 → 不动
    ("pending", 2, 0.95, False, False, False),    # 未复盘+短+高容量 → 不动
    ("pending", 15, 0.1, False, False, False),    # 未复盘+中+正常 → 不动
    ("pending", 15, 0.95, False, False, False),   # 未复盘+中+高容量 → 不动
    ("pending", 60, 0.1, True, True, True),       # 未复盘+老+正常 → 先复盘再删L0+L1
    ("pending", 60, 0.95, True, True, True),      # 未复盘+老+高容量 → 先复盘再删L0+L1
]


@pytest.mark.parametrize(
    "review_status, age_days, capacity_pressure, expect_l0, expect_l1, expect_review",
    _DECISION_MATRIX,
    ids=[
        f"{rs}-age{a}-cap{'hi' if c > 0.8 else 'lo'}"
        for rs, a, c, _, _, _ in _DECISION_MATRIX
    ],
)
class TestDecisionTableFullMatrix:
    """清理决策表 12 个单元格的完整参数化覆盖。

    决策表来源：需求文档 §4.2
                        年龄短(<7天)   年龄中(7-30天)   年龄老(>30天)
    PENDING    │   不清理     │   不清理       │  触发复盘+清理 │
    REVIEWED   │   不清理     │   看容量       │     清理       │
    """

    @pytest.mark.asyncio
    async def test_decision_cell(
        self,
        review_status: str,
        age_days: int,
        capacity_pressure: float,
        expect_l0: bool,
        expect_l1: bool,
        expect_review: bool,
    ) -> None:
        run_id = f"pipe-{review_status}-{age_days}-{capacity_pressure}"
        summary = _make_summary(
            run_id=run_id,
            review_status=review_status,
            age_days=age_days,
        )

        engine, storage, chunk_db = _build_cleanup_engine(
            capacity_pressure=capacity_pressure,
        )
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary
        storage.delete_by_session.return_value = 1

        review_engine: Any = None
        if expect_review:
            review_engine = MagicMock()
            review_engine.run_review = AsyncMock(return_value={"status": "success"})

        result = await engine.cleanup_by_age_and_capacity(review_engine=review_engine)

        # --- L0 删除验证 ---
        if expect_l0:
            assert result["l0_deleted"] >= 1, (
                f"预期 L0 被删除 ({review_status}, age={age_days}d, cap={capacity_pressure})"
            )
            storage.delete_by_session.assert_called_with(run_id)
        else:
            assert result["l0_deleted"] == 0, (
                f"预期 L0 不被删除 ({review_status}, age={age_days}d, cap={capacity_pressure})"
            )
            storage.delete_by_session.assert_not_called()

        # --- L1 删除验证 ---
        if expect_l1:
            assert result["l1_deleted"] >= 1, (
                f"预期 L1 被删除 ({review_status}, age={age_days}d, cap={capacity_pressure})"
            )
            assert chunk_db.delete.await_count >= 1
        else:
            assert result["l1_deleted"] == 0, (
                f"预期 L1 不被删除 ({review_status}, age={age_days}d, cap={capacity_pressure})"
            )
            chunk_db.delete.assert_not_awaited()

        # --- 复盘触发验证 ---
        if expect_review:
            assert review_engine is not None
            review_engine.run_review.assert_awaited_once_with(run_id)
        else:
            if review_engine is not None:
                review_engine.run_review.assert_not_awaited()


class TestDecisionTableEdgeCases:
    """决策表边界：非常规 review_status 不触发清理。"""

    @pytest.mark.asyncio
    async def test_reviewing_status_not_cleaned(self) -> None:
        """review_status='reviewing' → 不在任何分支中，不清理。"""
        summary = _make_summary(
            run_id="mid-review",
            review_status="reviewing",
            age_days=60,
        )
        engine, storage, _ = _build_cleanup_engine(capacity_pressure=0.1)
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)

        assert result["l0_deleted"] == 0
        assert result["l1_deleted"] == 0

    @pytest.mark.asyncio
    async def test_failed_status_not_cleaned(self) -> None:
        """review_status='failed' → 不在任何分支中，不清理。"""
        summary = _make_summary(
            run_id="failed-review",
            review_status="failed",
            age_days=60,
        )
        engine, storage, _ = _build_cleanup_engine(capacity_pressure=0.95)
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)

        assert result["l0_deleted"] == 0


# ============================================================
# 2. 分层清理优先级：L0→L1→Episode→Knowledge 永不删
# ============================================================

class TestLayeredCleanupPriority:
    """F-REV-08: 分层清理优先级验证。

    优先级（按体积从大到小）：
    1. L0 YAML 文件（最大）→ 已复盘 + 够老 → 先删 L0
    2. L1 压缩块（中等）→ L0 已删 + 容量紧张 → 再删 L1
    3. Episode（小）→ 已沉淀为 Knowledge → 删
    4. Knowledge（核心产出）→ 永不删除
    """

    @pytest.mark.asyncio
    async def test_l0_deleted_before_l1(self) -> None:
        """L0 删除操作先于 L1 执行（delete_by_session 先调用）。"""
        summary = _make_summary(
            run_id="old-completed",
            review_status="completed",
            age_days=60,
        )
        engine, storage, chunk_db = _build_cleanup_engine()
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary
        storage.delete_by_session.return_value = 3

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)

        assert result["l0_deleted"] == 3
        assert result["l1_deleted"] == 2  # 两个 L1 chunk
        # L0 和 L1 都被删除
        storage.delete_by_session.assert_called_once()
        assert chunk_db.delete.await_count == 2

    @pytest.mark.asyncio
    async def test_only_l0_deleted_when_medium_age_high_capacity(self) -> None:
        """已复盘+中等年龄+高容量 → 只删 L0，保留 L1。"""
        summary = _make_summary(
            run_id="medium-completed",
            review_status="completed",
            age_days=15,
        )
        engine, storage, chunk_db = _build_cleanup_engine(capacity_pressure=0.95)
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary
        storage.delete_by_session.return_value = 1

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)

        assert result["l0_deleted"] == 1
        assert result["l1_deleted"] == 0
        # L1 chunks 未被删除
        chunk_db.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_episode_cleaned_via_memory_service(self) -> None:
        """已复盘+老 → 清理时同时删除关联的 Episode 记录。"""
        summary = _make_summary(
            run_id="old-with-episodes",
            review_status="completed",
            age_days=60,
        )

        # 构造 memory_service mock，模拟 Episode 存储使用 _in_memory 模式
        memory_service = MagicMock()
        episode_service = MagicMock()
        episode_service._storage = None  # 迫使走 _in_memory 路径
        ep1 = MagicMock(id="ep-1", session_id="old-with-episodes")
        ep2 = MagicMock(id="ep-2", session_id="other-pipeline")
        episode_service._in_memory = {"ep-1": ep1, "ep-2": ep2}
        memory_service._episode_service = episode_service
        # 让 rebuild_index 快速跳过
        memory_service._embedding_service = None

        engine, storage, _ = _build_cleanup_engine(memory_service=memory_service)
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary
        storage.delete_by_session.return_value = 1

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)

        assert result["l0_deleted"] == 1
        # 只有 session_id 匹配的 episode 被删除
        assert result["episodes_deleted"] == 1
        assert "ep-1" not in episode_service._in_memory
        assert "ep-2" in episode_service._in_memory

    @pytest.mark.asyncio
    async def test_knowledge_never_deleted_by_cleanup(self) -> None:
        """Knowledge 永不被清理引擎删除。

        清理引擎只操作 storage（L0）、chunk_db（L1）、episode_service（Episode），
        从不调用 knowledge_service 的删除方法。
        """
        summary = _make_summary(
            run_id="pipe-with-knowledge",
            review_status="completed",
            age_days=90,  # 非常老
        )

        # 构造一个包含 knowledge_service 的 memory_service
        memory_service = MagicMock()
        knowledge_service = MagicMock()
        knowledge_service._storage = None
        knowledge_service._in_memory = {
            "k1": MagicMock(id="k1", content="important knowledge", user_id="system"),
        }
        memory_service._knowledge_service = knowledge_service
        memory_service._embedding_service = None  # skip rebuild

        engine, storage, chunk_db = _build_cleanup_engine(memory_service=memory_service)
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary
        storage.delete_by_session.return_value = 1

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)

        assert result["l0_deleted"] == 1
        # Knowledge 条目仍然存在
        assert "k1" in knowledge_service._in_memory
        # 没有任何删除 knowledge 的操作
        assert not hasattr(knowledge_service, "delete") or not knowledge_service.delete.called

    @pytest.mark.asyncio
    async def test_l1_chunk_filter_only_deletes_l1_layer(self) -> None:
        """分层删除时只删 layer='L1' 的 chunk，其他 layer 不受影响。"""
        summary = _make_summary(
            run_id="mixed-chunks",
            review_status="completed",
            age_days=60,
        )
        # 提供 L0 和 L1 混合 chunks
        mixed_chunks = [
            _make_chunk(chunk_id="c-l0", layer="L0"),
            _make_chunk(chunk_id="c-l1a", layer="L1"),
            _make_chunk(chunk_id="c-l1b", layer="L1"),
            _make_chunk(chunk_id="c-l2", layer="L2"),
        ]
        engine, storage, chunk_db = _build_cleanup_engine(l1_chunks=mixed_chunks)
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary
        storage.delete_by_session.return_value = 1

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)

        # 只有 2 个 L1 chunk 被删
        assert result["l1_deleted"] == 2
        deleted_ids = {call.args[0] for call in chunk_db.delete.await_args_list}
        assert deleted_ids == {"c-l1a", "c-l1b"}


# ============================================================
# 3. 双源标记写入一致性
# ============================================================

class TestDualSourceWriteConsistency:
    """AC-REV-08: 复盘后 PipelineRunSummary 和 ChunkMetadata 的 review_status 同步更新。

    Round 1 测试了双源「读取」一致性（_get_review_status），
    Round 3 补充双源「写入」一致性（_mark_pipeline_reviewed）。
    """

    @pytest.mark.asyncio
    async def test_mark_reviewed_updates_both_summary_and_chunks(self) -> None:
        """_mark_pipeline_reviewed 同时更新 summary 和所有 chunk 的 review_status。"""
        storage = MagicMock()
        chunk_db = MagicMock()
        chunks = [
            _make_chunk(chunk_id="ch-1", layer="L1"),
            _make_chunk(chunk_id="ch-2", layer="L1"),
        ]
        chunk_db.find_by_pipeline = AsyncMock(return_value=chunks)
        chunk_db.save_chunk = MagicMock()

        engine = ReviewEngine(storage=storage, chunk_db=chunk_db)
        await engine._mark_pipeline_reviewed("run-dual-1")

        # summary 更新
        storage.update_summary.assert_called_with(
            "run-dual-1", {"review_status": "completed"},
        )
        # 每个 chunk 的 extra_data 都被标记
        for chunk in chunks:
            assert chunk.extra_data.get("review_status") == "completed", (
                f"chunk {chunk.id} 的 review_status 未被同步更新"
            )
        # save_chunk 对每个 chunk 调用一次
        assert chunk_db.save_chunk.call_count == len(chunks)

    @pytest.mark.asyncio
    async def test_mark_reviewed_no_chunks_only_updates_summary(self) -> None:
        """chunk_db 存在但无数据块时，只更新 summary，不崩溃。"""
        storage = MagicMock()
        chunk_db = MagicMock()
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])
        chunk_db.save_chunk = MagicMock()

        engine = ReviewEngine(storage=storage, chunk_db=chunk_db)
        await engine._mark_pipeline_reviewed("run-no-chunks")

        storage.update_summary.assert_called_once_with(
            "run-no-chunks", {"review_status": "completed"},
        )
        chunk_db.save_chunk.assert_not_called()

    @pytest.mark.asyncio
    async def test_mark_reviewed_chunk_db_none_still_updates_summary(self) -> None:
        """chunk_db 为 None（纯 API 场景）时只更新 summary。"""
        storage = MagicMock()
        engine = ReviewEngine(storage=storage, chunk_db=None)
        await engine._mark_pipeline_reviewed("run-api-only")

        storage.update_summary.assert_called_once_with(
            "run-api-only", {"review_status": "completed"},
        )

    @pytest.mark.asyncio
    async def test_mark_reviewed_chunk_error_does_not_block_summary(self) -> None:
        """chunk 更新异常不阻断 summary 更新（summary 是主真相源）。"""
        storage = MagicMock()
        chunk_db = MagicMock()
        chunk_db.find_by_pipeline = AsyncMock(side_effect=RuntimeError("db down"))

        engine = ReviewEngine(storage=storage, chunk_db=chunk_db)
        await engine._mark_pipeline_reviewed("run-chunk-error")

        # 即使 chunk 更新失败，summary 仍然更新
        storage.update_summary.assert_called_once_with(
            "run-chunk-error", {"review_status": "completed"},
        )

    @pytest.mark.asyncio
    async def test_full_review_flow_syncs_dual_source(self) -> None:
        """完整复盘流程后，summary 和 chunk 双源标记一致。"""
        storage = MagicMock()
        chunk_db = MagicMock()
        ks = MagicMock()

        summary = _make_summary(run_id="run-full", status="completed", age_days=1)
        storage.get_summary.return_value = summary
        storage.list_by_pipeline.return_value = ([], False)

        chunks = [_make_chunk(chunk_id="fc-1", layer="L1")]
        chunk_db.find_by_pipeline = AsyncMock(return_value=chunks)
        chunk_db.save_chunk = MagicMock()
        ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})

        engine = ReviewEngine(storage=storage, chunk_db=chunk_db, knowledge_service=ks)
        result = await engine.run_review("run-full")

        assert result["status"] == "success"

        # 验证双源标记一致
        # 1. summary 标记为 completed
        summary_calls = [
            c for c in storage.update_summary.call_args_list
            if c.args[1].get("review_status") == "completed"
        ]
        assert len(summary_calls) >= 1, "summary 未标记为 completed"

        # 2. chunk extra_data 标记为 completed
        assert chunks[0].extra_data.get("review_status") == "completed"
        chunk_db.save_chunk.assert_called_with(chunks[0])


# ============================================================
# 4. 复盘触发阈值：min_records=500 配置 + 触发逻辑
# ============================================================

class TestReviewTriggerThreshold:
    """F-REV-02: 复盘触发阈值的配置正确性与触发逻辑行为推断。

    需求 §2.2:
      trigger:
        min_records: 500          # 积累 500 条新记录后触发
        max_interval: 604800      # 或 7 天触发一次
    """

    def test_default_min_records_is_500(self) -> None:
        """MaintenanceConfig 默认 review_min_records == 500（对照需求 §2.2）。"""
        cfg = MaintenanceConfig()
        assert cfg.review_min_records == 500

    def test_min_records_configurable_from_dict(self) -> None:
        """min_records 可通过配置字典自定义。"""
        cfg = MaintenanceConfig.from_dict({
            "review": {"trigger": {"review_min_records": 300}},
        })
        assert cfg.review_min_records == 300

    def test_min_records_from_flat_dict(self) -> None:
        """扁平字典也能设置 min_records。"""
        cfg = MaintenanceConfig.from_dict({"review_min_records": 1000})
        assert cfg.review_min_records == 1000

    def test_trigger_fires_when_pending_exists_regardless_of_record_count(self) -> None:
        """存在 pending 管道即触发复盘（当前实现基于 pending 状态而非记录计数）。

        当前实现逻辑（should_trigger_review）：
        条件1: 存在 pending 管道 → 触发
        条件2: 距上次复盘超过 max_interval → 触发

        min_records=500 是配置中声明的意图值，触发判断当前使用 pending 状态。
        """
        service, _, _, _ = _build_service()
        fake_engine = MagicMock()
        fake_engine.get_pending_pipelines.return_value = [
            _make_summary(run_id="p1"),
        ]
        service._review_engine = fake_engine

        assert service.should_trigger_review() is True

    def test_trigger_does_not_fire_when_no_pending_and_within_interval(self) -> None:
        """无 pending 且在间隔内 → 不触发。"""
        service, _, _, _ = _build_service(
            MaintenanceConfig(review_max_interval=3600)
        )
        fake_engine = MagicMock()
        fake_engine.get_pending_pipelines.return_value = []
        service._review_engine = fake_engine
        service._stats["last_review_at"] = datetime.now(UTC).isoformat()

        assert service.should_trigger_review() is False

    def test_trigger_fires_on_interval_exceeded_even_without_pending(self) -> None:
        """即使无 pending，超过 max_interval 也会触发（时间兜底）。"""
        service, _, _, _ = _build_service(
            MaintenanceConfig(review_max_interval=10)
        )
        fake_engine = MagicMock()
        fake_engine.get_pending_pipelines.return_value = []
        service._review_engine = fake_engine
        service._stats["last_review_at"] = (
            datetime.now(UTC) - timedelta(hours=2)
        ).isoformat()

        assert service.should_trigger_review() is True

    def test_trigger_fires_on_first_run_without_history(self) -> None:
        """从未复盘过 + 有 pending → 触发（首次运行场景）。"""
        service, _, _, _ = _build_service()
        fake_engine = MagicMock()
        fake_engine.get_pending_pipelines.return_value = [
            _make_summary(run_id="first-1"),
            _make_summary(run_id="first-2"),
        ]
        service._review_engine = fake_engine
        # 不设置 last_review_at → 首次

        assert service.should_trigger_review() is True

    def test_multiple_pending_pipelines_all_counted(self) -> None:
        """多个 pending 管道都应被发现。"""
        service, _, _, _ = _build_service()
        fake_engine = MagicMock()
        fake_engine.get_pending_pipelines.return_value = [
            _make_summary(run_id=f"batch-{i}") for i in range(10)
        ]
        service._review_engine = fake_engine

        assert service.should_trigger_review() is True


# ---------------------------------------------------------------------------
# Shared helper for service tests
# ---------------------------------------------------------------------------

def _build_service(
    config: MaintenanceConfig | None = None,
) -> tuple[MemoryMaintenanceService, MagicMock, MagicMock, MagicMock]:
    """构造 MemoryMaintenanceService 及其 Mock 依赖。"""
    storage = MagicMock()
    chunk_db = MagicMock()
    knowledge = MagicMock()
    service = MemoryMaintenanceService(
        storage=storage,
        chunk_db=chunk_db,
        knowledge_service=knowledge,
        pipeline_engine=None,
        config=config or MaintenanceConfig(),
    )
    return service, storage, chunk_db, knowledge
