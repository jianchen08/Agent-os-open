"""日志收集器测试。

覆盖模块：tests/test_utils/log_collector.py

测试场景：
- LogCollector start/stop 生命周期
- LogCollector 捕获日志并按级别统计
- LogCaptureResult 的 error_count / warning_count / errors / warnings
- LogEntry 数据结构
- min_level 过滤
- 多次 start/stop 重置
"""

from __future__ import annotations

import logging

import pytest

from src.core.logging.context import LogContext
from tests.test_utils.log_collector import LogCollector, LogCaptureResult, LogEntry


class TestLogEntry:
    """LogEntry 数据类测试。"""

    def test_default_extra(self) -> None:
        """extra 默认为空字典。"""
        entry = LogEntry(
            timestamp="2025-01-01 00:00:00",
            level="INFO",
            logger_name="test",
            message="hello",
            context={},
        )
        assert entry.extra == {}

    def test_fields(self) -> None:
        """所有字段正确存储。"""
        entry = LogEntry(
            timestamp="2025-01-01 00:00:00",
            level="ERROR",
            logger_name="src.core",
            message="fail",
            context={"request_id": "r1"},
            extra={"duration_ms": 100},
        )
        assert entry.level == "ERROR"
        assert entry.logger_name == "src.core"
        assert entry.message == "fail"
        assert entry.context["request_id"] == "r1"
        assert entry.extra["duration_ms"] == 100


class TestLogCaptureResult:
    """LogCaptureResult 测试。"""

    def _make_entry(self, level: str, msg: str = "test") -> LogEntry:
        return LogEntry(
            timestamp="2025-01-01 00:00:00",
            level=level,
            logger_name="test.module",
            message=msg,
            context={},
        )

    def test_error_count_zero(self) -> None:
        """无错误时 error_count 为 0。"""
        result = LogCaptureResult()
        assert result.error_count == 0

    def test_error_count_with_errors(self) -> None:
        """error_count 统计 ERROR 和 CRITICAL。"""
        result = LogCaptureResult()
        result.entries.append(self._make_entry("ERROR"))
        result.entries.append(self._make_entry("CRITICAL"))
        result.entries.append(self._make_entry("INFO"))
        result._level_counts["ERROR"] += 1
        result._level_counts["CRITICAL"] += 1
        result._level_counts["INFO"] += 1
        assert result.error_count == 2

    def test_warning_count(self) -> None:
        """warning_count 统计 WARNING。"""
        result = LogCaptureResult()
        result.entries.append(self._make_entry("WARNING"))
        result.entries.append(self._make_entry("WARNING"))
        result._level_counts["WARNING"] += 2
        assert result.warning_count == 2

    def test_errors_returns_only_errors(self) -> None:
        """errors() 只返回 ERROR 及以上级别。"""
        result = LogCaptureResult()
        err_entry = self._make_entry("ERROR", "err1")
        warn_entry = self._make_entry("WARNING", "warn1")
        crit_entry = self._make_entry("CRITICAL", "crit1")
        result.entries.extend([err_entry, warn_entry, crit_entry])
        errors = result.errors()
        assert len(errors) == 2
        assert err_entry in errors
        assert crit_entry in errors

    def test_warnings_returns_only_warnings(self) -> None:
        """warnings() 只返回 WARNING 级别。"""
        result = LogCaptureResult()
        warn_entry = self._make_entry("WARNING")
        err_entry = self._make_entry("ERROR")
        result.entries.extend([warn_entry, err_entry])
        warnings = result.warnings()
        assert len(warnings) == 1
        assert warn_entry in warnings

    def test_for_logger_filter(self) -> None:
        """for_logger 按名称前缀过滤。"""
        result = LogCaptureResult()
        e1 = LogEntry("ts", "INFO", "src.core.engine", "msg1", {})
        e2 = LogEntry("ts", "INFO", "src.core.config", "msg2", {})
        e3 = LogEntry("ts", "INFO", "src.pipeline.engine", "msg3", {})
        result.entries.extend([e1, e2, e3])
        filtered = result.for_logger("src.core")
        assert len(filtered) == 2
        assert e1 in filtered
        assert e2 in filtered

    def test_format_errors_no_errors(self) -> None:
        """无错误时 format_errors 返回提示。"""
        result = LogCaptureResult()
        assert result.format_errors() == "无错误日志。"

    def test_format_errors_with_errors(self) -> None:
        """有错误时 format_errors 包含错误信息。"""
        result = LogCaptureResult()
        result.entries.append(LogEntry(
            "ts", "ERROR", "src.core", "something failed",
            {"request_id": "r1"},
        ))
        result._level_counts["ERROR"] += 1
        formatted = result.format_errors()
        assert "ERROR" in formatted
        assert "something failed" in formatted

    def test_format_errors_with_context(self) -> None:
        """format_errors 包含上下文信息。"""
        result = LogCaptureResult()
        result.entries.append(LogEntry(
            "ts", "ERROR", "src.core", "fail",
            {"request_id": "r1", "task_id": "-"},
        ))
        result._level_counts["ERROR"] += 1
        formatted = result.format_errors()
        assert "request_id=r1" in formatted


class TestLogCollector:
    """LogCollector 测试。"""

    def setup_method(self) -> None:
        LogContext.unbind()

    def teardown_method(self) -> None:
        LogContext.unbind()

    def test_initial_state(self) -> None:
        """初始状态为未激活。"""
        collector = LogCollector()
        assert collector.active is False

    def test_start_activates(self) -> None:
        """start 激活收集器。"""
        collector = LogCollector()
        collector.start()
        try:
            assert collector.active is True
        finally:
            collector.stop()

    def test_stop_deactivates(self) -> None:
        """stop 停止收集器。"""
        collector = LogCollector()
        collector.start()
        collector.stop()
        assert collector.active is False

    def test_double_start_idempotent(self) -> None:
        """重复 start 不会添加多个 handler。"""
        collector = LogCollector()
        collector.start()
        handler_count = len(logging.root.handlers)
        collector.start()  # 第二次应该跳过
        try:
            assert len(logging.root.handlers) == handler_count
        finally:
            collector.stop()

    def test_double_stop_safe(self) -> None:
        """重复 stop 不会报错。"""
        collector = LogCollector()
        collector.start()
        collector.stop()
        collector.stop()  # 不应抛异常

    def test_captures_warning_logs(self) -> None:
        """默认捕获 WARNING 及以上级别的日志。"""
        collector = LogCollector()
        logger = logging.getLogger("test.capture.warning")
        collector.start()
        try:
            logger.warning("test warning")
            logger.info("test info")  # 低于 WARNING，不应捕获
            result = collector.get_result()
            assert result.warning_count == 1
            assert any("test warning" in e.message for e in result.entries)
        finally:
            collector.stop()

    def test_captures_error_logs(self) -> None:
        """捕获 ERROR 级别日志。"""
        collector = LogCollector()
        logger = logging.getLogger("test.capture.error")
        collector.start()
        try:
            logger.error("test error")
            result = collector.get_result()
            assert result.error_count == 1
        finally:
            collector.stop()

    def test_captures_with_custom_level(self) -> None:
        """自定义最低级别捕获 DEBUG 日志。"""
        collector = LogCollector()
        logger = logging.getLogger("test.capture.debug")
        collector.start(min_level=logging.DEBUG)
        try:
            logger.debug("test debug")
            result = collector.get_result()
            assert len(result.entries) >= 1
            assert any("test debug" in e.message for e in result.entries)
        finally:
            collector.stop()

    def test_captures_context_info(self) -> None:
        """捕获的日志包含 LogContext 信息。"""
        collector = LogCollector()
        logger = logging.getLogger("test.capture.ctx")
        LogContext.bind(request_id="req-001", task_id="task-001")
        collector.start(min_level=logging.WARNING)
        try:
            logger.warning("context test")
            result = collector.get_result()
            assert len(result.entries) >= 1
            entry = result.entries[0]
            assert entry.context.get("request_id") == "req-001"
            assert entry.context.get("task_id") == "task-001"
        finally:
            collector.stop()

    def test_start_resets_result(self) -> None:
        """再次 start 会重置收集结果。"""
        collector = LogCollector()
        logger = logging.getLogger("test.capture.reset")
        collector.start()
        logger.warning("first")
        collector.stop()

        collector.start()
        try:
            result = collector.get_result()
            assert len(result.entries) == 0
        finally:
            collector.stop()

    def test_get_result_while_active(self) -> None:
        """收集期间可以获取中间结果。"""
        collector = LogCollector()
        logger = logging.getLogger("test.capture.mid")
        collector.start(min_level=logging.WARNING)
        try:
            logger.warning("msg1")
            result1 = collector.get_result()
            assert len(result1.entries) >= 1

            logger.error("msg2")
            result2 = collector.get_result()
            assert len(result2.entries) >= 2
        finally:
            collector.stop()

    def test_stop_removes_handler(self) -> None:
        """stop 从 root logger 移除 handler。"""
        collector = LogCollector()
        initial_count = len(logging.root.handlers)
        collector.start()
        assert len(logging.root.handlers) == initial_count + 1
        collector.stop()
        assert len(logging.root.handlers) == initial_count

    def test_captures_from_multiple_loggers(self) -> None:
        """可以从多个 logger 捕获日志。"""
        collector = LogCollector()
        logger_a = logging.getLogger("test.module_a")
        logger_b = logging.getLogger("test.module_b")
        collector.start(min_level=logging.WARNING)
        try:
            logger_a.warning("warn from a")
            logger_b.error("error from b")
            result = collector.get_result()
            assert len(result.entries) >= 2
        finally:
            collector.stop()
