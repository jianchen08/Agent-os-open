"""记忆与复盘模块补充测试。

对照 docs/requirements/各模块需求文档/08_记忆与复盘系统模块需求文档.md，
覆盖现有测试尚未覆盖的验收标准与行为契约。

修复的 bug 回归：
1. cleanup_engine.py 调用不存在的 review_engine._review_single_pipeline
   → 修复为 review_engine.run_review(run_id)
2. log_parser.py 使用绝对 import from src.memory.maintenance...
   → 修复为相对导入 from .review_engine import ...
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.maintenance import MaintenanceConfig, MemoryMaintenanceService
from memory.maintenance.cleanup_engine import CleanupEngine
from memory.maintenance.log_parser import PipelineLogParser
from memory.maintenance.review_engine import (
    ErrorRecord,
    ExecutionRecord,
    Pipeline,
    PipelineRunSummary,
    ReviewEngine,
    ReviewStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_summary(
    run_id: str = "run-001",
    status: str = "completed",
    review_status: str = "pending",
    created_at: str | None = None,
    **overrides: Any,
) -> PipelineRunSummary:
    """创建 PipelineRunSummary 测试 fixture。"""
    defaults: dict[str, Any] = dict(
        run_id=run_id,
        total_records=5,
        total_iterations=3,
        created_at=created_at or "2026-01-01T00:00:00+00:00",
        status=status,
        error="",
        review_status=review_status,
    )
    defaults.update(overrides)
    return PipelineRunSummary(**defaults)


def _make_record(
    iteration: int = 1,
    rtype: str = "tool",
    name: str = "step_a",
    error: str = "",
    content: str = "result",
) -> ExecutionRecord:
    """创建 ExecutionRecord 测试 fixture。"""
    return ExecutionRecord(
        iteration=iteration,
        type=rtype,
        name=name,
        error=error,
        content=content,
    )


# ============================================================
# AC-REV-06: 复盘后管道标记 REVIEWED
# ============================================================


class TestPipelineMarkedReviewed:
    """复盘成功后，必须调用 storage.update_summary 把 review_status 设为 completed。"""

    @pytest.mark.asyncio
    async def test_AC_REV_06_pipeline_marked_completed_after_success(self) -> None:
        """成功复盘后 update_summary 被调用，传入 review_status=completed。"""
        storage = MagicMock()
        chunk_db = MagicMock()
        ks = MagicMock()
        storage.get_summary.return_value = _make_summary(run_id="r1")
        storage.list_by_pipeline.return_value = (
            [_make_record(name="step_a", error="timeout")],
            False,
        )
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])
        ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})
        ks.create_knowledge = AsyncMock(return_value={"id": "k-1"})

        engine = ReviewEngine(storage=storage, chunk_db=chunk_db, knowledge_service=ks)
        result = await engine.run_review("r1")

        assert result["status"] == "success"
        calls = storage.update_summary.call_args_list
        # 最后一次 update_summary 必须写入 review_status=completed
        last_review_status = calls[-1].args[1].get("review_status")
        assert last_review_status == "completed", (
            f"复盘成功后应标记 completed，实际: {last_review_status}"
        )

    @pytest.mark.asyncio
    async def test_AC_REV_06_pipeline_marked_failed_on_exception(self) -> None:
        """复盘内部异常时 update_summary(review_status=failed)。"""
        storage = MagicMock()
        chunk_db = MagicMock()
        ks = MagicMock()
        storage.get_summary.return_value = _make_summary(run_id="r1")
        # 注入异常：list_by_pipeline 抛错
        storage.list_by_pipeline.side_effect = RuntimeError("storage broken")

        engine = ReviewEngine(storage=storage, chunk_db=chunk_db, knowledge_service=ks)
        result = await engine.run_review("r1")

        assert result["status"] == "error"
        calls = storage.update_summary.call_args_list
        assert any(
            c.args[1].get("review_status") == "failed" for c in calls
        ), "异常时应把 review_status 置为 failed"

    @pytest.mark.asyncio
    async def test_AC_REV_06_reviewing_set_before_processing(self) -> None:
        """复盘开始时先把 review_status 置为 reviewing。"""
        storage = MagicMock()
        chunk_db = MagicMock()
        ks = MagicMock()
        storage.get_summary.return_value = _make_summary(run_id="r1")
        storage.list_by_pipeline.return_value = ([], False)
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])
        ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})

        engine = ReviewEngine(storage=storage, chunk_db=chunk_db, knowledge_service=ks)
        await engine.run_review("r1")

        first_call = storage.update_summary.call_args_list[0]
        assert first_call.args[0] == "r1"
        assert first_call.args[1].get("review_status") == "reviewing"


# ============================================================
# AC-REV-07: 清理决策表
# ============================================================


def _build_cleanup_engine(
    *,
    capacity_pressure: float = 0.0,
    config_overrides: dict[str, Any] | None = None,
) -> tuple[CleanupEngine, MagicMock, MagicMock]:
    """构造 CleanupEngine 及其依赖。

    capacity_pressure: 通过 monkey-patch _get_capacity_pressure 模拟容量压力。
    """
    storage = MagicMock()
    chunk_db = MagicMock()
    chunk_db.find_by_pipeline = AsyncMock(return_value=[])
    chunk_db.delete = AsyncMock(return_value=None)

    config = MaintenanceConfig()
    if config_overrides:
        for k, v in config_overrides.items():
            setattr(config, k, v)

    engine = CleanupEngine(
        storage=storage,
        chunk_db=chunk_db,
        memory_service=None,
        config=config,
    )
    engine._get_capacity_pressure = lambda: capacity_pressure  # type: ignore[method-assign]
    return engine, storage, chunk_db


class TestCleanupDecisionTable:
    """AC-REV-07: 清理按 复盘状态 × 年龄 × 容量压力 综合判断。"""

    @pytest.mark.asyncio
    async def test_AC_REV_07_completed_old_pipeline_deletes_l0_and_l1(self) -> None:
        """已复盘 + 年龄 > 30 天 → 删 L0 和 L1。"""
        engine, storage, _ = _build_cleanup_engine()
        old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        summary = _make_summary(
            run_id="old-completed",
            status="completed",
            review_status="completed",
            created_at=old_ts,
        )
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary
        storage.delete_by_session.return_value = 3

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)

        assert result["l0_deleted"] == 3
        storage.delete_by_session.assert_called_once_with("old-completed")

    @pytest.mark.asyncio
    async def test_AC_REV_07_completed_young_pipeline_not_deleted(self) -> None:
        """已复盘 + 年龄 < 7 天 → 不清理。"""
        engine, storage, _ = _build_cleanup_engine()
        young_ts = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        summary = _make_summary(
            run_id="young-completed",
            review_status="completed",
            created_at=young_ts,
        )
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)

        assert result["l0_deleted"] == 0
        storage.delete_by_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_AC_REV_07_completed_medium_with_capacity_pressure_deletes_l0(self) -> None:
        """已复盘 + 中等年龄 (7-30 天) + 容量紧张 (>0.8) → 只删 L0。"""
        engine, storage, _ = _build_cleanup_engine(capacity_pressure=0.95)
        ts = (datetime.now(UTC) - timedelta(days=15)).isoformat()
        summary = _make_summary(
            run_id="medium-completed",
            review_status="completed",
            created_at=ts,
        )
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary
        storage.delete_by_session.return_value = 1

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)

        # 中等年龄 + 容量压力 → 只删 L0，不删 L1
        assert result["l0_deleted"] == 1
        assert result["l1_deleted"] == 0

    @pytest.mark.asyncio
    async def test_AC_REV_07_completed_medium_no_capacity_pressure_keeps(self) -> None:
        """已复盘 + 中等年龄 (7-30 天) + 容量充足 → 不清理。"""
        engine, storage, _ = _build_cleanup_engine(capacity_pressure=0.1)
        ts = (datetime.now(UTC) - timedelta(days=15)).isoformat()
        summary = _make_summary(
            run_id="medium-completed-relaxed",
            review_status="completed",
            created_at=ts,
        )
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)

        assert result["l0_deleted"] == 0

    @pytest.mark.asyncio
    async def test_AC_REV_07_pending_old_pipeline_triggers_review_then_cleans(self) -> None:
        """未复盘 + 年龄 > 30 天 → 先触发复盘再清理（L0+L1）。"""
        engine, storage, _ = _build_cleanup_engine()
        old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        summary = _make_summary(
            run_id="old-pending",
            review_status="pending",
            created_at=old_ts,
        )
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary
        storage.delete_by_session.return_value = 2

        review_engine = MagicMock()
        review_engine.run_review = AsyncMock(return_value={"status": "success"})

        result = await engine.cleanup_by_age_and_capacity(review_engine=review_engine)

        # 必须先复盘
        review_engine.run_review.assert_awaited_once_with("old-pending")
        # 然后删 L0
        assert result["l0_deleted"] == 2

    @pytest.mark.asyncio
    async def test_AC_REV_07_pending_young_pipeline_not_touched(self) -> None:
        """未复盘 + 年龄 < 7 天 → 不动。"""
        engine, storage, _ = _build_cleanup_engine()
        young_ts = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        summary = _make_summary(
            run_id="young-pending",
            review_status="pending",
            created_at=young_ts,
        )
        storage.list_all_summaries.return_value = [summary]
        storage.get_summary.return_value = summary

        review_engine = MagicMock()
        review_engine.run_review = AsyncMock()

        result = await engine.cleanup_by_age_and_capacity(review_engine=review_engine)

        review_engine.run_review.assert_not_called()
        storage.delete_by_session.assert_not_called()
        assert result["l0_deleted"] == 0

    @pytest.mark.asyncio
    async def test_AC_REV_07_empty_summaries_skipped(self) -> None:
        """无管道数据时直接 skipped。"""
        engine, storage, _ = _build_cleanup_engine()
        storage.list_all_summaries.return_value = []

        result = await engine.cleanup_by_age_and_capacity(review_engine=None)

        assert result["status"] == "skipped"
        assert "no pipelines" in result["reason"]


# ============================================================
# AC-REV-08: 双源标记一致性
# 复盘状态从 L0 summary 优先读取，L0 不存在则读 L1 块的 extra_data
# ============================================================


class TestDualSourceReviewStatus:
    """AC-REV-08: PipelineRunSummary + ChunkMetadata 双源标记一致性。"""

    @pytest.mark.asyncio
    async def test_AC_REV_08_l0_summary_takes_priority(self) -> None:
        """L0 summary 存在时直接返回其 review_status。"""
        engine, storage, chunk_db = _build_cleanup_engine()
        summary = _make_summary(run_id="dual-1", review_status="completed")
        storage.get_summary.return_value = summary

        status = await engine._get_review_status("dual-1")

        assert status == "completed"
        # 不应再读 L1
        chunk_db.find_by_pipeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_AC_REV_08_falls_back_to_l1_when_l0_missing(self) -> None:
        """L0 不存在时读 L1 块的 extra_data.review_status。"""
        engine, storage, chunk_db = _build_cleanup_engine()
        storage.get_summary.return_value = None

        l1_chunk = MagicMock()
        l1_chunk.extra_data = {"review_status": "completed"}
        chunk_db.find_by_pipeline = AsyncMock(return_value=[l1_chunk])

        status = await engine._get_review_status("dual-2")

        assert status == "completed"

    @pytest.mark.asyncio
    async def test_AC_REV_08_returns_deleted_when_both_missing(self) -> None:
        """L0 与 L1 均不存在时返回 deleted（已被清理）。"""
        engine, storage, chunk_db = _build_cleanup_engine()
        storage.get_summary.return_value = None
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])

        status = await engine._get_review_status("dual-3")

        assert status == "deleted"

    @pytest.mark.asyncio
    async def test_AC_REV_08_l1_without_review_status_returns_pending(self) -> None:
        """L1 存在但 extra_data 没有 review_status → pending。"""
        engine, storage, chunk_db = _build_cleanup_engine()
        storage.get_summary.return_value = None

        l1_chunk = MagicMock()
        l1_chunk.extra_data = {}
        chunk_db.find_by_pipeline = AsyncMock(return_value=[l1_chunk])

        status = await engine._get_review_status("dual-4")

        assert status == "pending"


# ============================================================
# F-REV-02: 复盘触发条件 / should_trigger_review / should_trigger_cleanup
# ============================================================


def _build_service(config: MaintenanceConfig | None = None) -> tuple[
    MemoryMaintenanceService, MagicMock, MagicMock, MagicMock
]:
    """构造 MemoryMaintenanceService 及其依赖。"""
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


class TestTriggerConditions:
    """should_trigger_review / should_trigger_cleanup 行为契约。"""

    def test_F_REV_02_should_trigger_review_when_pending_exists(self) -> None:
        """存在 pending pipeline 即应触发复盘。"""
        service, _, _, _ = _build_service()
        fake_review_engine = MagicMock()
        fake_review_engine.get_pending_pipelines.return_value = [
            _make_summary(run_id="p1"),
        ]
        service._review_engine = fake_review_engine

        assert service.should_trigger_review() is True

    def test_F_REV_02_should_not_trigger_when_no_pending_no_history(self) -> None:
        """无 pending 且无历史复盘 → 不触发。"""
        service, _, _, _ = _build_service()
        fake_review_engine = MagicMock()
        fake_review_engine.get_pending_pipelines.return_value = []
        service._review_engine = fake_review_engine

        assert service.should_trigger_review() is False

    def test_F_REV_02_should_trigger_when_interval_exceeded(self) -> None:
        """距上次复盘超过 max_interval → 触发。"""
        service, _, _, _ = _build_service(
            MaintenanceConfig(review_max_interval=10)  # 10 秒
        )
        fake_review_engine = MagicMock()
        fake_review_engine.get_pending_pipelines.return_value = []
        service._review_engine = fake_review_engine

        # 模拟上次复盘 1 小时前
        long_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        service._stats["last_review_at"] = long_ago

        assert service.should_trigger_review() is True

    def test_F_REV_02_should_not_trigger_when_interval_not_exceeded(self) -> None:
        """距上次复盘未超过 max_interval → 不触发。"""
        service, _, _, _ = _build_service(
            MaintenanceConfig(review_max_interval=3600)
        )
        fake_review_engine = MagicMock()
        fake_review_engine.get_pending_pipelines.return_value = []
        service._review_engine = fake_review_engine

        recent = datetime.now(UTC).isoformat()
        service._stats["last_review_at"] = recent

        assert service.should_trigger_review() is False

    def test_F_REV_06_should_trigger_cleanup_on_first_run(self) -> None:
        """从未清理过 → 触发清理。"""
        service, _, _, _ = _build_service()
        assert service.should_trigger_cleanup() is True

    def test_F_REV_06_should_not_trigger_cleanup_when_recent(self) -> None:
        """距上次清理未超过 cleanup_check_interval → 不触发。"""
        service, _, _, _ = _build_service(
            MaintenanceConfig(cleanup_check_interval=3600)
        )
        service._stats["last_cleanup_at"] = datetime.now(UTC).isoformat()
        assert service.should_trigger_cleanup() is False


# ============================================================
# F-REV-05: 经验内容构造 (_build_experience_content)
# 验证经验产出的三档完整度，让经验可读
# ============================================================


class TestExperienceContentBuilding:
    """经验内容构造的三档完整度（有 agent / 有 task / 都没有）。"""

    def test_F_REV_05_full_info_with_agent_and_task(self) -> None:
        """最完整：agent + status + 轮数 + 时长 + 时间 + 任务 + 来源 + 错误。"""
        content = ReviewEngine._build_experience_content(
            run_id="run-abc",
            status="failed",
            error="timeout occurred",
            task="测试评估指标",
            iterations=5,
            duration=30.0,
            created_at="2026-01-01T10:30:00",
            source_name="step_eval",
            agent="solution_planning_agent",
        )
        assert "solution_planning_agent" in content
        assert "failed" in content
        assert "5轮" in content
        assert "30s" in content
        assert "2026-01-01T10:30" in content
        assert '任务: "测试评估指标"' in content
        assert "[step_eval]" in content
        assert "timeout occurred" in content
        assert "pipeline=run-abc" in content

    def test_F_REV_05_long_duration_shown_as_minutes(self) -> None:
        """时长超过 60 秒以分钟显示。"""
        content = ReviewEngine._build_experience_content(
            run_id="r1",
            status="completed",
            error="e",
            duration=120.0,
        )
        # 2.0min
        assert "2.0min" in content

    def test_F_REV_05_no_agent_falls_back_to_task(self) -> None:
        """没有 agent 时，任务描述兜底。"""
        content = ReviewEngine._build_experience_content(
            run_id="r2",
            status="failed",
            error="err",
            task="设计容器",
        )
        assert '任务: "设计容器"' in content
        # 无 agent 时不应出现 agent 字段
        assert "solution_planning_agent" not in content

    def test_F_REV_05_minimal_only_status_and_error(self) -> None:
        """最少信息：仅状态+错误+pipeline。"""
        content = ReviewEngine._build_experience_content(
            run_id="r3",
            status="failed",
            error="something broke",
        )
        assert "failed" in content
        assert "something broke" in content
        assert "pipeline=r3" in content

    def test_F_REV_05_extract_task_description_from_user_record(self) -> None:
        """从 user 类型的执行记录中提取首条 user 消息作为任务描述。"""
        records = [
            ExecutionRecord(iteration=0, type="system", name="", error="", content="sys"),
            ExecutionRecord(iteration=1, type="user", name="", error="", content="请帮我写一个测试"),
            ExecutionRecord(iteration=2, type="tool", name="", error="", content="..."),
        ]
        desc = ReviewEngine._extract_task_description(records)
        assert desc == "请帮我写一个测试"

    def test_F_REV_05_extract_task_description_truncates_at_80(self) -> None:
        """超长 user 消息截断到 80 字符并加省略号。"""
        long_text = "a" * 200
        records = [
            ExecutionRecord(iteration=0, type="user", name="", error="", content=long_text),
        ]
        desc = ReviewEngine._extract_task_description(records)
        assert desc.endswith("...")
        assert len(desc) <= 83  # 80 + "..."

    def test_F_REV_05_extract_task_description_empty_when_no_user(self) -> None:
        """没有 user 消息时返回空串。"""
        records = [
            ExecutionRecord(iteration=0, type="tool", name="x", error="", content="y"),
        ]
        assert ReviewEngine._extract_task_description(records) == ""


# ============================================================
# F-REV-01/F-REV-03: 简化版复盘 + 经验提取
# 经验提取分类的回归测试，覆盖 ErrorRecord → Experience 的核心契约
# ============================================================


class TestSimpleReviewExperienceExtraction:
    """简化版复盘（内存 pipeline）经验提取规则。"""

    def test_F_REV_03_timeout_categorized_as_performance(self) -> None:
        """timeout 错误归类为 performance。"""
        engine = ReviewEngine()
        pipeline = Pipeline(
            pipeline_id="p1",
            errors=[
                ErrorRecord(error_id="e1", error_type="timeout",
                            message="took too long", timestamp="t"),
            ],
        )
        engine.register_pipeline(pipeline)
        engine.run_review()

        assert len(pipeline.experiences) == 1
        assert pipeline.experiences[0].category == "performance"
        assert "重试" in pipeline.experiences[0].lesson

    def test_F_REV_03_connection_categorized_as_infrastructure(self) -> None:
        """connection 错误归类为 infrastructure。"""
        engine = ReviewEngine()
        pipeline = Pipeline(
            pipeline_id="p2",
            errors=[
                ErrorRecord(error_id="e1", error_type="connection",
                            message="ECONNRESET", timestamp="t"),
            ],
        )
        engine.register_pipeline(pipeline)
        engine.run_review()

        assert pipeline.experiences[0].category == "infrastructure"

    def test_F_REV_03_validation_categorized_as_data_quality(self) -> None:
        """validation 错误归类为 data_quality。"""
        engine = ReviewEngine()
        pipeline = Pipeline(
            pipeline_id="p3",
            errors=[
                ErrorRecord(error_id="e1", error_type="validation",
                            message="invalid field", timestamp="t"),
            ],
        )
        engine.register_pipeline(pipeline)
        engine.run_review()

        assert pipeline.experiences[0].category == "data_quality"

    def test_F_REV_03_permission_categorized_as_security(self) -> None:
        """permission 错误归类为 security。"""
        engine = ReviewEngine()
        pipeline = Pipeline(
            pipeline_id="p4",
            errors=[
                ErrorRecord(error_id="e1", error_type="permission",
                            message="forbidden", timestamp="t"),
            ],
        )
        engine.register_pipeline(pipeline)
        engine.run_review()

        assert pipeline.experiences[0].category == "security"

    def test_F_REV_03_unknown_error_type_categorized_as_unknown(self) -> None:
        """未知错误类型 → unknown 类别。"""
        engine = ReviewEngine()
        pipeline = Pipeline(
            pipeline_id="p5",
            errors=[
                ErrorRecord(error_id="e1", error_type="weird",
                            message="??", timestamp="t"),
            ],
        )
        engine.register_pipeline(pipeline)
        engine.run_review()

        assert pipeline.experiences[0].category == "unknown"

    def test_F_REV_03_status_transitions_to_completed(self) -> None:
        """复盘完成后 pipeline.status = COMPLETED。"""
        engine = ReviewEngine()
        pipeline = Pipeline(pipeline_id="p6", errors=[])
        engine.register_pipeline(pipeline)
        engine.run_review()

        assert pipeline.status == ReviewStatus.COMPLETED
        assert pipeline.reviewed_at is not None

    def test_F_REV_03_get_pending_pipelines_excludes_completed(self) -> None:
        """get_pending_pipelines 只返回 PENDING 状态的。"""
        engine = ReviewEngine()
        p_pending = Pipeline(pipeline_id="pp", errors=[], status=ReviewStatus.PENDING)
        p_done = Pipeline(pipeline_id="pd", errors=[], status=ReviewStatus.COMPLETED)
        engine.register_pipelines([p_pending, p_done])

        pending = engine.get_pending_pipelines()

        assert len(pending) == 1
        assert pending[0].run_id == "pp"


# ============================================================
# run_review 完整版：summary 缺失 / 状态未终态 / 异常路径
# ============================================================


class TestRunReviewFullGuardrails:
    """run_review(run_id) 在异常入参下必须返回 error 而非崩溃。"""

    @pytest.mark.asyncio
    async def test_pipeline_not_found_returns_error(self) -> None:
        """summary 不存在 → status=error。"""
        engine, storage, _, _ = _build_review_engine()
        storage.get_summary.return_value = None

        result = await engine.run_review("ghost")

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_pipeline_not_terminal_returns_error(self) -> None:
        """管道 status 非 terminal（如 running） → 不复盘。"""
        engine, storage, _, _ = _build_review_engine()
        storage.get_summary.return_value = _make_summary(
            run_id="r1", status="running"
        )

        result = await engine.run_review("r1")

        assert result["status"] == "error"
        assert "not completed" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_terminal_status_success_accepted(self) -> None:
        """status=success 属于 terminal，应被接受。"""
        engine, storage, chunk_db, ks = _build_review_engine()
        storage.get_summary.return_value = _make_summary(run_id="r1", status="success")
        storage.list_by_pipeline.return_value = ([_make_record(error="err")], False)
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])
        ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})
        ks.create_knowledge = AsyncMock(return_value={"id": "k1"})

        result = await engine.run_review("r1")

        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_terminal_status_failed_accepted(self) -> None:
        """status=failed 属于 terminal，应被接受。"""
        engine, storage, chunk_db, ks = _build_review_engine()
        storage.get_summary.return_value = _make_summary(run_id="r1", status="failed", error="boom")
        storage.list_by_pipeline.return_value = ([], False)
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])
        ks.list_semantic_memory = AsyncMock(return_value={"items": [], "total": 0})
        ks.create_knowledge = AsyncMock(return_value={"id": "k1"})

        result = await engine.run_review("r1")

        assert result["status"] == "success"


def _build_review_engine(
    *,
    task_lookup: Any = None,
) -> tuple[ReviewEngine, MagicMock, MagicMock, MagicMock]:
    """构建 ReviewEngine 及其 Mock 依赖。"""
    storage = MagicMock()
    chunk_db = MagicMock()
    ks = MagicMock()
    engine = ReviewEngine(
        storage=storage,
        chunk_db=chunk_db,
        knowledge_service=ks,
        pipeline_engine=None,
        task_lookup=task_lookup,
    )
    return engine, storage, chunk_db, ks


# ============================================================
# F-REV-02/F-REV-03: run_batch_review 批量复盘适配器
# ============================================================


class TestRunBatchReview:
    """run_batch_review 在不同入参下的行为契约。"""

    def test_run_batch_with_no_ids_runs_simple_review(self) -> None:
        """run_ids=None 时走简化版（内存 pipeline）。"""
        engine = ReviewEngine()
        engine.register_pipeline(Pipeline(
            pipeline_id="bp1",
            errors=[
                ErrorRecord(error_id="e1", error_type="timeout",
                            message="m", timestamp="t"),
            ],
        ))
        result = engine.run_batch_review(run_ids=None)

        assert "total_pending" in result
        assert result["processed"] == 1

    def test_run_batch_with_empty_ids_list_returns_empty(self) -> None:
        """run_ids=[] 时走完整版流程，无 ID → 无结果。"""
        engine = ReviewEngine()
        result = engine.run_batch_review(run_ids=[])
        assert result["total"] == 0


# ============================================================
# MaintenanceConfig.from_dict: 嵌套配置展平
# ============================================================


class TestMaintenanceConfigParsing:
    """MaintenanceConfig.from_dict 嵌套字典展平行为。"""

    def test_flat_dict_passes_through(self) -> None:
        cfg = MaintenanceConfig.from_dict({"review_min_records": 200, "enabled": True})
        assert cfg.review_min_records == 200
        assert cfg.enabled is True

    def test_nested_dict_flattened_one_level(self) -> None:
        """一层嵌套字典展平到顶层。"""
        cfg = MaintenanceConfig.from_dict({
            "review": {
                "skeleton_budget_percent": 20,
                "max_records_per_review": 5000,
            },
        })
        assert cfg.skeleton_budget_percent == 20
        assert cfg.max_records_per_review == 5000

    def test_nested_two_level_flattened(self) -> None:
        """两层嵌套（review.trigger.xxx）展平到顶层。"""
        cfg = MaintenanceConfig.from_dict({
            "review": {
                "trigger": {
                    "review_min_records": 800,
                    "review_max_interval": 3600,
                },
            },
        })
        assert cfg.review_min_records == 800
        assert cfg.review_max_interval == 3600

    def test_unknown_keys_silently_ignored(self) -> None:
        """未知字段被静默丢弃。"""
        cfg = MaintenanceConfig.from_dict({"foo": "bar", "enabled": True})
        assert cfg.enabled is True
        assert not hasattr(cfg, "foo")

    def test_empty_dict_returns_defaults(self) -> None:
        cfg = MaintenanceConfig.from_dict({})
        default = MaintenanceConfig()
        assert cfg.review_min_records == default.review_min_records
        assert cfg.cleanup_min_age_days == default.cleanup_min_age_days

    def test_default_config_values_match_requirements(self) -> None:
        """默认值对照需求文档 §2.2 的约定值。"""
        cfg = MaintenanceConfig()
        # 需求文档 §2.2: min_records: 500
        assert cfg.review_min_records == 500
        # 需求文档 §2.2: max_interval: 604800 (7天)
        assert cfg.review_max_interval == 604800
        # 需求文档 §2.2: skeleton_budget_percent: 15
        assert cfg.skeleton_budget_percent == 15
        # 需求文档 §2.2: max_records_per_review: 2000
        assert cfg.max_records_per_review == 2000


# ============================================================
# F-REV-02: MemoryMaintenanceService 配置参数 / 统计
# ============================================================


class TestMaintenanceServiceStats:
    """get_stats 返回初始统计字段，便于监控确认。"""

    def test_initial_stats_has_required_keys(self) -> None:
        service, _, _, _ = _build_service()
        stats = service.get_stats()
        for key in ("last_review_at", "last_cleanup_at", "review_count",
                     "cleanup_count", "total_pipelines_reviewed",
                     "total_experiences_saved", "total_pipelines_cleaned"):
            assert key in stats

    def test_stats_are_snapshot_not_reference(self) -> None:
        """get_stats 返回的是副本，外部修改不影响内部状态。"""
        service, _, _, _ = _build_service()
        stats = service.get_stats()
        stats["review_count"] = 999
        assert service.get_stats()["review_count"] == 0


# ============================================================
# PipelineLogParser: 修复后导入正确性 + 日志解析鲁棒性
# ============================================================


class TestLogParserImport:
    """Bug 修复回归：log_parser 使用相对导入，确保不会因 sys.path 差异而 ImportError。"""

    def test_import_works_without_src_prefix(self) -> None:
        """从 memory.maintenance 包路径导入 PipelineLogParser 不报错。"""
        from memory.maintenance.log_parser import PipelineLogParser as PLP
        assert PLP is PipelineLogParser


class TestLogParserParsing:
    """PipelineLogParser.parse_pipeline_logs 日志解析行为。"""

    def test_parse_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        """空目录 → 空列表。"""
        result = PipelineLogParser.parse_pipeline_logs(tmp_path)
        assert result == []

    def test_parse_nonexistent_dir_returns_empty(self) -> None:
        """不存在的目录 → 空列表（不崩溃）。"""
        result = PipelineLogParser.parse_pipeline_logs("/no/such/dir/xyz")
        assert result == []

    def test_parse_valid_log_file(self, tmp_path: Path) -> None:
        """解析包含 ERROR 行的日志文件，提取 pipeline + 错误。"""
        log_file = tmp_path / "pipeline_abc123.log"
        log_file.write_text(
            '2026-01-01 10:00:00.000 [pipeline_abc123] INFO start\n'
            '2026-01-01 10:00:01.000 [pipeline_abc123] ERROR '
            'something error_type=timeout error="request timed out"\n',
            encoding="utf-8",
        )
        pipelines = PipelineLogParser.parse_pipeline_logs(tmp_path)
        assert len(pipelines) == 1
        assert pipelines[0].pipeline_id == "abc123"
        assert len(pipelines[0].errors) == 1
        assert pipelines[0].errors[0].error_type == "timeout"
        assert "request timed out" in pipelines[0].errors[0].message

    def test_parse_log_without_errors(self, tmp_path: Path) -> None:
        """日志只有 INFO 行，没有 ERROR → pipeline 存在但 errors 为空。"""
        log_file = tmp_path / "pipeline_clean.log"
        log_file.write_text(
            '2026-01-01 10:00:00.000 [pipeline_clean] INFO all good\n',
            encoding="utf-8",
        )
        pipelines = PipelineLogParser.parse_pipeline_logs(tmp_path)
        assert len(pipelines) == 1
        assert pipelines[0].pipeline_id == "clean"
        assert len(pipelines[0].errors) == 0

    def test_parse_multiple_errors_in_one_file(self, tmp_path: Path) -> None:
        """同一管道多个 ERROR → 多个 ErrorRecord。"""
        log_file = tmp_path / "pipeline_multi.log"
        log_file.write_text(
            '2026-01-01 10:00:00.000 [pipeline_multi] ERROR a error_type=timeout error="t1"\n'
            '2026-01-01 10:00:01.000 [pipeline_multi] ERROR b error_type=connection error="c1"\n',
            encoding="utf-8",
        )
        pipelines = PipelineLogParser.parse_pipeline_logs(tmp_path)
        assert len(pipelines[0].errors) == 2

    def test_parse_non_log_files_ignored(self, tmp_path: Path) -> None:
        """不以 pipeline_ 开头的文件被忽略。"""
        (tmp_path / "other.log").write_text("noise", encoding="utf-8")
        result = PipelineLogParser.parse_pipeline_logs(tmp_path)
        assert result == []

    def test_parse_empty_log_file_handled(self, tmp_path: Path) -> None:
        """空日志文件不崩溃，由于没有 pipeline_id 可识别，结果为空列表。"""
        log_file = tmp_path / "pipeline_empty.log"
        log_file.write_text("", encoding="utf-8")
        pipelines = PipelineLogParser.parse_pipeline_logs(tmp_path)
        # 空文件无法识别 pipeline_id，因此不会产生 Pipeline 对象
        assert pipelines == []
