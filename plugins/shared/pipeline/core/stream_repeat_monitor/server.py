#!/usr/bin/env python3
"""stream_repeat_monitor core pipeline plugin MCP 服务端——纯接口适配层。

老代码从 src/plugins/shared/core/stream_repeat_monitor/plugin.py 原封不动复制到本目录。
StreamRepetitionMonitor 是一个回调包装器（__call__ 语义），本文件将其适配为 MCP tool：
接收 chunks 列表，逐片调用 __call__，汇总是否触发重复检测 stop 信号。
"""
from __future__ import annotations

import logging
import os
import sys

_this_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _this_dir)
# _shared_dir 让老 plugin.py 可解析 from pipeline.plugin import ...（便于未来扩展）
_shared_dir = os.path.join(_this_dir, "..", "..", "..")
sys.path.insert(0, _shared_dir)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from plugin import StreamRepetitionMonitor  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("stream_repeat_monitor_pipeline")

_instance: StreamRepetitionMonitor | None = None


@plugin.on_load
async def _on_load(params: dict) -> None:
    """Initialize stream_repeat_monitor plugin.

    StreamRepetitionMonitor 的 original 参数是可选的回调包装目标。
    作为独立 MCP tool 运行时不需要包装原始回调，传 None 即可。
    """
    global _instance
    config = plugin.get_config()
    _instance = StreamRepetitionMonitor(
        original=None,
        window=config.get("window", 100),
        interval=config.get("interval", 200),
        similarity=config.get("similarity", 0.9),
        trigger=config.get("trigger", 3),
    )


@plugin.on_unload
async def _on_unload(params: dict) -> None:
    """Cleanup stream_repeat_monitor plugin."""
    global _instance
    _instance = None


@plugin.tool(
    name="stream_repeat_monitor.check",
    schema={
        "type": "object",
        "properties": {
            "chunks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Content chunks to check for repetition (sliding window)",
            },
        },
        "required": ["chunks"],
    },
    description="Check content chunks for streaming repetition. Returns stop signal when "
    "similarity exceeds threshold for N consecutive windows.",
)
async def check_repetition(chunks: list[str]) -> dict:
    """Check if content chunks show repetition patterns.

    Simulates streaming detection by feeding each chunk to StreamRepetitionMonitor.__call__.
    Returns immediately when a 'stop' signal is detected.

    Args:
        chunks: List of text content chunks to analyze sequentially.

    Returns:
        Analysis result with 'detected' flag and chunk index where repetition was found.
    """
    stop_index: int | None = None
    for i, chunk_text in enumerate(chunks):
        result = _instance({"type": "text", "content": chunk_text})
        if result == "stop":
            stop_index = i
            break

    return {
        "detected": stop_index is not None,
        "stop_index": stop_index,
        "total_chunks": len(chunks),
    }


if __name__ == "__main__":
    plugin.run()
