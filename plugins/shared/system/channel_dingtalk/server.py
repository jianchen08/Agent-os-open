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

# 渠道共享包 channel_common（input_adapter/output_adapter/base_combo_adapter 单一事实源）。
# 路径纪律：这三个模块名是通用名（各渠道目录历史上各有一份、现由
# scripts/check_channel_copy_guard.py 禁止复制回潮），同进程 sys.path 按目录顺序解析，
# 本目录 insert(0) 会反过来遮蔽共享包——所以共享包只允许 append 追加。
# 完整背景见 docs/working/渠道合流C1C2与CLI插件化方案_20260819.md §三。
if (_cc := os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "channel_common"))) not in sys.path and os.path.isdir(_cc):
    sys.path.append(_cc)

from adapter import DingTalkAdapter

from agentos_plugin_sdk import AgentOSPlugin

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
