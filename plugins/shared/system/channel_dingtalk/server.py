#!/usr/bin/env python3
"""DingTalk Channel MCP 服务端——纯接口适配层。

老代码从 0.1 src/channels/dingtalk/ 原封不动复制到本目录（平铺），
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

from adapter import DingTalkAdapter

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("channel_dingtalk")

_adapter: DingTalkAdapter | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize DingTalk channel adapter on load."""
    global _adapter
    config = plugin.get_config()
    client_id = config.get("client_id", "")
    client_secret = config.get("client_secret", "")
    _adapter = DingTalkAdapter(
        client_id=client_id,
        client_secret=client_secret,
    )
    logger.info("DingTalk channel adapter initialized")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup DingTalk channel adapter on unload."""
    global _adapter
    if _adapter:
        await _adapter.stop()
        _adapter = None


@plugin.tool(
    name="dingtalk.send_message",
    schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "DingTalk user staff_id"},
            "content": {"type": "string", "description": "Message content text"},
            "msg_type": {
                "type": "string",
                "enum": ["text", "markdown"],
                "default": "text",
            },
        },
        "required": ["user_id", "content"],
    },
    description="Send a text message to a DingTalk user via Stream API",
)
async def dingtalk_send_message(
    user_id: str, content: str, msg_type: str = "text"
) -> dict[str, Any]:
    """Send a message to a DingTalk user.

    Args:
        user_id: DingTalk user staff_id
        content: Message content text
        msg_type: Message type ("text" or "markdown")

    Returns:
        DingTalk API response dictionary
    """
    if _adapter is None or _adapter.stream_client is None:
        return {"error": "DingTalk adapter not initialized"}
    if _adapter.stream_client._session is None:
        return {"error": "DingTalk stream client not connected"}
    result = await _adapter.stream_client.send_message(user_id, content, msg_type)
    return result


@plugin.tool(
    name="dingtalk.get_status",
    schema={"type": "object", "properties": {}},
    description="Get DingTalk channel adapter connection status",
)
async def dingtalk_get_status() -> dict[str, Any]:
    """Get the connection status of the DingTalk channel adapter.

    Returns:
        Status dictionary with type, connected, and healthy fields
    """
    if _adapter is None:
        return {"type": "dingtalk", "connected": False, "healthy": False}
    return _adapter.get_status()


if __name__ == "__main__":
    plugin.run()
