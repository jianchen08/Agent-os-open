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
import os
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 上下文变量（线程安全）
# ---------------------------------------------------------------------------

_context = threading.local()


def set_trace_id(trace_id: str) -> None:
    """设置当前线程的 trace_id。

    Args:
        trace_id: 追踪 ID
    """
    _context.trace_id = trace_id


def get_trace_id() -> str:
    """获取当前线程的 trace_id。

    Returns:
        trace_id 字符串，未设置时返回空字符串
    """
    return getattr(_context, "trace_id", "")


def set_request_id(request_id: str) -> None:
    """设置当前线程的 request_id。

    Args:
        request_id: 请求 ID
    """
    _context.request_id = request_id


def get_request_id() -> str:
    """获取当前线程的 request_id。

    Returns:
        request_id 字符串，未设置时返回空字符串
    """
    return getattr(_context, "request_id", "")


class ContextFilter(logging.Filter):
    """日志上下文过滤器。

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

        return json.dumps(log_entry, ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    """彩色控制台日志格式化器。

    适合开发环境使用，带颜色区分日志级别。
    """

    # ANSI 颜色码
    _COLORS: dict[int, str] = {
        logging.DEBUG: "\033[36m",     # 青色
        logging.INFO: "\033[32m",      # 绿色
        logging.WARNING: "\033[33m",   # 黄色
        logging.ERROR: "\033[31m",     # 红色
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
        msg = (
            f"{timestamp} {color}{record.levelname:8s}{self._RESET} "
            f"{record.name}: {record.getMessage()}{trace_part}"
        )
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
    """初始化日志系统。

    配置根日志器，添加控制台和文件 handler，
    支持开发和生产两种格式模式。

    Args:
        log_dir: 日志文件目录，默认 data/logs/
        log_level: 日志级别，默认 INFO
        json_format: 是否使用 JSON 格式（生产），默认 False（彩色控制台）
        max_bytes: 单个日志文件最大字节数，默认 10MB
        backup_count: 日志轮转备份数，默认 5
    """
    # 确保日志目录存在
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # 获取日志级别
    level = getattr(logging, log_level.upper(), logging.INFO)

    # 配置根日志器
    root = logging.getLogger()
    root.setLevel(level)

    # 清除已有 handler（避免重复）
    root.handlers.clear()

    # 上下文过滤器
    ctx_filter = ContextFilter()

    # 1. 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    if json_format:
        console_handler.setFormatter(_JsonFormatter())
    else:
        console_handler.setFormatter(_ConsoleFormatter())
    console_handler.addFilter(ctx_filter)
    root.addHandler(console_handler)

    # 2. 主日志文件 handler（轮转）
    main_log = log_path / "app.log"
    file_handler = RotatingFileHandler(
        str(main_log),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    if json_format:
        file_handler.setFormatter(_JsonFormatter())
    else:
        file_fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
        file_handler.setFormatter(logging.Formatter(file_fmt))
    file_handler.addFilter(ctx_filter)
    root.addHandler(file_handler)

    # 3. 错误日志单独文件
    error_log = log_path / "error.log"
    error_handler = RotatingFileHandler(
        str(error_log),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    if json_format:
        error_handler.setFormatter(_JsonFormatter())
    else:
        error_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
        )
    error_handler.addFilter(ctx_filter)
    root.addHandler(error_handler)

    logging.info("Logging initialized: dir=%s, level=%s, json=%s", log_dir, log_level, json_format)
