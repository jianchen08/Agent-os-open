"""统一日志系统测试。

覆盖场景：
- LoggingConfig 从环境变量/字典构建
- LogContext 上下文绑定/解绑/scoped
- JsonFormatter 结构化输出格式
- StructuredFormatter 文本格式输出
- setup_logging 初始化与 handler 配置
- get_logger 兼容性
"""

from __future__ import annotations

import io
import json
import logging
import os

import pytest

from src.core.logging import (
    JsonFormatter,
    LogContext,
    LoggingConfig,
    StructuredFormatter,
    get_logger,
    setup_logging,
)


# ── LoggingConfig 测试 ──────────────────────────────────────────


class TestLoggingConfig:
    """LoggingConfig 配置测试。"""

    def test_default_values(self) -> None:
        """默认配置值为 INFO 级别、console 输出。"""
        config = LoggingConfig()
        assert config.level == logging.INFO
        assert config.output == "console"
        assert config.json_output is False

    def test_from_env_reads_log_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env 从 LOG_LEVEL 环境变量读取级别。"""
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        config = LoggingConfig.from_env()
        assert config.level == logging.DEBUG

    def test_from_env_reads_json_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env 从 LOG_JSON 环境变量读取 JSON 开关。"""
        monkeypatch.setenv("LOG_JSON", "true")
        config = LoggingConfig.from_env()
        assert config.json_output is True

    def test_from_env_reads_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """from_env 从 LOG_OUTPUT 环境变量读取输出目标。"""
        monkeypatch.setenv("LOG_OUTPUT", "both")
        config = LoggingConfig.from_env()
        assert config.output == "both"

    def test_from_env_invalid_level_falls_back_to_info(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无效的 LOG_LEVEL 回退到 INFO。"""
        monkeypatch.setenv("LOG_LEVEL", "INVALID")
        config = LoggingConfig.from_env()
        assert config.level == logging.INFO

    def test_from_dict_ignores_unknown_keys(self) -> None:
        """from_dict 忽略未知键。"""
        config = LoggingConfig.from_dict({"level": "WARNING", "unknown_key": 42})
        assert config.level == logging.WARNING

    def test_from_dict_string_level_converted(self) -> None:
        """from_dict 将字符串级别转为 int。"""
        config = LoggingConfig.from_dict({"level": "ERROR"})
        assert config.level == logging.ERROR

    def test_frozen_dataclass(self) -> None:
        """LoggingConfig 是不可变的。"""
        config = LoggingConfig()
        with pytest.raises(AttributeError):
            config.level = logging.DEBUG  # type: ignore[misc]

    def test_context_fields_include_trace_id(self) -> None:
        """默认 context_fields 包含 trace_id 和 agent_name。"""
        config = LoggingConfig()
        assert "trace_id" in config.context_fields
        assert "agent_name" in config.context_fields


# ── LogContext 测试 ──────────────────────────────────────────────


class TestLogContext:
    """LogContext 上下文追踪测试。"""

    def setup_method(self) -> None:
        """每个测试前清除上下文。"""
        LogContext.unbind()

    def teardown_method(self) -> None:
        """每个测试后清除上下文。"""
        LogContext.unbind()

    def test_default_value_is_dash(self) -> None:
        """未设置时返回 '-'。"""
        assert LogContext.get("trace_id") == "-"
        assert LogContext.get("request_id") == "-"

    def test_bind_and_get(self) -> None:
        """bind 设置后 get 返回值。"""
        LogContext.bind(trace_id="trace-abc", task_id="task-001")
        assert LogContext.get("trace_id") == "trace-abc"
        assert LogContext.get("task_id") == "task-001"

    def test_unbind_resets_all(self) -> None:
        """unbind 重置全部字段为 '-'。"""
        LogContext.bind(trace_id="abc", agent_name="灵汐")
        LogContext.unbind()
        assert LogContext.get("trace_id") == "-"
        assert LogContext.get("agent_name") == "-"

    def test_snapshot(self) -> None:
        """snapshot 返回全部字段快照。"""
        LogContext.bind(trace_id="t1", request_id="r1")
        snap = LogContext.snapshot()
        assert snap["trace_id"] == "t1"
        assert snap["request_id"] == "r1"
        assert "task_id" in snap

    def test_format_context(self) -> None:
        """format_context 输出短字符串。"""
        LogContext.bind(request_id="req-123", task_id="t-456")
        result = LogContext.format_context()
        assert "rid=req-123" in result
        assert "tid=t-456" in result

    def test_format_context_empty(self) -> None:
        """未设置任何字段时 format_context 返回 '-'。"""
        assert LogContext.format_context() == "-"

    def test_scoped_restores_previous(self) -> None:
        """scoped 退出后恢复之前的值。"""
        LogContext.bind(trace_id="outer")
        with LogContext.scoped(trace_id="inner"):
            assert LogContext.get("trace_id") == "inner"
        assert LogContext.get("trace_id") == "outer"

    def test_scoped_restores_on_exception(self) -> None:
        """scoped 在异常时也恢复。"""
        LogContext.bind(trace_id="outer")
        with pytest.raises(RuntimeError):
            with LogContext.scoped(trace_id="inner"):
                raise RuntimeError("test")
        assert LogContext.get("trace_id") == "outer"

    def test_get_unknown_field(self) -> None:
        """获取未注册字段返回 '-'。"""
        assert LogContext.get("nonexistent_field") == "-"

    def test_register_custom_field(self) -> None:
        """注册自定义字段后可以读写。"""
        LogContext.register_field("custom_field")
        LogContext.bind(custom_field="val")
        assert LogContext.get("custom_field") == "val"
        LogContext.unbind()


# ── JsonFormatter 测试 ──────────────────────────────────────────


class TestJsonFormatter:
    """JSON 格式化器测试。"""

    def test_basic_output_structure(self) -> None:
        """JSON 输出包含必填字段。"""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test.module",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="测试消息",
            args=None,
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)

        assert data["level"] == "INFO"
        assert data["message"] == "测试消息"
        assert data["logger"] == "test.module"
        assert "timestamp" in data
        assert "module" in data
        assert "function" in data
        assert "line" in data

    def test_includes_context_fields(self) -> None:
        """JSON 输出包含上下文追踪字段。"""
        LogContext.unbind()
        LogContext.bind(trace_id="trace-xyz", task_id="t-001")
        try:
            formatter = JsonFormatter(
                context_fields=("trace_id", "task_id", "agent_name")
            )
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=1,
                msg="msg",
                args=None,
                exc_info=None,
            )
            data = json.loads(formatter.format(record))
            assert data["trace_id"] == "trace-xyz"
            assert data["task_id"] == "t-001"
            assert data["agent_name"] == "-"
        finally:
            LogContext.unbind()

    def test_includes_exception_info(self) -> None:
        """JSON 输出包含异常信息。"""
        formatter = JsonFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
            record = logging.LogRecord(
                name="test",
                level=logging.ERROR,
                pathname="",
                lineno=1,
                msg="error occurred",
                args=None,
                exc_info=exc_info,
            )
            data = json.loads(formatter.format(record))
            assert "exception" in data
            assert data["exception"]["type"] == "ValueError"
            assert data["exception"]["message"] == "test error"

    def test_extra_fields_included(self) -> None:
        """JSON 输出包含 extra 字段。"""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="msg",
            args=None,
            exc_info=None,
        )
        record.duration_ms = 150  # type: ignore[attr-defined]
        data = json.loads(formatter.format(record))
        assert data["duration_ms"] == 150

    def test_non_serializable_value_converted_to_str(self) -> None:
        """不可序列化的值转为字符串。"""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="msg",
            args=None,
            exc_info=None,
        )
        record.custom_obj = object()  # type: ignore[attr-defined]
        output = formatter.format(record)
        data = json.loads(output)
        assert isinstance(data["custom_obj"], str)

    def test_chinese_not_escaped(self) -> None:
        """中文字符不被转义（ensure_ascii=False）。"""
        formatter = JsonFormatter(ensure_ascii=False)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="中文测试消息",
            args=None,
            exc_info=None,
        )
        output = formatter.format(record)
        assert "中文测试消息" in output


# ── StructuredFormatter 测试 ────────────────────────────────────


class TestStructuredFormatter:
    """结构化文本格式化器测试。"""

    def test_basic_format(self) -> None:
        """基础格式包含时间、级别、名称、消息。"""
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="test.module",
            level=logging.WARNING,
            pathname="test.py",
            lineno=10,
            msg="警告消息",
            args=None,
            exc_info=None,
        )
        output = formatter.format(record)
        assert "WARNING" in output
        assert "test.module" in output
        assert "警告消息" in output

    def test_context_injected(self) -> None:
        """上下文字段注入到日志行。"""
        LogContext.unbind()
        LogContext.bind(request_id="req-abc")
        try:
            formatter = StructuredFormatter()
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=1,
                msg="msg",
                args=None,
                exc_info=None,
            )
            output = formatter.format(record)
            assert "rid=req-abc" in output
        finally:
            LogContext.unbind()

    def test_extra_fields_appended(self) -> None:
        """extra 字段追加到消息末尾。"""
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="msg",
            args=None,
            exc_info=None,
        )
        record.duration_ms = 100  # type: ignore[attr-defined]
        output = formatter.format(record)
        assert "duration_ms=100" in output


# ── get_logger / setup_logging 测试 ────────────────────────────


class TestGetLogger:
    """get_logger 兼容性测试。"""

    def test_returns_standard_logger(self) -> None:
        """get_logger 返回标准 logging.Logger。"""
        log = get_logger("test.module")
        assert isinstance(log, logging.Logger)

    def test_same_name_returns_same_instance(self) -> None:
        """同名 logger 返回同一实例。"""
        log1 = get_logger("test.same")
        log2 = logging.getLogger("test.same")
        assert log1 is log2

    def test_none_returns_root(self) -> None:
        """None 返回 root logger。"""
        log = get_logger(None)
        assert log is logging.getLogger()


class TestSetupLogging:
    """setup_logging 初始化测试。"""

    def test_idempotent(self) -> None:
        """重复调用不报错（跳过）。"""
        setup_logging(LoggingConfig(output="console"))
        # 第二次调用应跳过
        setup_logging(LoggingConfig(output="console"))

    def test_reset_reconfigures(self) -> None:
        """reset=True 强制重新配置。"""
        config = LoggingConfig(output="console", level=logging.DEBUG)
        setup_logging(config, reset=True)
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_console_handler_added(self) -> None:
        """console 输出模式添加 StreamHandler。"""
        setup_logging(LoggingConfig(output="console"), reset=True)
        root = logging.getLogger()
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)

    def test_third_party_quieted(self) -> None:
        """第三方库日志被降级。"""
        setup_logging(
            LoggingConfig(output="console", third_party_level=logging.ERROR),
            reset=True,
        )
        assert logging.getLogger("urllib3").level == logging.ERROR
        assert logging.getLogger("httpx").level == logging.ERROR
