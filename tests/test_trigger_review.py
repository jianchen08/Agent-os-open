"""测试复盘触发脚本的完整流程验证。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from src.memory.maintenance.review_engine import (
    ErrorRecord,
    Pipeline,
    ReviewEngine,
    ReviewStatus,
)
from src.memory.maintenance.service import MemoryMaintenanceService


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "trigger_review.py"


class TestTriggerReviewScript:
    """复盘触发脚本完整流程测试。"""

    def test_script_runs_with_exit_code_zero(self):
        """验证脚本可正常运行，退出码为0。"""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"脚本退出码应为0，实际为 {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_no_uncaught_exceptions(self):
        """验证运行过程无未捕获异常。"""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "Traceback" not in result.stderr, (
            f"存在未捕获异常:\n{result.stderr}"
        )

    def test_review_engine_processes_all_pending_pipelines(self):
        """验证 ReviewEngine 直接复盘阶段：3个 pending pipeline 全部完成。"""
        engine = ReviewEngine()
        pipelines = [
            Pipeline(pipeline_id="pipeline-001", errors=[
                ErrorRecord("err-001", "timeout", "API 调用超时", "2026-05-28T08:00:00Z"),
                ErrorRecord("err-002", "connection", "数据库连接断开", "2026-05-28T08:01:00Z"),
            ]),
            Pipeline(pipeline_id="pipeline-002", errors=[
                ErrorRecord("err-003", "validation", "参数格式不正确", "2026-05-28T08:05:00Z"),
            ]),
            Pipeline(pipeline_id="pipeline-003", errors=[]),
        ]
        engine.register_pipelines(pipelines)
        result = engine.run_review()

        assert result["total_pending"] == 3
        assert result["processed"] == 3

    def test_experience_extraction_counts(self):
        """验证经验提取正确：pipeline-001→2条, pipeline-002→1条, pipeline-003→0条。"""
        engine = ReviewEngine()
        pipelines = [
            Pipeline(pipeline_id="pipeline-001", errors=[
                ErrorRecord("err-001", "timeout", "API 调用超时", "2026-05-28T08:00:00Z"),
                ErrorRecord("err-002", "connection", "数据库连接断开", "2026-05-28T08:01:00Z"),
            ]),
            Pipeline(pipeline_id="pipeline-002", errors=[
                ErrorRecord("err-003", "validation", "参数格式不正确", "2026-05-28T08:05:00Z"),
            ]),
            Pipeline(pipeline_id="pipeline-003", errors=[]),
        ]
        engine.register_pipelines(pipelines)
        result = engine.run_review()

        pr = result["pipeline_results"]
        assert pr[0]["pipeline_id"] == "pipeline-001"
        assert pr[0]["experience_count"] == 2
        assert pr[1]["pipeline_id"] == "pipeline-002"
        assert pr[1]["experience_count"] == 1
        assert pr[2]["pipeline_id"] == "pipeline-003"
        assert pr[2]["experience_count"] == 0

    def test_review_status_changes_to_completed(self):
        """验证所有 pending pipeline 最终变为 completed。"""
        engine = ReviewEngine()
        pipelines = [
            Pipeline(pipeline_id="pipeline-001", errors=[
                ErrorRecord("err-001", "timeout", "API 调用超时", "2026-05-28T08:00:00Z"),
                ErrorRecord("err-002", "connection", "数据库连接断开", "2026-05-28T08:01:00Z"),
            ]),
            Pipeline(pipeline_id="pipeline-002", errors=[
                ErrorRecord("err-003", "validation", "参数格式不正确", "2026-05-28T08:05:00Z"),
            ]),
            Pipeline(pipeline_id="pipeline-003", errors=[]),
        ]
        engine.register_pipelines(pipelines)
        engine.run_review()

        for p in pipelines:
            assert p.status == ReviewStatus.COMPLETED, (
                f"{p.pipeline_id} 状态应为 completed，实际 {p.status}"
            )

    def test_service_interface_mismatch_detected(self):
        """验证 MemoryMaintenanceService 正确捕获接口不匹配问题。"""
        service = MemoryMaintenanceService()
        result = service.trigger_review([{"pipeline_id": "test", "errors": []}])

        interface_issues = result["interface_check"]
        assert len(interface_issues) > 0, "应检测到接口不匹配问题"

        missing_methods = [i["missing_method"] for i in interface_issues]
        assert "run_batch_review" in missing_methods, "应检测到 run_batch_review 缺失"
