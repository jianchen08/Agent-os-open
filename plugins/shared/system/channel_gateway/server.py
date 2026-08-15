#!/usr/bin/env python3
"""Multi-Channel Gateway MCP 服务端——纯接口适配层。

老代码从 0.1 src/channels/gateway/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §5.2]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from channel_gateway import ChannelGateway
from unified_types import UnifiedResponse

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("channel_gateway")

_gateway: ChannelGateway | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize the multi-channel gateway on load."""
    global _gateway
    _gateway = ChannelGateway()
    logger.info("Multi-channel gateway initialized")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup the multi-channel gateway on unload."""
    global _gateway
    if _gateway:
        await _gateway.stop()
        _gateway = None


@plugin.tool(
    name="gateway.handle_message",
    schema={
        "type": "object",
        "properties": {
            "channel_type": {
                "type": "string",
                "description": "Source channel type (e.g. feishu, dingtalk, wecom, qq)",
            },
            "raw_message": {
                "type": "object",
                "description": "Channel raw message dict",
            },
        },
        "required": ["channel_type", "raw_message"],
    },
    description="Handle incoming message from any registered channel, normalize and route to pipeline",
)
async def gateway_handle_message(
    channel_type: str, raw_message: dict[str, Any]
) -> dict[str, Any]:
    """Handle an incoming message from a channel.

    Normalizes the raw message, resolves the session, builds initial pipeline state,
    and routes to the pipeline callback.

    Args:
        channel_type: Source channel type identifier
        raw_message: Channel-specific raw message dictionary

    Returns:
        Result dict indicating success
    """
    if _gateway is None:
        return {"error": "Gateway not initialized"}
    await _gateway.handle_message(channel_type, raw_message)
    return {"handled": True, "channel": channel_type}


@plugin.tool(
    name="gateway.send_response",
    schema={
        "type": "object",
        "properties": {
            "channel_type": {"type": "string", "description": "Target channel type"},
            "content": {"type": "string", "description": "Response content text"},
            "message_id": {"type": "string", "description": "Associated message ID"},
            "content_type": {
                "type": "string",
                "enum": ["text", "card"],
                "default": "text",
            },
        },
        "required": ["channel_type", "content"],
    },
    description="Send a unified response to a specific channel via its adapter",
)
async def gateway_send_response(
    channel_type: str,
    content: str,
    message_id: str = "",
    content_type: str = "text",
) -> dict[str, Any]:
    """Send a unified response to a channel.

    Args:
        channel_type: Target channel type
        content: Response content text
        message_id: Associated message ID
        content_type: Response content type ("text" or "card")

    Returns:
        Result dict indicating success
    """
    if _gateway is None:
        return {"error": "Gateway not initialized"}
    response = UnifiedResponse(
        message_id=message_id,
        channel_type=channel_type,
        content=content,
        content_type=content_type,
    )
    await _gateway.send_response(response)
    return {"sent": True, "channel": channel_type}


@plugin.tool(
    name="gateway.get_adapters",
    schema={"type": "object", "properties": {}},
    description="List all registered channel adapters",
)
async def gateway_get_adapters() -> dict[str, Any]:
    """List all registered channel adapters.

    Returns:
        Dict with list of registered adapter types
    """
    if _gateway is None:
        return {"error": "Gateway not initialized", "adapters": []}
    return {"adapters": list(_gateway._adapters.keys())}


if __name__ == "__main__":
    plugin.run()
