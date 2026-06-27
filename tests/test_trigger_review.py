"""复盘系统 B 路径触发链路与接口契约测试。

测试目标：锁定 B 路径（trigger_llm_review → review_agent LLM 深度复盘）
的"能触发、能查询、接口稳定"三件事，防止后续重构悄悄破坏触发链路。

覆盖范围：
1. ReviewEngine 查询层——get_pending_pipelines 过滤、get_summary、mark_reviewed
2. TriggerReviewTool 触发链路——服务不可用 / 正常提交 / 内存执行（无父管道）
3. MemoryMaintenanceService 接口契约——构造签名、trigger_llm_review、get_stats

历史说明：原文件测的是 A 路径（trigger_review_now / should_trigger_review /
_count_pending_records / 内存 run_review()），这些已随 A 路径删除，
全部替换为对 B 路径真实接口的契约测试。
"""
from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.memory.maintenance.review_engine import (
    PipelineRunSummary,
    ReviewEngine,
    ReviewStatus,
)
from src.memory.maintenance.service import MemoryMaintenanceService
from tools.builtin.trigger_review.tool import TriggerReviewTool


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _make_summary(
    run_id: str = "run-001",
    status: str = "completed",
    review_status: str = "pending",
    **overrides,
) -> PipelineRunSummary:
    """创建 PipelineRunSummary 测试 fixture。"""
    defaults = dict(
        run_id=run_id,
        total_records=5,
        total_iterations=3,
        created_at="2026-01-01T00:00:00",
        status=status,
        error="",
        review_status=review_status,
    )
    defaults.update(overrides)
    return PipelineRunSummary(**defaults)


def _run(coro):
    """在独立事件循环里跑一个协程并返回结果。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# ReviewEngine 查询层契约
# ---------------------------------------------------------------------------


class TestReviewEngineQueryLayer:
    """ReviewEngine 查询层契约：get_pending_pipelines 过滤、get_summary、mark_reviewed。

    A 路径删除后，ReviewEngine 只剩查询/标记能力，复盘执行交给 B 路径。
    """

    def test_get_pending_filters_by_status_and_review_status(self):
        """只返回 status=已结束 且 review_status=pending 的管道。"""
        storage = MagicMock()
        storage.list_all_summaries.return_value = [
            _make_summary(run_id="run-ok-1", status="success", review_status="pending"),
            _make_summary(run_id="run-ok-2", status="failed", review_status="pending"),
            _make_summary(run_id="run-done", status="success", review_status="completed"),
            _make_summary(run_id="run-running", status="running", review_status="pending"),
        ]
        engine = ReviewEngine(storage=storage)

        pending = engine.get_pending_pipelines()

        assert {s.run_id for s in pending} == {"run-ok-1", "run-ok-2"}

    def test_get_pending_returns_empty_when_no_storage(self):
        """storage 为 None 时返回空列表（内存模式已随 A 路径删除）。"""
        engine = ReviewEngine(storage=None)
        assert engine.get_pending_pipelines() == []

    def test_get_summary_delegates_to_storage(self):
        """get_summary 委托给 storage。"""
        storage = MagicMock()
        expected = _make_summary(run_id="run-x")
        storage.get_summary.return_value = expected
        engine = ReviewEngine(storage=storage)

        assert engine.get_summary("run-x") is expected
        storage.get_summary.assert_called_once_with("run-x")

    def test_get_summary_returns_none_when_no_storage(self):
        """storage 为 None 时 get_summary 返回 None。"""
        engine = ReviewEngine(storage=None)
        assert engine.get_summary("run-x") is None

    @pytest.mark.asyncio
    async def test_mark_reviewed_updates_summary_completed(self):
        """mark_reviewed 默认把 review_status 标记为 completed。"""
        storage = MagicMock()
        chunk_db = MagicMock()
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])
        engine = ReviewEngine(storage=storage, chunk_db=chunk_db)

        await engine.mark_reviewed("run-1")

        storage.update_summary.assert_called_once_with(
            "run-1", {"review_status": "completed"}
        )

    @pytest.mark.asyncio
    async def test_mark_reviewed_failed_when_failed_flag(self):
        """failed=True 时标记为 failed。"""
        storage = MagicMock()
        chunk_db = MagicMock()
        chunk_db.find_by_pipeline = AsyncMock(return_value=[])
        engine = ReviewEngine(storage=storage, chunk_db=chunk_db)

        await engine.mark_reviewed("run-1", failed=True)

        storage.update_summary.assert_called_once_with(
            "run-1", {"review_status": "failed"}
        )

    @pytest.mark.asyncio
    async def test_mark_reviewed_updates_chunk_flags(self):
        """mark_reviewed 同步更新 chunk 的 review_status 标记。"""
        storage = MagicMock()
        chunk = MagicMock()
        chunk.extra_data = {"reviewed": False}
        chunk_db = MagicMock()
        chunk_db.find_by_pipeline = AsyncMock(return_value=[chunk])
        engine = ReviewEngine(storage=storage, chunk_db=chunk_db)

        await engine.mark_reviewed("run-1")

        assert chunk.extra_data["review_status"] == "completed"
        chunk_db.save_chunk.assert_called_once_with(chunk)

    @pytest.mark.asyncio
    async def test_mark_reviewed_swallows_chunk_error(self):
        """chunk 操作异常不阻止 summary 更新。"""
        storage = MagicMock()
        chunk_db = MagicMock()
        chunk_db.find_by_pipeline = AsyncMock(side_effect=RuntimeError("disk error"))
        engine = ReviewEngine(storage=storage, chunk_db=chunk_db)

        await engine.mark_reviewed("run-1")

        storage.update_summary.assert_called_once_with(
            "run-1", {"review_status": "completed"}
        )


# ---------------------------------------------------------------------------
# TriggerReviewTool 触发链路契约（B 路径）
# ---------------------------------------------------------------------------


def _make_service_provider(maintenance_service):
    """构造 service provider，get('maintenance_service') 返回给定实例。"""
    provider = MagicMock()
    provider.get.return_value = maintenance_service
    return provider


class TestTriggerReviewToolBranches:
    """trigger_review 工具触发分支契约。

    对应"用户说帮我复盘 → Agent 调用 trigger_review 工具"的真实 B 路径。
    """

    def test_returns_failure_when_service_unavailable(self, monkeypatch):
        """分支1：provider 取不到 maintenance_service → SERVICE_UNAVAILABLE。"""
        provider = MagicMock()
        provider.get.return_value = None
        monkeypatch.setattr(
            "infrastructure.service_provider.get_service_provider",
            lambda: provider,
        )

        tool = TriggerReviewTool()
        result = _run(tool.execute({}))

        assert result.success is False
        assert result.error_code == "SERVICE_UNAVAILABLE"

    def test_submits_review_task(self, monkeypatch):
        """分支2：正常提交 → submitted。

        execute() 读 _current_pipeline_id（此处为空），调 trigger_llm_review，
        返回 success，data.output.status == submitted。
        """
        service = MagicMock()
        service.trigger_llm_review = AsyncMock(return_value={
            "status": "submitted",
            "message": "复盘任务已提交，完成后会通知您结果。",
        })
        monkeypatch.setattr(
            "infrastructure.service_provider.get_service_provider",
            lambda: _make_service_provider(service),
        )

        tool = TriggerReviewTool()
        result = _run(tool.execute({}))

        assert result.success is True
        assert result.output["status"] == "submitted"
        service.trigger_llm_review.assert_awaited_once()

    def test_returns_already_running_when_in_progress(self, monkeypatch):
        """分支3：_review_running=True → already_running，不重复触发。"""
        service = MagicMock()
        service._review_running = True
        service.trigger_llm_review = AsyncMock(return_value={
            "status": "already_running",
            "message": "复盘正在执行中，请稍后再试",
        })
        monkeypatch.setattr(
            "infrastructure.service_provider.get_service_provider",
            lambda: _make_service_provider(service),
        )

        tool = TriggerReviewTool()
        result = _run(tool.execute({}))

        assert result.success is True
        assert result.output["status"] == "already_running"


# ---------------------------------------------------------------------------
# MemoryMaintenanceService 接口契约（防回归）
# ---------------------------------------------------------------------------


class TestMemoryMaintenanceServiceContract:
    """锁定 MemoryMaintenanceService 对外接口签名，防止重构悄悄改契约。"""

    def test_init_requires_three_dependencies(self):
        """构造函数必须要求 storage/chunk_db/knowledge_service 三个必填依赖。"""
        sig = inspect.signature(MemoryMaintenanceService.__init__)
        required = {
            name for name, p in sig.parameters.items()
            if name != "self" and p.default is inspect.Parameter.empty
        }
        assert {"storage", "chunk_db", "knowledge_service"} <= required

    def test_exposes_trigger_llm_review(self):
        """对外复盘入口是 trigger_llm_review（B 路径）。"""
        assert hasattr(MemoryMaintenanceService, "trigger_llm_review")
        assert callable(MemoryMaintenanceService.trigger_llm_review)

    def test_a_path_methods_removed(self):
        """A 路径方法已删除，不应再出现。"""
        assert not hasattr(MemoryMaintenanceService, "trigger_review_now")
        assert not hasattr(MemoryMaintenanceService, "should_trigger_review")
        assert not hasattr(MemoryMaintenanceService, "run_maintenance")

    def test_exposes_run_cleanup_and_get_stats(self):
        """清理巡检入口和统计查询方法必须存在。"""
        assert callable(MemoryMaintenanceService.run_cleanup)
        assert callable(MemoryMaintenanceService.should_trigger_cleanup)
        assert callable(MemoryMaintenanceService.get_stats)

    def test_get_stats_returns_initial_counters(self):
        """新实例的 get_stats 应返回初始统计计数器。"""
        service = MemoryMaintenanceService(
            storage=MagicMock(), chunk_db=MagicMock(), knowledge_service=MagicMock(),
        )
        stats = service.get_stats()
        assert stats["review_count"] == 0
        assert stats["cleanup_count"] == 0
        assert stats["total_pipelines_reviewed"] == 0
        assert stats["total_experiences_saved"] == 0


class TestReviewStatusEnum:
    """ReviewStatus 枚举值与存储层 review_status 字段对齐。"""

    def test_enum_values(self):
        assert ReviewStatus.PENDING.value == "pending"
        assert ReviewStatus.IN_PROGRESS.value == "in_progress"
        assert ReviewStatus.COMPLETED.value == "completed"
        assert ReviewStatus.FAILED.value == "failed"
