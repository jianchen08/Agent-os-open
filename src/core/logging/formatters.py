"""日志格式化器。

提供两种 Formatter：
- StructuredFormatter — 人类可读的结构化文本（默认）
- JsonFormatter — JSON 结构化输出（适合 ELK / 日志聚合）
"""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Any

from src.core.logging.context import LogContext


class StructuredFormatter(logging.Formatter):
    """结构化文本格式化器。

    在标准日志格式中注入 ``%(context)s`` 占位符，由 ``LogContext`` 填充追踪信息。

    默认格式::

        2025-01-01 12:00:00 | INFO     | src.pipeline.engine | rid=abc tid=t-001 | 消息内容

    如果 ``record`` 上附带了 ``extra`` 字段（如 ``duration_ms``、``tool_name``），
    会自动追加到消息末尾。
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        context_fields: tuple[str, ...] | None = None,
    ) -> None:
        fmt = fmt or "%(asctime)s | %(levelname)-8s | %(name)s | %(context)s | %(message)s"
        datefmt = datefmt or "%Y-%m-%d %H:%M:%S"
        super().__init__(fmt, datefmt)
        self._context_fields = context_fields or ("request_id", "task_id", "session_id")

    def format(self, record: logging.LogRecord) -> str:
        # 注入上下文字符串
        record.context = LogContext.format_context()

        # 附带 extra 字段
        extra_parts = self._extract_extras(record)
        if extra_parts:
            record.msg = f"{record.msg} | {extra_parts}"

        return super().format(record)

    @staticmethod
    def _extract_extras(record: logging.LogRecord) -> str:
        """提取用户自定义的 extra 字段，拼接为 key=value 字符串。"""
        standard = {
            "name",
            "msg",
            "args",
            "created",
            "relativeCreated",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "pathname",
            "filename",
            "module",
            "thread",
            "threadName",
            "process",
            "processName",
            "levelno",
            "levelname",
            "message",
            "msecs",
            "context",
            "taskName",
        }
        extras: list[str] = []
        for key, val in record.__dict__.items():
            if key.startswith("_") or key in standard:
                continue
            extras.append(f"{key}={val}")
        return " ".join(extras)


class JsonFormatter(logging.Formatter):
    """JSON 结构化格式化器。

    每条日志输出为一行 JSON，包含完整上下文信息，便于日志聚合系统（ELK、Loki）检索。

    输出示例::

        {
          "timestamp": "2025-01-01T12:00:00.123Z",
          "level": "INFO",
          "logger": "src.pipeline.engine",
          "message": "管道启动",
          "request_id": "abc",
          "task_id": "t-001",
          "session_id": "-",
          "module": "engine",
          "function": "run",
          "line": 42
        }
    """

    def __init__(
        self,
        context_fields: tuple[str, ...] | None = None,
        ensure_ascii: bool = False,
    ) -> None:
        super().__init__()
        self._context_fields = context_fields or ("request_id", "task_id", "session_id")
        self._ensure_ascii = ensure_ascii

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": _iso_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # 注入上下文追踪字段
        for field_name in self._context_fields:
            log_entry[field_name] = LogContext.get(field_name)

        # 注入 extra 字段
        standard = _STANDARD_RECORD_KEYS
        for key, val in record.__dict__.items():
            if key.startswith("_") or key in standard:
                continue
            log_entry[key] = _json_safe(val)

        # 异常信息
        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Unknown",
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exception(*record.exc_info),
            }

        return json.dumps(log_entry, ensure_ascii=self._ensure_ascii, default=str)


def _iso_timestamp(created: float) -> str:
    """将 log record 的 created 浮点时间戳转为 ISO 8601 字符串。"""
    dt = datetime.fromtimestamp(created, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _json_safe(value: Any) -> Any:
    """确保值可 JSON 序列化。"""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return str(value)


def _standard_record_keys() -> set[str]:
    """返回 logging.LogRecord 的标准属性名集合（模块级缓存，仅初始化一次）。"""
    record = logging.LogRecord(
        name="",
        level=0,
        pathname="",
        lineno=0,
        msg="",
        args=None,
        exc_info=None,
    )
    return set(record.__dict__.keys()) | {"message", "asctime", "context", "taskName"}


# 模块级常量：标准 LogRecord 属性名集合（避免每条日志创建临时对象）
_STANDARD_RECORD_KEYS: set[str] = _standard_record_keys()


__all__ = ["StructuredFormatter", "JsonFormatter"]
