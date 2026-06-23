"""复盘系统触发链路与接口契约测试。

测试目标：锁定复盘系统"能触发、能执行、接口稳定"这三件事，防止后续重构
悄悄破坏触发链路或改掉对外方法签名。

覆盖范围：
1. ReviewEngine 简化版复盘（内存 pipeline）——核心逻辑契约
2. TriggerReviewTool 触发链路——服务不可用/正在运行/不满足条件/正常提交 四条分支
3. MemoryMaintenanceService 真实接口契约——构造签名、should_trigger_review、get_stats

历史说明：本文件原有一组用例依赖 scripts/trigger_review.py（该脚本从未存在于仓库）
和 service.trigger_review()（该方法不存在，真实方法为 trigger_review_now），
属于测不存在的契约，已替换为对真实接口的契约测试。
"""
from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest

from src.memory.maintenance.review_engine import (
    ErrorRecord,
    Pipeline,
    ReviewEngine,
    ReviewStatus,
)
from src.memory.maintenance.service import MemoryMaintenanceService
from tools.builtin.trigger_review.tool import TriggerReviewTool


# ---------------------------------------------------------------------------
# ReviewEngine 简化版复盘契约（内存 pipeline）
# ---------------------------------------------------------------------------


def _build_pending_pipelines() -> list[Pipeline]:
    """构造 3 个 pending pipeline：001 含 2 错误、002 含 1 错误、003 无错误。"""
    return [
        Pipeline(pipeline_id="pipeline-001", errors=[
            ErrorRecord("err-001", "timeout", "API 调用超时", "2026-05-28T08:00:00Z"),
            ErrorRecord("err-002", "connection", "数据库连接断开", "2026-05-28T08:01:00Z"),
        ]),
        Pipeline(pipeline_id="pipeline-002", errors=[
            ErrorRecord("err-003", "validation", "参数格式不正确", "2026-05-28T08:05:00Z"),
        ]),
        Pipeline(pipeline_id="pipeline-003", errors=[]),
    ]


class TestReviewEngineSimpleFlow:
    """ReviewEngine 简化版复盘流程契约。"""

    def test_processes_all_pending_pipelines(self):
        """所有 pending pipeline 都被处理（total_pending=3, processed=3）。"""
        engine = ReviewEngine()
        engine.register_pipelines(_build_pending_pipelines())
        result = engine.run_review()

        assert result["total_pending"] == 3
        assert result["processed"] == 3

    def test_experience_extraction_counts(self):
        """经验提取数量按错误数计算：001→2, 002→1, 003→0。"""
        engine = ReviewEngine()
        engine.register_pipelines(_build_pending_pipelines())
        result = engine.run_review()

        pr = result["pipeline_results"]
        assert pr[0]["pipeline_id"] == "pipeline-001"
        assert pr[0]["experience_count"] == 2
        assert pr[1]["pipeline_id"] == "pipeline-002"
        assert pr[1]["experience_count"] == 1
        assert pr[2]["pipeline_id"] == "pipeline-003"
        assert pr[2]["experience_count"] == 0

    def test_status_transitions_to_completed(self):
        """复盘完成后所有 pipeline 状态流转为 completed。"""
        engine = ReviewEngine()
        pipelines = _build_pending_pipelines()
        engine.register_pipelines(pipelines)
        engine.run_review()

        for p in pipelines:
            assert p.status == ReviewStatus.COMPLETED


# ---------------------------------------------------------------------------
# TriggerReviewTool 触发链路契约
# ---------------------------------------------------------------------------


def _make_service_provider(maintenance_service):
    """构造 service provider，get('maintenance_service') 返回给定实例。"""
    provider = MagicMock()
    provider.get.return_value = maintenance_service
    return provider


def _run(coro):
    """在独立事件循环里跑一个协程并返回结果。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestTriggerReviewToolBranches:
    """trigger_review 工具的 4 条触发分支契约。

    对应"用户说帮我复盘 → Agent 调用 trigger_review 工具"的真实路径，
    覆盖：服务不可用、正在运行、不满足条件、正常提交四种情形。

    注意：ToolExecutionResult 把 create_success_result(data=...) 的 data 存到
    .output 字段（非 .data），按真实结构断言。
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

    def test_returns_already_running_when_review_in_progress(self, monkeypatch):
        """分支2：_review_running=True → already_running，不重复触发。"""
        service = MagicMock()
        service._review_running = True
        monkeypatch.setattr(
            "infrastructure.service_provider.get_service_provider",
            lambda: _make_service_provider(service),
        )

        tool = TriggerReviewTool()
        result = _run(tool.execute({}))

        assert result.success is True
        assert result.output["status"] == "already_running"

    def test_returns_skipped_when_trigger_condition_not_met(self, monkeypatch):
        """分支3：非强制 + 不满足触发条件 → skipped，并返回 pending 数。"""
        review_engine = MagicMock()
        review_engine._count_pending_records.return_value = 5
        service = MagicMock()
        service._review_running = False
        service.should_trigger_review.return_value = False
        service._get_review_engine.return_value = review_engine
        monkeypatch.setattr(
            "infrastructure.service_provider.get_service_provider",
            lambda: _make_service_provider(service),
        )

        tool = TriggerReviewTool()
        result = _run(tool.execute({"force": False}))

        assert result.success is True
        assert result.output["status"] == "skipped"
        assert result.output["pending_records"] == 5

    def test_submits_when_condition_met(self, monkeypatch):
        """分支4：满足条件 → submitted。

        防回归要点：提交后 _review_running 置 True，后台任务跑完 finally 复位。
        execute() 同步返回 submitted 即视为触发成功；后台任务内部的
        load_agent_config / send_pipeline_message 是运行时组件，被 tool.py
        的 except Exception 兜底，单测里无需 patch 也无需等待其完成
        （它挂在 _run 创建并关闭的循环上，循环关闭时被清理）。
        """
        service = MagicMock()
        service._review_running = False
        service.should_trigger_review.return_value = True
        monkeypatch.setattr(
            "infrastructure.service_provider.get_service_provider",
            lambda: _make_service_provider(service),
        )

        tool = TriggerReviewTool()
        result = _run(tool.execute({"force": False}))

        assert result.success is True
        assert result.output["status"] == "submitted"


# ---------------------------------------------------------------------------
# MemoryMaintenanceService 接口契约（防回归）
# ---------------------------------------------------------------------------


class TestMemoryMaintenanceServiceContract:
    """锁定 MemoryMaintenanceService 对外接口签名，防止重构悄悄改契约。

    存在意义：历史上出现过 service.trigger_review() 这种"测不存在方法"
    的用例混进测试文件却没人发现，说明接口契约没有真正被守护。
    这里显式锁定真实方法名和构造签名。
    """

    def test_init_requires_three_dependencies(self):
        """构造函数必须要求 storage/chunk_db/knowledge_service 三个必填依赖。"""
        sig = inspect.signature(MemoryMaintenanceService.__init__)
        required = {
            name for name, p in sig.parameters.items()
            if name != "self" and p.default is inspect.Parameter.empty
        }
        assert {"storage", "chunk_db", "knowledge_service"} <= required

    def test_exposes_trigger_review_now_not_trigger_review(self):
        """对外方法名是 trigger_review_now；历史上误用的 trigger_review 不应存在。"""
        assert hasattr(MemoryMaintenanceService, "trigger_review_now")
        assert callable(MemoryMaintenanceService.trigger_review_now)
        assert not hasattr(MemoryMaintenanceService, "trigger_review"), (
            "trigger_review 是历史误用的不存在方法，不应出现在真实接口上"
        )

    def test_exposes_should_trigger_review_and_get_stats(self):
        """触发判断与统计查询方法必须存在。"""
        assert callable(MemoryMaintenanceService.should_trigger_review)
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

    async def test_trigger_review_now_returns_result_dict_when_no_pending(self):
        """trigger_review_now 在无 pending 时也应返回结构化结果。"""
        storage = MagicMock()
        storage.list_all_summaries.return_value = []
        service = MemoryMaintenanceService(
            storage=storage,
            chunk_db=MagicMock(),
            knowledge_service=MagicMock(),
        )

        result = await service.trigger_review_now(force=False)

        assert result["force"] is False
        assert result["pending_count"] == 0
        assert result["pipelines_reviewed"] == 0
        assert "started_at" in result
        assert "completed_at" in result

    def test_should_trigger_review_false_when_no_pending_and_no_history(self):
        """无 pending 且无 last_review_at 记录时不应触发。"""
        storage = MagicMock()
        storage.list_all_summaries.return_value = []
        service = MemoryMaintenanceService(
            storage=storage,
            chunk_db=MagicMock(),
            knowledge_service=MagicMock(),
        )

        assert service.should_trigger_review() is False
