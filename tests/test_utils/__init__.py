"""测试增强工具包。

提供三大能力：
1. ``locate_bug`` / ``BugLocation`` — 测试失败时自动定位 bug 位置（文件+行号+上下文代码）
2. ``LogCollector``  — 测试失败时自动收集相关日志
3. ``ReportGenerator`` — 生成结构化测试报告（JSON/HTML）
"""

from tests.test_utils.bug_locator import BugLocation, CodeLocation, locate_bug
from tests.test_utils.log_collector import LogCaptureResult, LogCollector, LogEntry
from tests.test_utils.report_generator import ReportGenerator, TestCaseResult, TestReport

__all__ = [
    "locate_bug",
    "BugLocation",
    "CodeLocation",
    "LogCollector",
    "LogCaptureResult",
    "LogEntry",
    "ReportGenerator",
    "TestReport",
    "TestCaseResult",
]
