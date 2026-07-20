#!/usr/bin/env python3
"""QQ Channel MCP 服务端——纯接口适配层。

老代码从 0.1 src/channels/qq/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §5.2]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from lingxi_plugin_sdk import AgentOSPlugin

from adapter import QQAdapter

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("channel_qq")

_adapter: QQAdapter | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize QQ channel adapter on load."""
    global _adapter
    config = plugin.get_config()
    _adapter = QQAdapter(
        ws_host=config.get("ws_host", "0.0.0.0"),
        ws_port=int(config.get("ws_port", 8080)),
        http_api_url=config.get("http_api_url", "http://127.0.0.1:5700"),
    )
    logger.info("QQ channel adapter initialized")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup QQ channel adapter on unload."""
    global _adapter
    if _adapter:
        await _adapter.stop()
        _adapter = None


@plugin.tool(
    name="qq.send_message",
    schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "QQ user number"},
            "content": {"type": "string", "description": "Message content text"},
            "message_type": {
                "type": "string",
                "enum": ["private", "group"],
                "default": "private",
            },
            "group_id": {
                "type": "integer",
                "description": "Group ID (required for group messages)",
            },
        },
        "required": ["user_id", "content"],
    },
    description="Send a message to a QQ user or group via OneBot HTTP API",
)
async def qq_send_message(
    user_id: int,
    content: str,
    message_type: str = "private",
    group_id: int | None = None,
) -> dict[str, Any]:
    """Send a message to a QQ user or group.

    Args:
        user_id: QQ user number
        content: Message content text
        message_type: Message type ("private" or "group")
        group_id: Group ID (required for group messages)

    Returns:
        OneBot API response dictionary
    """
    if _adapter is None or _adapter.stream_client is None:
        return {"error": "QQ adapter not initialized"}
    if _adapter.stream_client._session is None:
        return {"error": "OneBot client not connected"}
    result = await _adapter.stream_client.send_message(
        user_id=user_id,
        content=content,
        message_type=message_type,
        group_id=group_id,
    )
    return result


@plugin.tool(
    name="qq.get_status",
    schema={"type": "object", "properties": {}},
    description="Get QQ channel adapter connection status",
)
async def qq_get_status() -> dict[str, Any]:
    """Get the connection status of the QQ channel adapter.

    Returns:
        Status dictionary with type, connected, and healthy fields
    """
    if _adapter is None:
        return {"type": "qq", "connected": False, "healthy": False}
    return _adapter.get_status()


if __name__ == "__main__":
    plugin.run()
