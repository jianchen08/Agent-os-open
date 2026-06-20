"""结构化日志 + 链路追踪补充测试

补充覆盖：
- ContextFilter 注入上下文字段
- LogContext.register_field 边界
- StructuredFormatter 自定义格式
- JsonFormatter 完整上下文字段链
- BridgeCore LogContext 集成
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from src.core.logging import (
    ContextFilter,
    JsonFormatter,
    LogContext,
    LoggingConfig,
    StructuredFormatter,
    get_logger,
    setup_logging,
)


# ============================================================
# ContextFilter 测试
# ============================================================

class TestContextFilter:
    """ContextFilter 注入跟踪字段。"""

    def setup_method(self):
        LogContext.unbind()

    def teardown_method(self):
        LogContext.unbind()

    def test_injects_all_fields_to_record(self):
        """绑定后 filter 注入上下文字段到 record。"""
        LogContext.bind(request_id="r1", task_id="t1", trace_id="trace-x")
        f = ContextFilter()
        rec = logging.LogRecord("test", logging.INFO, "", 1, "msg", None, None)
        assert f.filter(rec) is True
        assert rec.request_id == "r1"
        assert rec.task_id == "t1"
        assert rec.trace_id == "trace-x"

    def test_does_not_overwrite_existing_attrs(self):
        """不覆盖 record 已有属性。"""
        LogContext.bind(request_id="from-context")
        f = ContextFilter()
        rec = logging.LogRecord("test", logging.INFO, "", 1, "msg", None, None)
        rec.request_id = "explicit"
        f.filter(rec)
        assert rec.request_id == "explicit"

    def test_default_values_when_unbound(self):
        """未绑定时注入 '-'。"""
        f = ContextFilter()
        rec = logging.LogRecord("test", logging.INFO, "", 1, "msg", None, None)
        f.filter(rec)
        assert rec.request_id == "-"


# ============================================================
# LogContext 补充测试
# ============================================================

class TestLogContextEdgeCases:
    """LogContext 边界场景。"""

    def setup_method(self):
        LogContext.unbind()

    def teardown_method(self):
        LogContext.unbind()

    def test_bind_overwrites_previous(self):
        """bind 覆盖已有值。"""
        LogContext.bind(request_id="first")
        LogContext.bind(request_id="second")
        assert LogContext.get("request_id") == "second"

    def test_snapshot_all_fields(self):
        """snapshot 返回所有标准字段。"""
        LogContext.bind(trace_id="t-001", agent_name="灵汐")
        snap = LogContext.snapshot()
        for key in ("request_id", "task_id", "session_id", "trace_id",
                     "pipeline_id", "thread_id", "agent_name"):
            assert key in snap
        assert snap["trace_id"] == "t-001"

    def test_format_context_partial(self):
        """仅设置部分字段。"""
        LogContext.bind(pipeline_id="pipe-abc")
        result = LogContext.format_context()
        assert "pipeline_id=pipe-abc" in result
        assert "rid=" not in result

    def test_register_duplicate_field(self):
        """重复注册同名字段不报错。"""
        LogContext.register_field("custom_x")
        LogContext.register_field("custom_x")

    def test_bind_ignores_unknown_keys(self):
        """绑定未知键不抛异常。"""
        LogContext.bind(unknown_key="val")
        assert LogContext.get("unknown_key") == "-"

    def test_scoped_nested(self):
        """嵌套 scoped 逐层恢复。"""
        LogContext.bind(request_id="L0")
        with LogContext.scoped(request_id="L1"):
            assert LogContext.get("request_id") == "L1"
            with LogContext.scoped(request_id="L2"):
                assert LogContext.get("request_id") == "L2"
            assert LogContext.get("request_id") == "L1"
        assert LogContext.get("request_id") == "L0"


# ============================================================
# StructuredFormatter 补充测试
# ============================================================

class TestStructuredFormatterEdgeCases:
    """StructuredFormatter 边界。"""

    def test_custom_format_string(self):
        """自定义格式字符串。"""
        fmt = StructuredFormatter(
            fmt="%(levelname)s | %(message)s", datefmt="%H:%M"
        )
        rec = logging.LogRecord("test", logging.INFO, "", 1, "hello", None, None)
        out = fmt.format(rec)
        assert "INFO" in out
        assert "hello" in out

    def test_multiple_extras(self):
        """多个 extra 字段追加。"""
        fmt = StructuredFormatter()
        rec = logging.LogRecord("x", logging.INFO, "", 1, "base", None, None)
        rec.a = 1
        rec.b = "val"
        out = fmt.format(rec)
        assert "a=1" in out
        assert "b=val" in out

    def test_no_extras(self):
        """无 extra 字段。"""
        fmt = StructuredFormatter()
        rec = logging.LogRecord("x", logging.INFO, "", 1, "plain", None, None)
        out = fmt.format(rec)
        assert "plain" in out

    def test_private_attrs_excluded(self):
        """_ 开头的属性被排除。"""
        fmt = StructuredFormatter()
        rec = logging.LogRecord("x", logging.INFO, "", 1, "msg", None, None)
        rec._private = "secret"
        rec.public = "visible"
        out = fmt.format(rec)
        assert "public=visible" in out
        assert "_private" not in out


# ============================================================
# JsonFormatter 补充测试
# ============================================================

class TestJsonFormatterEdgeCases:
    """JsonFormatter 边界。"""

    def test_timestamp_format(self):
        """时间戳为 ISO 8601 格式。"""
        fmt = JsonFormatter()
        rec = logging.LogRecord("t", logging.INFO, "", 1, "msg", None, None)
        rec.created = 1718841600.0
        out = fmt.format(rec)
        data = json.loads(out)
        assert data["timestamp"].endswith("Z")
        assert "T" in data["timestamp"]

    def test_no_exception_when_no_exc_info(self):
        """无异常时不生成 exception 字段。"""
        fmt = JsonFormatter()
        rec = logging.LogRecord("t", logging.INFO, "", 1, "msg", None, None)
        data = json.loads(fmt.format(rec))
        assert "exception" not in data

    def test_empty_message(self):
        """空消息正常。"""
        fmt = JsonFormatter()
        rec = logging.LogRecord("t", logging.INFO, "", 1, "", None, None)
        data = json.loads(fmt.format(rec))
        assert data["message"] == ""

    def test_custom_context_fields(self):
        """自定义上下文字段列表。"""
        LogContext.bind(request_id="r99", pipeline_id="p99", agent_name="bot")
        try:
            fmt = JsonFormatter(context_fields=("request_id", "pipeline_id"))
            rec = logging.LogRecord("t", logging.INFO, "", 1, "msg", None, None)
            data = json.loads(fmt.format(rec))
            assert data.get("request_id") == "r99"
            assert data.get("pipeline_id") == "p99"
            assert "agent_name" not in data
        finally:
            LogContext.unbind()


# ============================================================
# LoggingConfig 补充测试
# ============================================================

class TestLoggingConfigEdgeCases:
    """LoggingConfig 边界场景。"""

    def test_from_env_file_path(self, monkeypatch):
        """from_env 读取 LOG_FILE 环境变量。"""
        monkeypatch.setenv("LOG_FILE", "/tmp/test.log")
        config = LoggingConfig.from_env()
        assert config.file_path == "/tmp/test.log"

    def test_from_dict_complete(self):
        """完整 from_dict。"""
        config = LoggingConfig.from_dict({
            "level": "WARNING",
            "output": "both",
            "json_output": True,
            "file_path": "/var/log/app.log",
            "file_max_bytes": 1000000,
            "file_backup_count": 5,
            "third_party_level": "ERROR",
            "context_fields": ["request_id", "task_id", "trace_id"],
        })
        assert config.level == logging.WARNING
        assert config.output == "both"
        assert config.json_output is True
        assert config.file_path == "/var/log/app.log"
        assert config.file_max_bytes == 1000000
        assert config.file_backup_count == 5
        assert config.third_party_level == logging.ERROR
        assert list(config.context_fields) == ["request_id", "task_id", "trace_id"]

    def test_context_fields_default(self):
        """默认 context_fields 包含所有追踪字段。"""
        config = LoggingConfig()
        fields = set(config.context_fields)
        assert "request_id" in fields
        assert "task_id" in fields
        assert "session_id" in fields


# ============================================================
# BridgeCore LogContext 集成测试
# ============================================================

class TestBridgeCoreLogContextIntegration:
    """BridgeCore 初始化时绑定 LogContext。"""

    def test_bridge_init_binds_log_context(self):
        """BridgeCore.__init__ 绑定 pipeline_id 到 LogContext。"""
        from pipeline.bridge_core import BridgeCore

        class MockSink:
            async def send_event(self, e): return True
            @property
            def sink_id(self): return "test"

        LogContext.unbind()
        try:
            b = BridgeCore()
            b._init_core_state("pipe-log-test", MockSink())
            assert LogContext.get("pipeline_id") == "pipe-log-test"
        finally:
            LogContext.unbind()
