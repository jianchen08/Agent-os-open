"""sidecar 进程统一日志初始化。

sidecar(Python 插件进程)的 stdout 被 JSON-RPC 协议占用，日志必须输出到 stderr，
由内核 McpClient 的 stderr reader 消费并转发到 tracing（统一汇聚）。

本模块在 ``plugin.run()`` 启动时调用一次，复用项目统一日志基础设施
(``src.core.logging``)：基于 contextvars 注入 trace_id/pipeline_id 等追踪字段，
JSON/彩色双格式，第三方库降级。若 ``src.core.logging`` 不可 import（如独立运行），
降级到标准 ``logging.basicConfig(stream=sys.stderr)``。

环境变量（与内核 ``src/core/logging/config.py`` 对齐）::

    LOG_LEVEL    — DEBUG / INFO / WARNING / ERROR (默认 INFO)
    LOG_JSON     — 1 / true → JSON 输出（默认 False，彩色控制台）
    LOG_FORMAT   — 自定义格式字符串
"""

from __future__ import annotations

import logging
import os
import sys

_INITIALIZED = False


def setup_sidecar_logging() -> None:
    """初始化 sidecar 进程的统一日志配置（幂等）。

    读取 LOG_* 环境变量，复用 ``src.core.logging.setup_logging``。失败时降级到
    基础 stderr 配置，确保 sidecar 至少有可用的日志输出。
    """
    global _INITIALIZED  # noqa: PLW0603
    if _INITIALIZED:
        return

    # 优先复用项目统一日志基础设施。
    # src.core.logging 的 StreamHandler 默认输出到 stderr，正好契合 sidecar 约束
    #（stdout 被 JSON-RPC 占用）。output 强制 console，不写本地文件——sidecar 日志
    # 由内核 stderr reader 汇聚到统一 sink，避免双写。
    try:
        from src.core.logging import (  # noqa: PLC0415
            LoggingConfig,
            setup_logging,
        )

        config = LoggingConfig.from_env()
        # sidecar 强制 console（stderr），避免本地文件双写
        config = LoggingConfig(
            level=config.level,
            json_output=config.json_output,
            output="console",
            third_party_level=config.third_party_level,
        )
        setup_logging(config, reset=True)
        _INITIALIZED = True
        logging.getLogger(__name__).debug(
            "sidecar logging via src.core.logging (level=%s, json=%s)",
            logging.getLevelName(config.level),
            config.json_output,
        )
        return
    except Exception as exc:  # noqa: BLE001
        # src.core.logging 不可用（独立运行 / PYTHONPATH 未含 src/），降级。
        # 必须仍输出到 stderr，否则日志会污染 JSON-RPC 的 stdout 通道。
        level = _parse_level(os.getenv("LOG_LEVEL", "INFO"))
        logging.basicConfig(
            level=level,
            stream=sys.stderr,
            format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            force=True,  # 覆盖任何已存在的 handler / lastResort
        )
        _INITIALIZED = True
        logging.getLogger(__name__).warning(
            "src.core.logging 不可用，降级到 basicConfig(stream=stderr)：%s", exc
        )


def _parse_level(name: str) -> int:
    """级别名 → logging 常量，未知默认 INFO。"""
    return getattr(logging, name.upper(), logging.INFO)


__all__ = ["setup_sidecar_logging"]
