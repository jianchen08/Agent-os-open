"""HTML 测试报告渲染 — 单元测试。

验证 html_report 模块能正确渲染基础 HTML 报告和生产级汇总报告。
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import pytest

from tests.test_utils.ac_matrix import ACMatrixTracker, ACStatus
from tests.test_utils.feature_coverage import FeatureCoverageTracker
from tests.test_utils.html_report import (
    _collect_screenshots_base64,
    _render_screenshots_html,
    render_combined_report,
    render_html_report,
)
from tests.test_utils.report_generator import TestCaseResult, TestReport


# ── 辅助函数 ──────────────────────────────────────────


def _make_report(
    total: int = 3,
    outcomes: list[str] | None = None,
) -> TestReport:
    """构造一个包含指定结果的 TestReport。"""
    report = TestReport(
        timestamp="2025-01-01T00:00:00Z",
        environment={"python": "3.10", "platform": "linux"},
    )
    if outcomes is None:
        outcomes = ["passed"] * total
    for i, outcome in enumerate(outcomes):
        case = TestCaseResult(
            node_id=f"tests/test_foo.py::test_{i}",
            name=f"test_{i}",
            outcome=outcome,
            duration_ms=10.0 * (i + 1),
            error_message="assertion failed" if outcome == "failed" else "",
        )
        report.test_cases.append(case)
        report.total += 1
        report.duration_ms += case.duration_ms
        if outcome == "passed":
            report.passed += 1
        elif outcome == "failed":
            report.failed += 1
        elif outcome == "error":
            report.errors += 1
        elif outcome == "skipped":
            report.skipped += 1
    return report


# ── render_html_report 测试 ───────────────────────────


class TestRenderHtmlReport:
    """render_html_report 基础 HTML 报告测试。"""

    def test_output_is_self_contained_html(self):
        """验证输出是完整的自包含 HTML 文档。"""
        report = _make_report(total=1)
        html = render_html_report(report)
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "</html>" in html
        assert "<style>" in html
        assert "测试报告" in html

    def test_shows_total_passed_failed(self):
        """验证 HTML 包含正确的总计/通过/失败数字。"""
        report = _make_report(outcomes=["passed", "passed", "failed"])
        html = render_html_report(report)
        assert "总计" in html
        assert "通过" in html
        assert "失败" in html

    def test_shows_environment_info(self):
        """验证 HTML 包含环境信息。"""
        report = _make_report(total=1)
        html = render_html_report(report)
        assert "python" in html
        assert "linux" in html

    def test_error_message_is_escaped(self):
        """验证失败用例的错误消息被 HTML 转义。"""
        case = TestCaseResult(
            node_id="tests/test.py::test_x",
            name="test_x",
            outcome="failed",
            duration_ms=5.0,
            error_message="<script>alert('xss')</script>",
        )
        report = TestReport(
            timestamp="2025-01-01T00:00:00Z",
            total=1,
            passed=0,
            failed=1,
            errors=0,
            skipped=0,
            duration_ms=5.0,
            test_cases=[case],
            environment={},
        )
        html = render_html_report(report)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ── render_combined_report 测试 ────────────────────────


class TestRenderCombinedReport:
    """render_combined_report 生产级汇总报告测试。"""

    def test_minimal_report_is_self_contained_html(self):
        """验证无任何数据时仍输出完整 HTML。"""
        html = render_combined_report()
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "</html>" in html
        assert "灵汐系统测试总报告" in html

    def test_with_backend_report_shows_test_cases(self):
        """验证包含后端报告时展示测试用例表格。"""
        report = _make_report(outcomes=["passed", "failed"])
        html = render_combined_report(backend_report=report)
        assert "后端测试用例" in html
        assert "PASSED" in html
        assert "FAILED" in html

    def test_with_frontend_data_shows_section(self):
        """验证包含前端数据时展示前端测试区段。"""
        html = render_combined_report(
            frontend_data={
                "total": 5,
                "passed": 4,
                "failed": 1,
                "duration_ms": 200,
            }
        )
        assert "前端 Vitest 测试" in html
        assert "5" in html

    def test_with_e2e_data_shows_section(self):
        """验证包含 E2E 数据时展示 E2E 测试区段。"""
        html = render_combined_report(
            e2e_data={
                "total": 3,
                "passed": 3,
                "failed": 0,
                "duration_ms": 500,
            }
        )
        assert "E2E 浏览器测试" in html

    def test_with_ac_tracker_shows_matrix(self):
        """验证包含 AC tracker 时输出 AC 状态矩阵。"""
        ac = ACMatrixTracker()
        ac.update("AC-1", ACStatus.PASSED, evidence="测试通过")
        html = render_combined_report(ac_tracker=ac)
        assert "AC 状态矩阵" in html
        assert "AC-1" in html

    def test_with_coverage_tracker_shows_matrix(self):
        """验证包含覆盖率 tracker 时输出功能覆盖率矩阵。"""
        cov = FeatureCoverageTracker()
        cov.mark_tested("1. 对话与聊天", "主对话")
        html = render_combined_report(coverage_tracker=cov)
        assert "功能覆盖率矩阵" in html
        assert "1. 对话与聊天" in html

    def test_with_screenshot_dir_shows_gallery(self):
        """验证指定截图目录时展示截图画廊区段。"""
        html = render_combined_report(screenshot_dir="/nonexistent/path")
        assert "E2E 截图画廊" in html

    def test_dashboard_grand_total_is_sum(self):
        """验证仪表盘总数等于后端+前端+E2E 之和。"""
        report = _make_report(outcomes=["passed", "passed"])
        html = render_combined_report(
            backend_report=report,
            frontend_data={"total": 5, "passed": 5, "failed": 0, "duration_ms": 100},
            e2e_data={"total": 3, "passed": 2, "failed": 1, "duration_ms": 300},
        )
        # grand_total = 2 + 5 + 3 = 10
        assert "10" in html


# ── 截图辅助函数测试 ──────────────────────────────────


class TestScreenshotHelpers:
    """截图收集与渲染辅助函数测试。"""

    def test_collect_screenshots_from_nonexistent_dir(self):
        """验证不存在的目录返回空列表。"""
        result = _collect_screenshots_base64("/nonexistent/path")
        assert result == []

    def test_collect_screenshots_from_empty_dir(self):
        """验证空目录返回空列表。"""
        with tempfile.TemporaryDirectory() as tmp:
            result = _collect_screenshots_base64(tmp)
            assert result == []

    def test_collect_screenshots_encodes_png(self):
        """验证 PNG 文件被正确 base64 编码。"""
        with tempfile.TemporaryDirectory() as tmp:
            png_path = Path(tmp) / "screenshot_login.png"
            png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
            result = _collect_screenshots_base64(tmp)
            assert len(result) == 1
            assert result[0]["name"] == "screenshot_login"
            assert result[0]["data_uri"].startswith("data:image/png;base64,")

    def test_render_screenshots_html_empty(self):
        """验证无截图时展示占位提示。"""
        html = _render_screenshots_html([])
        assert "E2E 截图画廊" in html
        assert "暂无截图" in html

    def test_render_screenshots_html_with_data(self):
        """验证有截图时渲染图片卡片。"""
        shots = [
            {
                "name": "login",
                "caption": "login page",
                "data_uri": "data:image/png;base64,abc123",
            }
        ]
        html = _render_screenshots_html(shots)
        assert "screenshot-card" in html
        assert "login page" in html
        assert "1 张截图" in html


# ── 端到端汇总场景 ────────────────────────────────────


class TestCombinedReportE2E:
    """汇总报告端到端场景测试。"""

    def test_full_report_contains_all_sections(self):
        """验证包含所有模块的完整报告输出所有区段。"""
        report = _make_report(outcomes=["passed", "failed"])
        ac = ACMatrixTracker()
        ac.update("AC-1", ACStatus.PASSED)
        ac.update("AC-7", ACStatus.FAILED)
        cov = FeatureCoverageTracker()
        cov.mark_tested("1. 对话与聊天", "主对话")

        html = render_combined_report(
            backend_report=report,
            frontend_data={"total": 2, "passed": 2, "failed": 0, "duration_ms": 50},
            e2e_data={"total": 1, "passed": 1, "failed": 0, "duration_ms": 200},
            ac_tracker=ac,
            coverage_tracker=cov,
            screenshot_dir="",
        )

        # 所有主要区段都应存在
        assert "总览仪表盘" in html or "dashboard" in html
        assert "AC 状态矩阵" in html
        assert "功能覆盖率矩阵" in html
        assert "后端测试用例" in html
        assert "前端 Vitest 测试" in html
        assert "E2E 浏览器测试" in html
        assert "E2E 截图画廊" in html
        assert "环境信息" in html

    def test_report_html_is_valid_markup(self):
        """验证报告 HTML 包含完整的 HTML5 结构。"""
        html = render_combined_report()
        assert "<!DOCTYPE html>" in html
        assert '<html lang="zh-CN">' in html
        assert "<head>" in html
        assert "<body>" in html
        assert "</body>" in html
        assert "</html>" in html
