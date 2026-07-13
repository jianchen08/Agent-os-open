"""报告生成器测试。

覆盖模块：tests/test_utils/report_generator.py

测试场景：
- ReportGenerator 添加测试用例结果
- TestReport 通过率/失败率计算
- JSON 报告生成
- HTML 报告生成
- 控制台摘要输出
- TestCaseResult / TestReport 数据结构
- Bug 定位集成
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

import pytest

from tests.test_utils.report_generator import (
    ReportGenerator,
    TestCaseResult,
    TestReport,
    _collect_env_info,
    _report_to_dict,
)


class TestTestCaseResult:
    """TestCaseResult 数据类测试。"""

    def test_default_values(self) -> None:
        """默认值正确。"""
        case = TestCaseResult(
            node_id="tests/test_a.py::test_x",
            name="test_x",
            outcome="passed",
            duration_ms=50.0,
        )
        assert case.file_path == ""
        assert case.line_number == 0
        assert case.error_message == ""
        assert case.traceback == ""
        assert case.bug_location is None
        assert case.captured_logs == ""


class TestTestReport:
    """TestReport 数据类测试。"""

    def test_initial_state(self) -> None:
        """初始状态所有计数为 0。"""
        report = TestReport()
        assert report.total == 0
        assert report.passed == 0
        assert report.failed == 0
        assert report.errors == 0
        assert report.skipped == 0
        assert report.duration_ms == 0.0

    def test_pass_rate_zero_when_empty(self) -> None:
        """无测试时通过率为 0。"""
        report = TestReport()
        assert report.pass_rate == 0.0

    def test_pass_rate_calculation(self) -> None:
        """通过率计算正确。"""
        report = TestReport(total=10, passed=8, failed=2)
        assert report.pass_rate == 0.8

    def test_pass_rate_all_passed(self) -> None:
        """全部通过时通过率为 1.0。"""
        report = TestReport(total=5, passed=5)
        assert report.pass_rate == 1.0

    def test_fail_rate_calculation(self) -> None:
        """失败率计算正确。"""
        report = TestReport(total=10, failed=3)
        assert report.fail_rate == 0.3

    def test_fail_rate_zero_when_empty(self) -> None:
        """无测试时失败率为 0。"""
        report = TestReport()
        assert report.fail_rate == 0.0

    def test_add_case_passed(self) -> None:
        """添加 passed 用例更新计数。"""
        report = TestReport()
        case = TestCaseResult("t::a", "a", "passed", 10.0)
        report.add_case(case)
        assert report.total == 1
        assert report.passed == 1
        assert report.duration_ms == 10.0

    def test_add_case_failed(self) -> None:
        """添加 failed 用例更新计数。"""
        report = TestReport()
        case = TestCaseResult("t::b", "b", "failed", 20.0)
        report.add_case(case)
        assert report.total == 1
        assert report.failed == 1
        assert report.duration_ms == 20.0

    def test_add_case_error(self) -> None:
        """添加 error 用例更新计数。"""
        report = TestReport()
        case = TestCaseResult("t::c", "c", "error", 5.0)
        report.add_case(case)
        assert report.total == 1
        assert report.errors == 1

    def test_add_case_skipped(self) -> None:
        """添加 skipped 用例更新计数。"""
        report = TestReport()
        case = TestCaseResult("t::d", "d", "skipped", 0.0)
        report.add_case(case)
        assert report.total == 1
        assert report.skipped == 1

    def test_add_multiple_cases(self) -> None:
        """添加多个用例累加计数。"""
        report = TestReport()
        report.add_case(TestCaseResult("t::a", "a", "passed", 10.0))
        report.add_case(TestCaseResult("t::b", "b", "failed", 20.0))
        report.add_case(TestCaseResult("t::c", "c", "passed", 30.0))
        assert report.total == 3
        assert report.passed == 2
        assert report.failed == 1
        assert report.duration_ms == 60.0
        assert len(report.test_cases) == 3


class TestReportGenerator:
    """ReportGenerator 测试。"""

    def test_initial_report_has_timestamp(self) -> None:
        """初始报告有时间戳。"""
        gen = ReportGenerator()
        assert gen.report.timestamp != ""

    def test_initial_report_has_environment(self) -> None:
        """初始报告有环境信息。"""
        gen = ReportGenerator()
        assert "python" in gen.report.environment
        assert "platform" in gen.report.environment

    def test_report_property(self) -> None:
        """report 属性返回当前报告。"""
        gen = ReportGenerator()
        assert isinstance(gen.report, TestReport)

    def test_add_case_passed(self) -> None:
        """添加 passed 用例。"""
        gen = ReportGenerator()
        gen.add_case(
            node_id="tests/test_a.py::test_pass",
            name="test_pass",
            outcome="passed",
            duration_ms=15.0,
        )
        assert gen.report.total == 1
        assert gen.report.passed == 1

    def test_add_case_failed(self) -> None:
        """添加 failed 用例。"""
        gen = ReportGenerator()
        gen.add_case(
            node_id="tests/test_b.py::test_fail",
            name="test_fail",
            outcome="failed",
            duration_ms=5.0,
            error_message="assert False",
            traceback="traceback text",
        )
        assert gen.report.total == 1
        assert gen.report.failed == 1
        case = gen.report.test_cases[0]
        assert case.error_message == "assert False"
        assert case.traceback == "traceback text"

    def test_add_case_with_file_and_line(self) -> None:
        """添加带文件路径和行号的用例。"""
        gen = ReportGenerator()
        gen.add_case(
            node_id="tests/test_c.py::test_loc",
            name="test_loc",
            outcome="passed",
            duration_ms=1.0,
            file_path="tests/test_c.py",
            line_number=42,
        )
        case = gen.report.test_cases[0]
        assert case.file_path == "tests/test_c.py"
        assert case.line_number == 42

    def test_add_case_skipped(self) -> None:
        """添加 skipped 用例。"""
        gen = ReportGenerator()
        gen.add_case(
            node_id="tests/test_d.py::test_skip",
            name="test_skip",
            outcome="skipped",
            duration_ms=0.0,
        )
        assert gen.report.skipped == 1


class TestReportGeneratorToJson:
    """ReportGenerator.to_json 测试。"""

    def test_generates_valid_json(self) -> None:
        """生成的 JSON 合法。"""
        gen = ReportGenerator()
        gen.add_case("t::a", "a", "passed", 10.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.json")
            result = gen.to_json(path)
            parsed = json.loads(result)
            assert isinstance(parsed, dict)

    def test_json_contains_summary(self) -> None:
        """JSON 包含 summary 部分。"""
        gen = ReportGenerator()
        gen.add_case("t::a", "a", "passed", 10.0)
        gen.add_case("t::b", "b", "failed", 20.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.json")
            result = gen.to_json(path)
            parsed = json.loads(result)
        assert "summary" in parsed
        assert parsed["summary"]["total"] == 2
        assert parsed["summary"]["passed"] == 1
        assert parsed["summary"]["failed"] == 1

    def test_json_summary_pass_rate(self) -> None:
        """JSON summary 包含通过率。"""
        gen = ReportGenerator()
        gen.add_case("t::a", "a", "passed", 10.0)
        gen.add_case("t::b", "b", "passed", 5.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.json")
            result = gen.to_json(path)
            parsed = json.loads(result)
        assert parsed["summary"]["pass_rate"] == 1.0

    def test_json_contains_test_cases(self) -> None:
        """JSON 包含 test_cases 部分。"""
        gen = ReportGenerator()
        gen.add_case("t::a", "a", "passed", 10.0, file_path="tests/a.py", line_number=5)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.json")
            result = gen.to_json(path)
            parsed = json.loads(result)
        cases = parsed["test_cases"]
        assert len(cases) == 1
        assert cases[0]["name"] == "a"
        assert cases[0]["outcome"] == "passed"
        assert cases[0]["duration_ms"] == 10.0
        assert cases[0]["file_path"] == "tests/a.py"
        assert cases[0]["line_number"] == 5

    def test_json_contains_environment(self) -> None:
        """JSON 包含 environment 部分。"""
        gen = ReportGenerator()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.json")
            result = gen.to_json(path)
            parsed = json.loads(result)
        assert "environment" in parsed
        assert "python" in parsed["environment"]

    def test_json_contains_timestamp(self) -> None:
        """JSON 包含时间戳。"""
        gen = ReportGenerator()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.json")
            result = gen.to_json(path)
            parsed = json.loads(result)
        assert "timestamp" in parsed
        assert len(parsed["timestamp"]) > 0

    def test_json_file_written(self) -> None:
        """JSON 文件实际写入磁盘。"""
        gen = ReportGenerator()
        gen.add_case("t::a", "a", "passed", 10.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sub", "report.json")
            gen.to_json(path)
            assert os.path.isfile(path)
            content = open(path, encoding="utf-8").read()
            assert "passed" in content

    def test_json_creates_parent_dirs(self) -> None:
        """自动创建父目录。"""
        gen = ReportGenerator()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "deep", "nested", "report.json")
            gen.to_json(path)
            assert os.path.isfile(path)


class TestReportGeneratorToHtml:
    """ReportGenerator.to_html 测试。"""

    def test_generates_html_string(self) -> None:
        """生成 HTML 字符串。"""
        gen = ReportGenerator()
        gen.add_case("t::a", "a", "passed", 10.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.html")
            result = gen.to_html(path)
            assert "<html" in result
            assert "</html>" in result

    def test_html_contains_test_data(self) -> None:
        """HTML 包含测试数据。"""
        gen = ReportGenerator()
        gen.add_case("t::a", "test_something", "passed", 15.0)
        gen.add_case("t::b", "test_fail", "failed", 30.0, error_message="assert False")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.html")
            result = gen.to_html(path)
        assert "test_something" in result
        assert "test_fail" in result
        assert "PASSED" in result
        assert "FAILED" in result

    def test_html_file_written(self) -> None:
        """HTML 文件实际写入磁盘。"""
        gen = ReportGenerator()
        gen.add_case("t::a", "a", "passed", 10.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.html")
            gen.to_html(path)
            assert os.path.isfile(path)

    def test_html_contains_summary_cards(self) -> None:
        """HTML 包含统计卡片。"""
        gen = ReportGenerator()
        gen.add_case("t::a", "a", "passed", 10.0)
        gen.add_case("t::b", "b", "failed", 5.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.html")
            result = gen.to_html(path)
        assert "总计" in result
        assert "通过" in result
        assert "失败" in result

    def test_html_contains_environment_info(self) -> None:
        """HTML 包含环境信息。"""
        gen = ReportGenerator()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.html")
            result = gen.to_html(path)
        assert "环境信息" in result

    def test_html_with_empty_report(self) -> None:
        """空报告也能生成 HTML。"""
        gen = ReportGenerator()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.html")
            result = gen.to_html(path)
        assert "<html" in result
        assert "总计" in result


class TestReportGeneratorToConsole:
    """ReportGenerator.to_console 测试。"""

    def test_console_summary_format(self) -> None:
        """控制台摘要格式正确。"""
        gen = ReportGenerator()
        gen.add_case("t::a", "a", "passed", 10.0)
        gen.add_case("t::b", "b", "failed", 5.0, error_message="assert False")
        output = gen.to_console()
        assert "测试报告摘要" in output
        assert "总计: 2" in output
        assert "通过: 1" in output
        assert "失败: 1" in output
        assert "通过率:" in output

    def test_console_shows_failed_cases(self) -> None:
        """失败用例在摘要中显示。"""
        gen = ReportGenerator()
        gen.add_case("t::a", "a", "failed", 5.0, error_message="boom")
        output = gen.to_console()
        assert "失败/错误用例" in output
        assert "t::a" in output

    def test_console_shows_slowest_tests(self) -> None:
        """摘要显示最慢的测试。"""
        gen = ReportGenerator()
        gen.add_case("t::a", "a", "passed", 100.0)
        gen.add_case("t::b", "b", "passed", 10.0)
        output = gen.to_console()
        assert "最慢" in output

    def test_console_empty_report(self) -> None:
        """空报告摘要。"""
        gen = ReportGenerator()
        output = gen.to_console()
        assert "总计: 0" in output
        assert "通过率: 0.0%" in output


class TestReportToDict:
    """_report_to_dict 内部函数测试。"""

    def test_basic_structure(self) -> None:
        """基本结构完整。"""
        report = TestReport(timestamp="2025-01-01T00:00:00")
        report.add_case(TestCaseResult("t::a", "a", "passed", 10.0))
        result = _report_to_dict(report)
        assert "timestamp" in result
        assert "summary" in result
        assert "test_cases" in result
        assert "environment" in result

    def test_case_dict_fields(self) -> None:
        """用例字典包含所有字段。"""
        report = TestReport()
        report.add_case(TestCaseResult(
            "t::a", "a", "failed", 5.0,
            file_path="tests/a.py",
            line_number=10,
            error_message="err",
            traceback="tb",
        ))
        result = _report_to_dict(report)
        case = result["test_cases"][0]
        assert case["node_id"] == "t::a"
        assert case["name"] == "a"
        assert case["outcome"] == "failed"
        assert case["duration_ms"] == 5.0
        assert case["file_path"] == "tests/a.py"
        assert case["line_number"] == 10
        assert case["error_message"] == "err"
        assert case["traceback"] == "tb"

    def test_case_without_bug_location(self) -> None:
        """无 bug_location 时不包含该字段。"""
        report = TestReport()
        report.add_case(TestCaseResult("t::a", "a", "passed", 10.0))
        result = _report_to_dict(report)
        case = result["test_cases"][0]
        assert "bug_location" not in case


class TestCollectEnvInfo:
    """_collect_env_info 测试。"""

    def test_returns_dict(self) -> None:
        """返回字典。"""
        info = _collect_env_info()
        assert isinstance(info, dict)

    def test_contains_python_version(self) -> None:
        """包含 Python 版本。"""
        info = _collect_env_info()
        assert "python" in info
        assert "3." in info["python"]

    def test_contains_platform(self) -> None:
        """包含平台信息。"""
        info = _collect_env_info()
        assert "platform" in info

    def test_contains_cwd(self) -> None:
        """包含工作目录。"""
        info = _collect_env_info()
        assert "cwd" in info
