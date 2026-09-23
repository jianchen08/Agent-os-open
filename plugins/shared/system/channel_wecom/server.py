#!/usr/bin/env python3
"""WeCom Channel MCP 服务端——纯接口适配层。

本目录为通道实现模块（平铺），本文件只做接口适配：
调用同目录实现模块，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §5.2]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# 渠道共享包 channel_common（input_adapter/output_adapter/base_combo_adapter 单一事实源）。
# 路径纪律：这三个模块名是通用名（各渠道目录不得持有同名副本，
# scripts/check_channel_copy_guard.py 执法），同进程 sys.path 按目录顺序解析，
# 本目录 insert(0) 会反过来遮蔽共享包——所以共享包只允许 append 追加。
# 完整背景见 docs/working/渠道合流C1C2与CLI插件化方案_20260819.md §三。
# 定位候选：工厂布局=兄弟目录；用户空间播种副本（<USER_ROOT>/plugins/<id>，共享库
# 目录不随播）回退 AGENTOS_PLUGINS_DIR（内核注入的工厂插件根）。append 不 insert(0)。
for _cc in (
    os.path.join(os.path.dirname(__file__), "..", "channel_common"),
    os.path.join(os.environ.get("AGENTOS_PLUGINS_DIR", ""), "system", "channel_common"),
):
    _cc = os.path.abspath(_cc)
    if os.path.isdir(_cc) and _cc not in sys.path:
        sys.path.append(_cc)

# 入站桥映射持久化定位需要 user_space（plugins/shared 平铺模块）。
from agentos_plugin_sdk.bootstrap import bootstrap_plugin  # noqa: E402

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

from adapter import WeComAdapter  # noqa: E402
from inbound_bridge import ChannelInboundBridge  # noqa: E402
from user_space import user_data_dir  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("channel_wecom")

_adapter: WeComAdapter | None = None
_bridge: ChannelInboundBridge | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize WeCom channel adapter and inbound bridge on load."""
    global _adapter, _bridge
    config = plugin.get_config()
    # 注入契约按 config_files[].id 命名空间（settings），见 plugin.json。
    settings = config.get("settings", {})
    corp_id = settings.get("corp_id", "")
    secret = settings.get("secret", "")
    missing = [k for k, v in (("corp_id", corp_id), ("secret", secret)) if not str(v).strip()]
    if missing:
        # fail-fast：空凭据装载出的 adapter 无法通过任何 API 鉴权，
        # 拒绝装载交宿主按失败处理重试，而非带残缺凭据空转。
        raise RuntimeError(f"channel_wecom 缺少必填配置: {', '.join(missing)}")
    _adapter = WeComAdapter(
        corp_id=corp_id,
        agent_id=int(settings.get("agent_id", 0)),
        secret=secret,
        token=settings.get("token", ""),
        encoding_aes_key=settings.get("encoding_aes_key", ""),
    )
    await _adapter.start()
    _data_root = user_data_dir()
    _bridge = ChannelInboundBridge(
        channel_id="channel_wecom",
        input_adapter=_adapter.input_adapter,
        output_adapter=_adapter.output_adapter,
        get_capability=plugin.get_capability,
        store_path=(_data_root / "channels" / "channel_wecom.json") if _data_root else None,
    )
    await _bridge.start()
    logger.info("WeCom channel adapter initialized")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup WeCom channel adapter and inbound bridge on unload."""
    global _adapter, _bridge
    if _bridge:
        await _bridge.stop()
        _bridge = None
    if _adapter:
        await _adapter.stop()
        _adapter = None


@plugin.on_domain_event
async def _on_domain_event(params: dict[str, Any]) -> None:
    """run 终态域事件 → 入站桥回复回流（completed/failed 之外的事件桥自行忽略）。"""
    if _bridge:
        await _bridge.handle_domain_event(params)


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
