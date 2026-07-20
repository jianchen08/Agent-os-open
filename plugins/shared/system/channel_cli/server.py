#!/usr/bin/env python3
"""CLI Channel MCP 服务端——纯接口适配层。

老代码从 0.1 src/channels/cli/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

注意：CLI 通道的完整运行需要 PipelineEngine/PluginRegistry 等深层依赖，
属于独立进程应用入口，不适合在 Sidecar 模式下完整启动。
本插件仅暴露 CLI 通道的可用能力（状态查询、文本格式化）。

[来源: docs/working/module_migration_plan.md §5.2]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from lingxi_plugin_sdk import AgentOSPlugin

from cli_output_adapter import sanitize_for_terminal

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("channel_cli")


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize CLI channel on load."""
    logger.info("CLI channel adapter initialized (passive mode)")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup CLI channel on unload."""
    pass


@plugin.tool(
    name="cli.get_status",
    schema={"type": "object", "properties": {}},
    description="Get CLI channel adapter status",
)
async def cli_get_status() -> dict[str, Any]:
    """Get the status of the CLI channel adapter.

    CLI adapter is always available (local terminal).

    Returns:
        Status dictionary
    """
    return {
        "type": "cli",
        "connected": True,
        "healthy": True,
        "note": "CLI channel runs as a standalone process, not via MCP sidecar",
    }


@plugin.tool(
    name="cli.sanitize_text",
    schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to sanitize for terminal"},
        },
        "required": ["text"],
    },
    description="Sanitize text for terminal output (strip ANSI codes, control chars)",
)
async def cli_sanitize_text(text: str) -> dict[str, Any]:
    """Sanitize text for safe terminal output.

    Args:
        text: Raw text that may contain ANSI escape codes or control characters

    Returns:
        Dict with sanitized text
    """
    sanitized = sanitize_for_terminal(text)
    return {"sanitized": sanitized}


if __name__ == "__main__":
    plugin.run()
