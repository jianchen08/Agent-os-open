"""统一日志系统 — 公共入口。

提供两个核心函数：

- ``setup_logging()`` — 初始化全局日志配置（应用启动时调用一次）
- ``get_logger()`` — 获取 logger（与 ``logging.getLogger()`` 完全兼容）

现有代码中的 ``logging.getLogger(__name__)`` 无需修改即可自动受益于统一配置。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from src.core.logging.config import LoggingConfig
from src.core.logging.context import LogContext
from src.core.logging.filters import ContextFilter
from src.core.logging.formatters import JsonFormatter, StructuredFormatter

_initialized: bool = False


def setup_logging(
    config: LoggingConfig | None = None,
    *,
    reset: bool = False,
) -> None:
    """初始化全局日志配置。

    调用一次即可。重复调用默认跳过（除非 ``reset=True``）。

    Args:
        config: 日志配置，为 None 则从环境变量读取。
        reset: 是否强制重新初始化。
    """
    global _initialized  # noqa: PLW0603
    if _initialized and not reset:
        return

    config = config or LoggingConfig.from_env()
    root = logging.getLogger()
    root.setLevel(config.level)

    # 清除已有 handler（避免重复输出）
    if reset:
        root.handlers.clear()

    formatter: logging.Formatter = (
        JsonFormatter(context_fields=config.context_fields)
        if config.json_output
        else StructuredFormatter(
            fmt=config.format_string,
            datefmt=config.date_format,
            context_fields=config.context_fields,
        )
    )

    # ── Handler ────────────────────────────────────────
    if config.output in ("console", "both"):
        _add_console_handler(root, formatter, config.level)

    if config.output in ("file", "both"):
        _add_file_handler(root, formatter, config.level, config)

    # ── 第三方库降级 ──────────────────────────────────
    _quiet_third_party(config.third_party_level)

    _initialized = True


def get_logger(name: str | None = None) -> logging.Logger:
    """获取 logger 实例。

    与 ``logging.getLogger()`` 完全兼容，现有代码无需修改。

    Args:
        name: logger 名称，通常传 ``__name__``。为 None 返回 root logger。

    Returns:
        标准 ``logging.Logger`` 实例。
    """
    return logging.getLogger(name)


# ── 内部辅助 ──────────────────────────────────────────────


def _add_console_handler(
    root: logging.Logger,
    formatter: logging.Formatter,
    level: int,
) -> None:
    """添加 stdout handler。"""
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.setLevel(level)
    handler.addFilter(ContextFilter())
    root.addHandler(handler)


def _add_file_handler(
    root: logging.Logger,
    formatter: logging.Formatter,
    level: int,
    config: LoggingConfig,
) -> None:
    """添加轮转文件 handler。"""
    log_path = Path(config.file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=config.file_max_bytes,
        backupCount=config.file_backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    handler.setLevel(level)
    handler.addFilter(ContextFilter())
    root.addHandler(handler)


def _quiet_third_party(level: int) -> None:
    """将常见第三方库日志级别设为指定值，减少噪音。"""
    noisy_loggers = [
        "urllib3",
        "httpx",
        "httpcore",
        "asyncio",
        "aiohttp.access",
        "liteLLM",
        "litellm",
        "watchfiles.main",
        "python_multipart.multipart",
    ]
    for name in noisy_loggers:
        logging.getLogger(name).setLevel(level)


__all__ = [
    "setup_logging",
    "get_logger",
    "LoggingConfig",
    "LogContext",
    "ContextFilter",
    "JsonFormatter",
    "StructuredFormatter",
]
