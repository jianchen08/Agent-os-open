"""结构化日志配置模块。

提供统一的日志初始化和上下文管理能力：
- JSON 格式输出（生产环境）
- 彩色控制台输出（开发环境）
- 日志轮转（RotatingFileHandler）
- trace_id / request_id 上下文注入
- 按 channel_type 过滤的日志 handler

日志文件默认路径: data/logs/
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.logging import LogContext

# ---------------------------------------------------------------------------
# 上下文变量（已统一到 src.core.logging.LogContext，基于 contextvars，async 安全）
# ---------------------------------------------------------------------------


def set_trace_id(trace_id: str) -> None:
    """设置当前上下文的 trace_id（转发到 LogContext）。

    Args:
        trace_id: 追踪 ID
    """
    LogContext.bind(trace_id=trace_id)


def get_trace_id() -> str:
    """获取当前上下文的 trace_id（转发到 LogContext）。

    Returns:
        trace_id 字符串，未设置时返回空字符串
    """
    val = LogContext.get("trace_id")
    return "" if val == "-" else val


def set_request_id(request_id: str) -> None:
    """设置当前上下文的 request_id（转发到 LogContext）。

    Args:
        request_id: 请求 ID
    """
    LogContext.bind(request_id=request_id)


def get_request_id() -> str:
    """获取当前上下文的 request_id（转发到 LogContext）。

    Returns:
        request_id 字符串，未设置时返回空字符串
    """
    val = LogContext.get("request_id")
    return "" if val == "-" else val


class ContextFilter(logging.Filter):
    """日志上下文过滤器（转发到 LogContext）。

    将 trace_id 和 request_id 注入每条日志记录，
    便于在 JSON 输出中进行请求链路追踪。

    Example::

        handler.addFilter(ContextFilter())
        # 日志记录中将包含 trace_id 和 request_id 属性
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """注入上下文信息到日志记录。

        Args:
            record: 日志记录对象

        Returns:
            始终返回 True（不过滤任何日志）
        """
        record.trace_id = get_trace_id()  # type: ignore[attr-defined]
        record.request_id = get_request_id()  # type: ignore[attr-defined]
        return True


# ---------------------------------------------------------------------------
# 格式化器
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """JSON 格式日志格式化器。

    将日志记录序列化为单行 JSON，适合生产环境日志聚合。
    """

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录为 JSON 字符串。

        Args:
            record: 日志记录对象

        Returns:
            单行 JSON 字符串
        """
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }

        # 上下文信息
        trace_id = getattr(record, "trace_id", "")
        if trace_id:
            log_entry["trace_id"] = trace_id
        request_id = getattr(record, "request_id", "")
        if request_id:
            log_entry["request_id"] = request_id

        # 异常信息
        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False, default=str)


class _ConsoleFormatter(logging.Formatter):
    """彩色控制台日志格式化器。

    适合开发环境使用，带颜色区分日志级别。
    """

    # ANSI 颜色码
    _COLORS: dict[int, str] = {
        logging.DEBUG: "\033[36m",  # 青色
        logging.INFO: "\033[32m",  # 绿色
        logging.WARNING: "\033[33m",  # 黄色
        logging.ERROR: "\033[31m",  # 红色
        logging.CRITICAL: "\033[35m",  # 紫色
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录为彩色控制台文本。

        Args:
            record: 日志记录对象

        Returns:
            彩色格式化的日志字符串
        """
        color = self._COLORS.get(record.levelno, "")
        # 时间 + 颜色级别 + logger + 消息
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        trace = getattr(record, "trace_id", "")
        trace_part = f" [{trace[:8]}]" if trace else ""
        msg = f"{timestamp} {color}{record.levelname:8s}{self._RESET} {record.name}: {record.getMessage()}{trace_part}"
        if record.exc_info and record.exc_info[1] is not None:
            msg += "\n" + self.formatException(record.exc_info)
        return msg


# ---------------------------------------------------------------------------
# channel_type 过滤 handler
# ---------------------------------------------------------------------------


class ChannelFilter(logging.Filter):
    """按 channel_type 过滤日志。

    只输出指定 channel_type 的日志记录。
    需要在日志记录中设置 channel_type 属性。

    Example::

        handler = logging.FileHandler("feishu.log")
        handler.addFilter(ChannelFilter("feishu"))
    """

    def __init__(self, channel_type: str) -> None:
        """初始化通道过滤器。

        Args:
            channel_type: 要保留的通道类型
        """
        super().__init__()
        self._channel_type = channel_type

    def filter(self, record: logging.LogRecord) -> bool:
        """仅保留匹配 channel_type 的日志。

        Args:
            record: 日志记录对象

        Returns:
            是否保留该日志
        """
        record_channel = getattr(record, "channel_type", None)
        if record_channel is None:
            # 没有 channel_type 属性的日志全部保留
            return True
        return record_channel == self._channel_type


# ---------------------------------------------------------------------------
# 日志初始化
# ---------------------------------------------------------------------------


def setup_logging(
    log_dir: str = "data/logs",
    log_level: str = "INFO",
    json_format: bool = False,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """初始化日志系统（已转发到统一日志模块 src.core.logging）。

    Args:
        log_dir: 日志文件目录，默认 data/logs/
        log_level: 日志级别，默认 INFO
        json_format: 是否使用 JSON 格式（生产），默认 False（彩色控制台）
        max_bytes: 单个日志文件最大字节数，默认 10MB
        backup_count: 日志轮转备份数，默认 5
    """
    from src.core.logging import LoggingConfig, setup_logging as _unified_setup  # noqa: PLC0415

    config = LoggingConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        json_output=json_format,
        output="both",
        file_path=str(Path(log_dir) / "app.log"),
        file_max_bytes=max_bytes,
        file_backup_count=backup_count,
    )
    _unified_setup(config, reset=True)

    logging.info("Logging initialized: dir=%s, level=%s, json=%s", log_dir, log_level, json_format)
