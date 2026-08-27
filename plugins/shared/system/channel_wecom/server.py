#!/usr/bin/env python3
"""WeCom Channel MCP 服务端——纯接口适配层。

老代码从 0.1 src/channels/wecom/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §5.2]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# 渠道共享包 channel_common（input_adapter/output_adapter/base_combo_adapter 单一事实源）。
# 路径纪律：这三个模块名是通用名（各渠道目录历史上各有一份、现由
# scripts/check_channel_copy_guard.py 禁止复制回潮），同进程 sys.path 按目录顺序解析，
# 本目录 insert(0) 会反过来遮蔽共享包——所以共享包只允许 append 追加。
# 完整背景见 docs/working/渠道合流C1C2与CLI插件化方案_20260819.md §三。
if (_cc := os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "channel_common"))) not in sys.path and os.path.isdir(_cc):
    sys.path.append(_cc)

from adapter import WeComAdapter

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("channel_wecom")

_adapter: WeComAdapter | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize WeCom channel adapter on load."""
    global _adapter
    config = plugin.get_config()
    _adapter = WeComAdapter(
        corp_id=config.get("corp_id", ""),
        agent_id=int(config.get("agent_id", 0)),
        secret=config.get("secret", ""),
        token=config.get("token", ""),
        encoding_aes_key=config.get("encoding_aes_key", ""),
    )
    logger.info("WeCom channel adapter initialized")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup WeCom channel adapter on unload."""
    global _adapter
    if _adapter:
        await _adapter.stop()
        _adapter = None


@plugin.tool(
    name="wecom.send_message",
    schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "WeCom user UserID"},
            "content": {"type": "string", "description": "Message content text"},
            "msg_type": {
                "type": "string",
                "enum": ["text", "markdown"],
                "default": "text",
            },
        },
        "required": ["user_id", "content"],
    },
    description="Send a text message to a WeCom user via HTTP API",
)
async def wecom_send_message(
    user_id: str, content: str, msg_type: str = "text"
) -> dict[str, Any]:
    """Send a message to a WeCom user.

    Args:
        user_id: WeCom user UserID
        content: Message content text
        msg_type: Message type ("text" or "markdown")

    Returns:
        WeCom API response dictionary
    """
    if _adapter is None or _adapter.stream_client is None:
        return {"error": "WeCom adapter not initialized"}
    if _adapter.stream_client._session is None:
        return {"error": "WeCom stream client not connected"}
    result = await _adapter.stream_client.send_message(user_id, content, msg_type)
    return result


@plugin.tool(
    name="wecom.handle_callback",
    schema={
        "type": "object",
        "properties": {
            "timestamp": {"type": "string", "description": "Callback timestamp"},
            "nonce": {"type": "string", "description": "Callback nonce"},
            "msg_signature": {"type": "string", "description": "Callback message signature"},
            "body": {"type": "string", "description": "Callback request body (encrypted XML)"},
        },
        "required": ["timestamp", "nonce", "msg_signature", "body"],
    },
    description="Handle WeCom callback request (URL verification or message decryption)",
)
async def wecom_handle_callback(
    timestamp: str, nonce: str, msg_signature: str, body: str
) -> dict[str, Any]:
    """Handle a WeCom callback request.

    Processes both URL verification (GET) and message reception (POST).

    Args:
        timestamp: Callback request timestamp
        nonce: Callback request nonce
        msg_signature: Callback request signature
        body: Callback request body (encrypted XML)

    Returns:
        Decrypted message content or empty string on failure
    """
    if _adapter is None:
        return {"error": "WeCom adapter not initialized"}
    result = await _adapter.handle_callback(timestamp, nonce, msg_signature, body)
    return {"decrypted": result}


@plugin.tool(
    name="wecom.get_status",
    schema={"type": "object", "properties": {}},
    description="Get WeCom channel adapter connection status",
)
async def wecom_get_status() -> dict[str, Any]:
    """Get the connection status of the WeCom channel adapter.

    Returns:
        Status dictionary with type, connected, and healthy fields
    """
    if _adapter is None:
        return {"type": "wecom", "connected": False, "healthy": False}
    return _adapter.get_status()


if __name__ == "__main__":
    plugin.run()
