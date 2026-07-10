"""结构化测试报告生成器。

从 pytest 执行结果生成 JSON 和 HTML 格式的测试报告，
包含通过率、失败详情、bug 定位信息、日志摘要等。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tests.test_utils.bug_locator import BugLocation, locate_bug
from tests.test_utils.html_report import render_html_report
from tests.test_utils.log_collector import LogCaptureResult


@dataclass
class TestCaseResult:
    """单个测试用例结果。"""

    node_id: str
    name: str
    outcome: str  # passed / failed / error / skipped
    duration_ms: float
    file_path: str = ""
    line_number: int = 0
    error_message: str = ""
    traceback: str = ""
    bug_location: BugLocation | None = None
    captured_logs: str = ""


@dataclass
class TestReport:
    """完整测试报告。"""

    timestamp: str = ""
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration_ms: float = 0.0
    test_cases: list[TestCaseResult] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        """通过率（0.0 ~ 1.0）。"""
        return self.passed / self.total if self.total > 0 else 0.0

    @property
    def fail_rate(self) -> float:
        """失败率。"""
        return self.failed / self.total if self.total > 0 else 0.0

    def add_case(self, case: TestCaseResult) -> None:
        """添加测试用例结果并自动更新计数。"""
        self.test_cases.append(case)
        self.total += 1
        self.duration_ms += case.duration_ms
        if case.outcome == "passed":
            self.passed += 1
        elif case.outcome == "failed":
            self.failed += 1
        elif case.outcome == "error":
            self.errors += 1
        elif case.outcome == "skipped":
            self.skipped += 1


class ReportGenerator:
    """测试报告生成器。

    用法::

        gen = ReportGenerator()
        gen.add_case(...)
        gen.to_json("reports/test_report.json")
        gen.to_html("reports/test_report.html")
    """

    def __init__(self) -> None:
        self._report: TestReport = TestReport(
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            environment=_collect_env_info(),
        )

    @property
    def report(self) -> TestReport:
        """当前报告对象。"""
        return self._report

    def add_case(
        self,
        node_id: str,
        name: str,
        outcome: str,
        duration_ms: float,
        file_path: str = "",
        line_number: int = 0,
        error_message: str = "",
        traceback: str = "",
        exc_info: tuple[Any, ...] | None = None,
        log_result: LogCaptureResult | None = None,
    ) -> None:
        """添加测试用例结果。

        Args:
            node_id: pytest 节点 ID（如 ``tests/test_foo.py::test_bar``）。
            name: 测试函数名。
            outcome: passed / failed / error / skipped。
            duration_ms: 执行耗时（毫秒）。
            file_path: 测试文件路径。
            line_number: 测试函数行号。
            error_message: 失败时的错误消息。
            traceback: 原始 traceback 文本。
            exc_info: 异常三元组（用于 bug 定位）。
            log_result: 日志收集结果。
        """
        bug_loc: BugLocation | None = None
        captured_logs = ""

        if outcome in ("failed", "error") and exc_info:
            bug_loc = locate_bug(exc_info)

        if log_result:
            captured_logs = log_result.format_errors()

        case = TestCaseResult(
            node_id=node_id,
            name=name,
            outcome=outcome,
            duration_ms=duration_ms,
            file_path=file_path,
            line_number=line_number,
            error_message=error_message,
            traceback=traceback,
            bug_location=bug_loc,
            captured_logs=captured_logs,
        )
        self._report.add_case(case)

    def to_json(self, output_path: str) -> str:
        """导出为 JSON 报告。

        Args:
            output_path: 输出文件路径。

        Returns:
            JSON 字符串。
        """
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        data = _report_to_dict(self._report)
        json_str = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        Path(output_path).write_text(json_str, encoding="utf-8")
        return json_str

    def to_html(self, output_path: str) -> str:
        """导出为 HTML 报告。

        Args:
            output_path: 输出文件路径。

        Returns:
            HTML 字符串。
        """
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        html = render_html_report(self._report)
        Path(output_path).write_text(html, encoding="utf-8")
        return html

    def to_console(self) -> str:
        """输出控制台摘要。"""
        r = self._report
        lines = [
            "",
            "=" * 60,
            "📊 测试报告摘要",
            "=" * 60,
            f"  总计: {r.total}  通过: {r.passed}  失败: {r.failed}  错误: {r.errors}  跳过: {r.skipped}",
            f"  通过率: {r.pass_rate:.1%}",
            f"  总耗时: {r.duration_ms:.0f}ms",
        ]

        if r.failed > 0 or r.errors > 0:
            lines.append("")
            lines.append("❌ 失败/错误用例:")
            for case in r.test_cases:
                if case.outcome in ("failed", "error"):
                    lines.append(f"  - {case.node_id}")
                    if case.error_message:
                        lines.append(f"    错误: {case.error_message[:200]}")
                    if case.bug_location and case.bug_location.bug_candidates:
                        top = case.bug_location.bug_candidates[0]
                        lines.append(f"    🎯 Bug候选: {top.file_path}:{top.line_number} in {top.function_name}")
                    if case.captured_logs:
                        for log_line in case.captured_logs.split("\n")[:3]:
                            lines.append(f"    {log_line}")

        # 耗时最长的 5 个测试
        if r.test_cases:
            by_duration = sorted(r.test_cases, key=lambda c: c.duration_ms, reverse=True)
            lines.append("")
            lines.append("⏱️ 最慢的 5 个测试:")
            for case in by_duration[:5]:
                lines.append(f"  {case.duration_ms:.0f}ms  {case.node_id}")

        lines.append("=" * 60)
        return "\n".join(lines)


# ── 内部辅助函数 ──────────────────────────────────────────


def _collect_env_info() -> dict[str, str]:
    """收集环境信息。"""
    import sys

    return {
        "python": sys.version,
        "platform": sys.platform,
        "cwd": os.getcwd(),
        "user": os.getenv("USER", os.getenv("USERNAME", "unknown")),
    }


def _report_to_dict(report: TestReport) -> dict[str, Any]:
    """将 TestReport 转为可序列化字典。"""
    cases: list[dict[str, Any]] = []
    for case in report.test_cases:
        case_dict: dict[str, Any] = {
            "node_id": case.node_id,
            "name": case.name,
            "outcome": case.outcome,
            "duration_ms": case.duration_ms,
            "file_path": case.file_path,
            "line_number": case.line_number,
            "error_message": case.error_message,
            "traceback": case.traceback,
            "captured_logs": case.captured_logs,
        }
        if case.bug_location:
            case_dict["bug_location"] = {
                "assertion": (
                    f"{case.bug_location.assertion_location.file_path}:"
                    f"{case.bug_location.assertion_location.line_number}"
                    if case.bug_location.assertion_location
                    else None
                ),
                "candidates": [
                    {
                        "file": c.file_path,
                        "line": c.line_number,
                        "function": c.function_name,
                        "code": c.code_line,
                    }
                    for c in case.bug_location.bug_candidates
                ],
                "source_files": case.bug_location.source_files,
            }
        cases.append(case_dict)

    return {
        "timestamp": report.timestamp,
        "summary": {
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "errors": report.errors,
            "skipped": report.skipped,
            "pass_rate": report.pass_rate,
            "duration_ms": report.duration_ms,
        },
        "test_cases": cases,
        "environment": report.environment,
    }



__all__ = ["ReportGenerator", "TestReport", "TestCaseResult"]
