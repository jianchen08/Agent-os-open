"""统一日志系统测试。

覆盖模块：
- src/core/logging/config.py   — LoggingConfig（环境变量/字典初始化、级别解析、输出目标）
- src/core/logging/context.py  — LogContext（bind/unbind/scoped/get/snapshot/format_context）
- src/core/logging/formatters.py — StructuredFormatter / JsonFormatter（格式化输出、上下文注入）
- src/core/logging/__init__.py — setup_logging / get_logger（全局初始化、handler 管理）
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from unittest.mock import patch

import pytest

from src.core.logging import get_logger, setup_logging
from src.core.logging.config import LoggingConfig, OutputTarget, _LEVEL_MAP, _parse_output
from src.core.logging.context import LogContext
from src.core.logging.filters import ContextFilter
from src.core.logging.formatters import JsonFormatter, StructuredFormatter


# ═══════════════════════════════════════════════════════════════════
# LoggingConfig 测试
# ═══════════════════════════════════════════════════════════════════


class TestLoggingConfigDefaults:
    """LoggingConfig 默认值测试。"""

    def test_default_level_is_info(self) -> None:
        """默认日志级别为 INFO。"""
        config = LoggingConfig()
        assert config.level == logging.INFO

    def test_default_output_is_console(self) -> None:
        """默认输出目标为 console。"""
        config = LoggingConfig()
        assert config.output == "console"

    def test_default_json_output_is_false(self) -> None:
        """默认不启用 JSON 输出。"""
        config = LoggingConfig()
        assert config.json_output is False

    def test_default_file_path(self) -> None:
        """默认日志文件路径为 logs/app.log。"""
        config = LoggingConfig()
        assert config.file_path == "logs/app.log"

    def test_default_context_fields(self) -> None:
        """默认上下文字段包含 request_id, task_id, session_id。"""
        config = LoggingConfig()
        assert "request_id" in config.context_fields
        assert "task_id" in config.context_fields
        assert "session_id" in config.context_fields

    def test_frozen_dataclass(self) -> None:
        """LoggingConfig 是不可变数据类，赋值应抛异常。"""
        config = LoggingConfig()
        with pytest.raises(AttributeError):
            config.level = logging.DEBUG  # type: ignore[misc]


class TestLoggingConfigFromEnv:
    """LoggingConfig.from_env() 测试。"""

    def test_from_env_defaults(self) -> None:
        """无环境变量时使用默认值。"""
        with patch.dict(os.environ, {}, clear=False):
            # 清除可能存在的日志相关环境变量
            env_copy = os.environ.copy()
            for key in list(env_copy):
                if key.startswith("LOG_"):
                    del os.environ[key]
            config = LoggingConfig.from_env()
        assert config.level == logging.INFO
        assert config.output == "console"
        assert config.json_output is False

    def test_from_env_log_level_debug(self) -> None:
        """LOG_LEVEL=DEBUG 时级别为 DEBUG。"""
        with patch.dict(os.environ, {"LOG_LEVEL": "DEBUG"}):
            config = LoggingConfig.from_env()
        assert config.level == logging.DEBUG

    def test_from_env_log_level_case_insensitive(self) -> None:
        """LOG_LEVEL 不区分大小写。"""
        with patch.dict(os.environ, {"LOG_LEVEL": "warning"}):
            config = LoggingConfig.from_env()
        assert config.level == logging.WARNING

    def test_from_env_log_level_error(self) -> None:
        """LOG_LEVEL=ERROR 时级别为 ERROR。"""
        with patch.dict(os.environ, {"LOG_LEVEL": "ERROR"}):
            config = LoggingConfig.from_env()
        assert config.level == logging.ERROR

    def test_from_env_log_level_critical(self) -> None:
        """LOG_LEVEL=CRITICAL 时级别为 CRITICAL。"""
        with patch.dict(os.environ, {"LOG_LEVEL": "CRITICAL"}):
            config = LoggingConfig.from_env()
        assert config.level == logging.CRITICAL

    def test_from_env_log_level_invalid_falls_to_info(self) -> None:
        """无效的 LOG_LEVEL 回退到 INFO。"""
        with patch.dict(os.environ, {"LOG_LEVEL": "INVALID"}):
            config = LoggingConfig.from_env()
        assert config.level == logging.INFO

    def test_from_env_json_output_true(self) -> None:
        """LOG_JSON=1 启用 JSON 输出。"""
        with patch.dict(os.environ, {"LOG_JSON": "1"}):
            config = LoggingConfig.from_env()
        assert config.json_output is True

    def test_from_env_json_output_true_string(self) -> None:
        """LOG_JSON=true 启用 JSON 输出。"""
        with patch.dict(os.environ, {"LOG_JSON": "true"}):
            config = LoggingConfig.from_env()
        assert config.json_output is True

    def test_from_env_json_output_false(self) -> None:
        """LOG_JSON=0 不启用 JSON 输出。"""
        with patch.dict(os.environ, {"LOG_JSON": "0"}):
            config = LoggingConfig.from_env()
        assert config.json_output is False

    def test_from_env_output_file(self) -> None:
        """LOG_OUTPUT=file 时输出到文件。"""
        with patch.dict(os.environ, {"LOG_OUTPUT": "file"}):
            config = LoggingConfig.from_env()
        assert config.output == "file"

    def test_from_env_output_both(self) -> None:
        """LOG_OUTPUT=both 时同时输出到控制台和文件。"""
        with patch.dict(os.environ, {"LOG_OUTPUT": "both"}):
            config = LoggingConfig.from_env()
        assert config.output == "both"

    def test_from_env_custom_file_path(self) -> None:
        """LOG_FILE 自定义日志文件路径。"""
        with patch.dict(os.environ, {"LOG_FILE": "/var/log/custom.log"}):
            config = LoggingConfig.from_env()
        assert config.file_path == "/var/log/custom.log"

    def test_from_env_file_max_bytes(self) -> None:
        """LOG_FILE_MAX_BYTES 自定义单文件最大字节数。"""
        with patch.dict(os.environ, {"LOG_FILE_MAX_BYTES": "10485760"}):
            config = LoggingConfig.from_env()
        assert config.file_max_bytes == 10485760

    def test_from_env_file_backup_count(self) -> None:
        """LOG_FILE_BACKUPS 自定义保留轮转文件数。"""
        with patch.dict(os.environ, {"LOG_FILE_BACKUPS": "3"}):
            config = LoggingConfig.from_env()
        assert config.file_backup_count == 3


class TestLoggingConfigFromDict:
    """LoggingConfig.from_dict() 测试。"""

    def test_from_dict_basic(self) -> None:
        """从字典构建基本配置。"""
        data = {"level": "DEBUG", "output": "file"}
        config = LoggingConfig.from_dict(data)
        assert config.level == logging.DEBUG
        assert config.output == "file"

    def test_from_dict_ignores_unknown_keys(self) -> None:
        """忽略字典中的未知键。"""
        data = {"level": "INFO", "unknown_key": "value"}
        config = LoggingConfig.from_dict(data)
        assert config.level == logging.INFO

    def test_from_dict_level_as_string(self) -> None:
        """level 可以用字符串传入。"""
        data = {"level": "WARNING"}
        config = LoggingConfig.from_dict(data)
        assert config.level == logging.WARNING

    def test_from_dict_level_as_int(self) -> None:
        """level 可以直接用 int 传入。"""
        data = {"level": 10}
        config = LoggingConfig.from_dict(data)
        assert config.level == 10

    def test_from_dict_third_party_level_as_string(self) -> None:
        """third_party_level 可以用字符串传入。"""
        data = {"third_party_level": "ERROR"}
        config = LoggingConfig.from_dict(data)
        assert config.third_party_level == logging.ERROR

    def test_from_dict_empty(self) -> None:
        """空字典使用全部默认值。"""
        config = LoggingConfig.from_dict({})
        assert config.level == logging.INFO
        assert config.output == "console"


class TestParseOutput:
    """_parse_output 辅助函数测试。"""

    def test_console(self) -> None:
        assert _parse_output("console") == "console"

    def test_file(self) -> None:
        assert _parse_output("file") == "file"

    def test_both(self) -> None:
        assert _parse_output("both") == "both"

    def test_unknown_falls_to_console(self) -> None:
        assert _parse_output("something") == "console"


class TestLevelMap:
    """_LEVEL_MAP 映射完整性测试。"""

    def test_all_standard_levels_present(self) -> None:
        """所有标准级别都在映射中。"""
        for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            assert name in _LEVEL_MAP

    def test_level_values_are_logging_constants(self) -> None:
        """映射值是 logging 模块的常量。"""
        assert _LEVEL_MAP["DEBUG"] == logging.DEBUG
        assert _LEVEL_MAP["INFO"] == logging.INFO
        assert _LEVEL_MAP["WARNING"] == logging.WARNING
        assert _LEVEL_MAP["ERROR"] == logging.ERROR
        assert _LEVEL_MAP["CRITICAL"] == logging.CRITICAL


# ═══════════════════════════════════════════════════════════════════
# LogContext 测试
# ═══════════════════════════════════════════════════════════════════


class TestLogContextBindUnbind:
    """LogContext.bind / unbind 测试。"""

    def setup_method(self) -> None:
        """每个测试前清除上下文。"""
        LogContext.unbind()

    def teardown_method(self) -> None:
        """每个测试后清除上下文。"""
        LogContext.unbind()

    def test_bind_request_id(self) -> None:
        """bind 设置 request_id。"""
        LogContext.bind(request_id="abc123")
        assert LogContext.get("request_id") == "abc123"

    def test_bind_task_id(self) -> None:
        """bind 设置 task_id。"""
        LogContext.bind(task_id="t-001")
        assert LogContext.get("task_id") == "t-001"

    def test_bind_multiple_fields(self) -> None:
        """bind 同时设置多个字段。"""
        LogContext.bind(request_id="abc", task_id="t-1", session_id="s-42")
        assert LogContext.get("request_id") == "abc"
        assert LogContext.get("task_id") == "t-1"
        assert LogContext.get("session_id") == "s-42"

    def test_bind_overwrites(self) -> None:
        """bind 覆盖已有值。"""
        LogContext.bind(request_id="old")
        LogContext.bind(request_id="new")
        assert LogContext.get("request_id") == "new"

    def test_bind_ignores_unknown_key(self) -> None:
        """bind 忽略未注册的键。"""
        LogContext.bind(unknown_field="value")
        assert LogContext.get("unknown_field") == "-"

    def test_unbind_clears_all(self) -> None:
        """unbind 清除所有字段恢复默认值。"""
        LogContext.bind(request_id="abc", task_id="t-1")
        LogContext.unbind()
        assert LogContext.get("request_id") == "-"
        assert LogContext.get("task_id") == "-"
        assert LogContext.get("session_id") == "-"

    def test_get_default_value(self) -> None:
        """未设置的字段返回 '-'。"""
        assert LogContext.get("request_id") == "-"


class TestLogContextSnapshot:
    """LogContext.snapshot() 测试。"""

    def setup_method(self) -> None:
        LogContext.unbind()

    def teardown_method(self) -> None:
        LogContext.unbind()

    def test_snapshot_empty(self) -> None:
        """无绑定时快照值为默认。"""
        snap = LogContext.snapshot()
        assert all(v == "-" for v in snap.values())

    def test_snapshot_with_values(self) -> None:
        """快照包含当前绑定的值。"""
        LogContext.bind(request_id="r1", task_id="t1")
        snap = LogContext.snapshot()
        assert snap["request_id"] == "r1"
        assert snap["task_id"] == "t1"
        assert snap["session_id"] == "-"

    def test_snapshot_is_copy(self) -> None:
        """快照是独立副本，修改不影响上下文。"""
        LogContext.bind(request_id="r1")
        snap = LogContext.snapshot()
        snap["request_id"] = "modified"
        assert LogContext.get("request_id") == "r1"


class TestLogContextFormatContext:
    """LogContext.format_context() 测试。"""

    def setup_method(self) -> None:
        LogContext.unbind()

    def teardown_method(self) -> None:
        LogContext.unbind()

    def test_format_empty(self) -> None:
        """无绑定时返回 '-'。"""
        assert LogContext.format_context() == "-"

    def test_format_with_request_id(self) -> None:
        """只设置 request_id 时格式为 rid=xxx。"""
        LogContext.bind(request_id="abc123")
        result = LogContext.format_context()
        assert "rid=abc123" in result

    def test_format_with_multiple_fields(self) -> None:
        """多个字段时用空格分隔。"""
        LogContext.bind(request_id="r1", task_id="t1", session_id="s1")
        result = LogContext.format_context()
        assert "rid=r1" in result
        assert "tid=t1" in result
        assert "sid=s1" in result

    def test_format_skips_unset_fields(self) -> None:
        """未设置的字段不出现在格式化结果中。"""
        LogContext.bind(request_id="r1")
        result = LogContext.format_context()
        assert "tid=" not in result
        assert "sid=" not in result


class TestLogContextScoped:
    """LogContext.scoped() 上下文管理器测试。"""

    def setup_method(self) -> None:
        LogContext.unbind()

    def teardown_method(self) -> None:
        LogContext.unbind()

    def test_scoped_sets_and_restores(self) -> None:
        """scoped 临时设置字段，退出后恢复。"""
        LogContext.bind(request_id="outer")
        with LogContext.scoped(request_id="inner"):
            assert LogContext.get("request_id") == "inner"
        assert LogContext.get("request_id") == "outer"

    def test_scoped_restores_default_on_empty(self) -> None:
        """scoped 在无外部绑定时恢复默认值。"""
        with LogContext.scoped(request_id="temp"):
            assert LogContext.get("request_id") == "temp"
        assert LogContext.get("request_id") == "-"

    def test_scoped_multiple_fields(self) -> None:
        """scoped 同时设置多个字段。"""
        with LogContext.scoped(request_id="r", task_id="t"):
            assert LogContext.get("request_id") == "r"
            assert LogContext.get("task_id") == "t"
        assert LogContext.get("request_id") == "-"
        assert LogContext.get("task_id") == "-"

    def test_scoped_restores_on_exception(self) -> None:
        """scoped 在异常时也恢复。"""
        LogContext.bind(request_id="safe")
        with pytest.raises(ValueError):
            with LogContext.scoped(request_id="danger"):
                raise ValueError("boom")
        assert LogContext.get("request_id") == "safe"

    def test_scoped_nested(self) -> None:
        """scoped 嵌套使用时内层退出恢复到外层值。"""
        LogContext.bind(request_id="level0")
        with LogContext.scoped(request_id="level1"):
            assert LogContext.get("request_id") == "level1"
            with LogContext.scoped(request_id="level2"):
                assert LogContext.get("request_id") == "level2"
            assert LogContext.get("request_id") == "level1"
        assert LogContext.get("request_id") == "level0"


class TestLogContextRegisterField:
    """LogContext.register_field() 测试。"""

    def setup_method(self) -> None:
        LogContext.unbind()
        # 清理可能注册的自定义字段
        if "custom_trace" in LogContext._vars:
            del LogContext._vars["custom_trace"]

    def teardown_method(self) -> None:
        LogContext.unbind()
        if "custom_trace" in LogContext._vars:
            del LogContext._vars["custom_trace"]

    def test_register_custom_field(self) -> None:
        """注册自定义字段后可以使用。"""
        LogContext.register_field("custom_trace")
        LogContext.bind(custom_trace="trace-001")
        assert LogContext.get("custom_trace") == "trace-001"

    def test_register_duplicate_no_effect(self) -> None:
        """重复注册同一字段无副作用。"""
        LogContext.register_field("custom_trace")
        LogContext.register_field("custom_trace")
        LogContext.bind(custom_trace="val")
        assert LogContext.get("custom_trace") == "val"

    def test_unregister_field_returns_default(self) -> None:
        """未注册的字段 get 返回默认值。"""
        assert LogContext.get("nonexistent") == "-"


# ═══════════════════════════════════════════════════════════════════
# StructuredFormatter 测试
# ═══════════════════════════════════════════════════════════════════


class TestStructuredFormatter:
    """StructuredFormatter 测试。"""

    def setup_method(self) -> None:
        LogContext.unbind()

    def teardown_method(self) -> None:
        LogContext.unbind()

    def _make_record(self, msg: str = "hello", **extra: Any) -> logging.LogRecord:
        """创建测试用 LogRecord。"""
        record = logging.LogRecord(
            name="test.module",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg=msg,
            args=None,
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_format_contains_timestamp(self) -> None:
        """输出包含时间戳。"""
        fmt = StructuredFormatter()
        result = fmt.format(self._make_record())
        # 时间戳格式: YYYY-MM-DD HH:MM:SS
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", result)

    def test_format_contains_level(self) -> None:
        """输出包含日志级别。"""
        fmt = StructuredFormatter()
        result = fmt.format(self._make_record())
        assert "INFO" in result

    def test_format_contains_module_name(self) -> None:
        """输出包含模块名。"""
        fmt = StructuredFormatter()
        result = fmt.format(self._make_record())
        assert "test.module" in result

    def test_format_contains_message(self) -> None:
        """输出包含消息内容。"""
        fmt = StructuredFormatter()
        result = fmt.format(self._make_record(msg="管道启动"))
        assert "管道启动" in result

    def test_format_injects_context(self) -> None:
        """输出注入上下文追踪字段。"""
        LogContext.bind(request_id="abc123", task_id="t-001")
        fmt = StructuredFormatter()
        result = fmt.format(self._make_record())
        assert "rid=abc123" in result
        assert "tid=t-001" in result

    def test_format_context_default_is_dash(self) -> None:
        """无上下文时输出 '-'。"""
        fmt = StructuredFormatter()
        result = fmt.format(self._make_record())
        # context 部分
        assert " | - | " in result or " | -" in result

    def test_format_with_extra_fields(self) -> None:
        """extra 字段追加到消息末尾。"""
        fmt = StructuredFormatter()
        result = fmt.format(self._make_record(duration_ms=150, tool_name="bash"))
        assert "duration_ms=150" in result
        assert "tool_name=bash" in result

    def test_format_custom_format_string(self) -> None:
        """自定义格式字符串。"""
        fmt = StructuredFormatter(fmt="%(levelname)s | %(message)s")
        result = fmt.format(self._make_record(msg="test msg"))
        assert result.startswith("INFO | test msg")

    def test_format_custom_date_format(self) -> None:
        """自定义日期格式。"""
        fmt = StructuredFormatter(datefmt="%H:%M:%S")
        result = fmt.format(self._make_record())
        assert re.search(r"\d{2}:\d{2}:\d{2}", result)


# ═══════════════════════════════════════════════════════════════════
# JsonFormatter 测试
# ═══════════════════════════════════════════════════════════════════


class TestJsonFormatter:
    """JsonFormatter 测试。"""

    def setup_method(self) -> None:
        LogContext.unbind()

    def teardown_method(self) -> None:
        LogContext.unbind()

    def _make_record(self, msg: str = "hello", **extra: Any) -> logging.LogRecord:
        """创建测试用 LogRecord。"""
        record = logging.LogRecord(
            name="src.pipeline.engine",
            level=logging.INFO,
            pathname="engine.py",
            lineno=42,
            msg=msg,
            args=None,
            exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_output_is_valid_json(self) -> None:
        """输出是合法 JSON。"""
        fmt = JsonFormatter()
        result = fmt.format(self._make_record())
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_json_contains_required_fields(self) -> None:
        """JSON 包含所有必要字段。"""
        fmt = JsonFormatter()
        result = fmt.format(self._make_record(msg="管道启动"))
        parsed = json.loads(result)
        assert "timestamp" in parsed
        assert "level" in parsed
        assert "logger" in parsed
        assert "message" in parsed
        assert "module" in parsed
        assert "function" in parsed
        assert "line" in parsed

    def test_json_field_values(self) -> None:
        """JSON 字段值正确。"""
        fmt = JsonFormatter()
        result = fmt.format(self._make_record(msg="test message"))
        parsed = json.loads(result)
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "src.pipeline.engine"
        assert parsed["message"] == "test message"
        assert parsed["line"] == 42

    def test_json_timestamp_format(self) -> None:
        """时间戳格式为 ISO 8601。"""
        fmt = JsonFormatter()
        result = fmt.format(self._make_record())
        parsed = json.loads(result)
        ts = parsed["timestamp"]
        assert ts.endswith("Z")
        assert "T" in ts

    def test_json_context_fields(self) -> None:
        """JSON 注入上下文字段。"""
        LogContext.bind(request_id="abc", task_id="t-1")
        fmt = JsonFormatter()
        result = fmt.format(self._make_record())
        parsed = json.loads(result)
        assert parsed["request_id"] == "abc"
        assert parsed["task_id"] == "t-1"

    def test_json_context_default_dash(self) -> None:
        """无上下文时字段为 '-'。"""
        fmt = JsonFormatter()
        result = fmt.format(self._make_record())
        parsed = json.loads(result)
        assert parsed["request_id"] == "-"
        assert parsed["task_id"] == "-"

    def test_json_extra_fields(self) -> None:
        """extra 字段包含在 JSON 中。"""
        fmt = JsonFormatter()
        result = fmt.format(self._make_record(duration_ms=200))
        parsed = json.loads(result)
        assert parsed["duration_ms"] == 200

    def test_json_exception_info(self) -> None:
        """异常信息包含在 JSON 中。"""
        fmt = JsonFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="error occurred",
            args=None,
            exc_info=exc_info,
        )
        result = fmt.format(record)
        parsed = json.loads(result)
        assert "exception" in parsed
        assert parsed["exception"]["type"] == "ValueError"
        assert parsed["exception"]["message"] == "test error"
        assert isinstance(parsed["exception"]["traceback"], list)

    def test_json_ensure_ascii_false_by_default(self) -> None:
        """默认不转义中文。"""
        fmt = JsonFormatter()
        result = fmt.format(self._make_record(msg="中文消息"))
        assert "中文消息" in result

    def test_json_ensure_ascii_true(self) -> None:
        """ensure_ascii=True 时转义非 ASCII 字符。"""
        fmt = JsonFormatter(ensure_ascii=True)
        result = fmt.format(self._make_record(msg="中文消息"))
        assert "\\u" in result

    def test_json_custom_context_fields(self) -> None:
        """自定义上下文字段。"""
        fmt = JsonFormatter(context_fields=("request_id",))
        result = fmt.format(self._make_record())
        parsed = json.loads(result)
        assert "request_id" in parsed
        # task_id 和 session_id 不在自定义列表中，不应被添加
        # （但字段不会出现，因为 context_fields 只包含 request_id）

    def test_json_non_serializable_extra_converted_to_string(self) -> None:
        """不可序列化的 extra 值转为字符串。"""
        fmt = JsonFormatter()
        result = fmt.format(self._make_record(data={"complex": object()}))
        parsed = json.loads(result)
        assert "data" in parsed
        assert isinstance(parsed["data"], str)


# ═══════════════════════════════════════════════════════════════════
# setup_logging / get_logger 测试
# ═══════════════════════════════════════════════════════════════════


class TestSetupLogging:
    """setup_logging 全局初始化测试。"""

    def setup_method(self) -> None:
        """重置全局状态。"""
        import src.core.logging as logging_mod

        logging_mod._initialized = False
        root = logging.getLogger()
        root.handlers.clear()
        LogContext.unbind()

    def teardown_method(self) -> None:
        """清理全局状态。"""
        import src.core.logging as logging_mod

        logging_mod._initialized = False
        root = logging.getLogger()
        root.handlers.clear()
        LogContext.unbind()

    def test_setup_with_default_config(self) -> None:
        """默认配置添加 console handler。"""
        config = LoggingConfig(level=logging.DEBUG, output="console")
        setup_logging(config, reset=True)
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)

    def test_setup_json_formatter(self) -> None:
        """json_output=True 时使用 JsonFormatter。"""
        config = LoggingConfig(level=logging.INFO, json_output=True, output="console")
        setup_logging(config, reset=True)
        root = logging.getLogger()
        has_json = any(
            isinstance(h.formatter, JsonFormatter) for h in root.handlers
        )
        assert has_json

    def test_setup_structured_formatter(self) -> None:
        """json_output=False 时使用 StructuredFormatter。"""
        config = LoggingConfig(level=logging.INFO, json_output=False, output="console")
        setup_logging(config, reset=True)
        root = logging.getLogger()
        has_structured = any(
            isinstance(h.formatter, StructuredFormatter) for h in root.handlers
        )
        assert has_structured

    def test_setup_idempotent(self) -> None:
        """重复调用不重复添加 handler（除非 reset=True）。"""
        config = LoggingConfig(level=logging.INFO, output="console")
        setup_logging(config, reset=True)
        count_after_first = len(logging.getLogger().handlers)
        setup_logging(config, reset=False)
        count_after_second = len(logging.getLogger().handlers)
        assert count_after_second == count_after_first

    def test_setup_reset_clears_handlers(self) -> None:
        """reset=True 时清除已有 handler。"""
        root = logging.getLogger()
        root.addHandler(logging.StreamHandler())
        initial_count = len(root.handlers)

        config = LoggingConfig(level=logging.INFO, output="console")
        setup_logging(config, reset=True)
        # reset 后应有恰好 1 个 handler（刚添加的 console）
        assert len(root.handlers) == 1

    def test_setup_quiet_third_party(self) -> None:
        """第三方库日志级别被降级。"""
        config = LoggingConfig(
            level=logging.DEBUG,
            output="console",
            third_party_level=logging.ERROR,
        )
        setup_logging(config, reset=True)
        assert logging.getLogger("urllib3").level == logging.ERROR
        assert logging.getLogger("httpx").level == logging.ERROR


class TestGetLogger:
    """get_logger 测试。"""

    def test_returns_logger_instance(self) -> None:
        """返回标准 Logger 实例。"""
        logger = get_logger("test.module")
        assert isinstance(logger, logging.Logger)

    def test_returns_named_logger(self) -> None:
        """返回指定名称的 logger。"""
        logger = get_logger("myapp.core")
        assert logger.name == "myapp.core"

    def test_returns_root_without_name(self) -> None:
        """无参数时返回 root logger。"""
        logger = get_logger()
        assert logger.name == "root"


# ═══════════════════════════════════════════════════════════════════
# ContextFilter 测试
# ═══════════════════════════════════════════════════════════════════


class TestContextFilter:
    """ContextFilter 测试。"""

    def setup_method(self) -> None:
        """每个测试前清除上下文。"""
        LogContext.unbind()

    def teardown_method(self) -> None:
        """每个测试后清除上下文。"""
        LogContext.unbind()

    def _make_record(self, msg: str = "hello") -> logging.LogRecord:
        """创建测试用 LogRecord。"""
        return logging.LogRecord(
            name="test.module",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=None,
            exc_info=None,
        )

    def test_filter_returns_true(self) -> None:
        """filter 始终返回 True，不拦截日志。"""
        record = self._make_record()
        assert ContextFilter().filter(record) is True

    def test_filter_injects_request_id(self) -> None:
        """filter 注入 request_id 到 record。"""
        LogContext.bind(request_id="req-001")
        record = self._make_record()
        ContextFilter().filter(record)
        assert hasattr(record, "request_id")
        assert record.request_id == "req-001"

    def test_filter_injects_all_context_fields(self) -> None:
        """filter 注入全部 7 个追踪字段。"""
        LogContext.bind(
            request_id="r1", task_id="t1", session_id="s1",
            trace_id="tr1", pipeline_id="p1", thread_id="th1",
            agent_name="agent",
        )
        record = self._make_record()
        ContextFilter().filter(record)
        assert record.request_id == "r1"
        assert record.task_id == "t1"
        assert record.session_id == "s1"
        assert record.trace_id == "tr1"
        assert record.pipeline_id == "p1"
        assert record.thread_id == "th1"
        assert record.agent_name == "agent"

    def test_filter_injects_default_dash_when_unset(self) -> None:
        """未设置的字段注入默认值 '-'。"""
        record = self._make_record()
        ContextFilter().filter(record)
        assert record.request_id == "-"
        assert record.task_id == "-"

    def test_filter_does_not_overwrite_existing_attr(self) -> None:
        """filter 不覆盖调用方通过 extra= 显式设置的值。"""
        record = self._make_record()
        record.request_id = "explicit-value"
        LogContext.bind(request_id="context-value")
        ContextFilter().filter(record)
        assert record.request_id == "explicit-value"

    def test_filter_with_standard_formatter(self) -> None:
        """ContextFilter + 标准 Formatter 可引用 %(request_id)s 占位符。"""
        LogContext.bind(request_id="trace-abc")
        handler = logging.StreamHandler()
        handler.addFilter(ContextFilter())
        handler.setFormatter(logging.Formatter(
            "[%(levelname)s] rid=%(request_id)s tid=%(task_id)s %(message)s"
        ))

        logger = logging.getLogger("test_context_filter_integration")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        import io
        stream = io.StringIO()
        handler.stream = stream
        logger.info("test message")

        output = stream.getvalue()
        assert "rid=trace-abc" in output
        assert "tid=-" in output
        assert "test message" in output


class TestSetupLoggingWithContextFilter:
    """setup_logging 集成 ContextFilter 测试。"""

    def setup_method(self) -> None:
        """重置全局状态。"""
        import src.core.logging as logging_mod

        logging_mod._initialized = False
        root = logging.getLogger()
        root.handlers.clear()
        LogContext.unbind()

    def teardown_method(self) -> None:
        """清理全局状态。"""
        import src.core.logging as logging_mod

        logging_mod._initialized = False
        root = logging.getLogger()
        root.handlers.clear()
        LogContext.unbind()

    def test_console_handler_has_context_filter(self) -> None:
        """setup_logging 后 console handler 包含 ContextFilter。"""
        config = LoggingConfig(level=logging.INFO, output="console")
        setup_logging(config, reset=True)
        root = logging.getLogger()
        has_filter = any(
            isinstance(f, ContextFilter) for h in root.handlers for f in h.filters
        )
        assert has_filter

    def test_file_handler_has_context_filter(self) -> None:
        """setup_logging 后 file handler 包含 ContextFilter。"""
        import tempfile

        with tempfile.TemporaryDirectory():
            config = LoggingConfig(
                level=logging.INFO, output="file",
                file_path="logs/test_context_filter.log",
            )
            setup_logging(config, reset=True)
            root = logging.getLogger()
            has_filter = any(
                isinstance(f, ContextFilter) for h in root.handlers for f in h.filters
            )
            assert has_filter
