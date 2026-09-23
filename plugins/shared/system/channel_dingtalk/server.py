#!/usr/bin/env python3
"""DingTalk Channel MCP 服务端——纯接口适配层。

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

from adapter import DingTalkAdapter  # noqa: E402
from inbound_bridge import ChannelInboundBridge  # noqa: E402
from user_space import user_data_dir  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("channel_dingtalk")

_adapter: DingTalkAdapter | None = None
_bridge: ChannelInboundBridge | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize DingTalk channel adapter and inbound bridge on load."""
    global _adapter, _bridge
    config = plugin.get_config()
    # 注入契约按 config_files[].id 命名空间（settings），见 plugin.json。
    settings = config.get("settings", {})
    client_id = settings.get("client_id", "")
    client_secret = settings.get("client_secret", "")
    missing = [k for k, v in (("client_id", client_id), ("client_secret", client_secret)) if not str(v).strip()]
    if missing:
        # fail-fast：空凭据装载出的 adapter 无法通过任何 API 鉴权，
        # 拒绝装载交宿主按失败处理重试，而非带残缺凭据空转。
        raise RuntimeError(f"channel_dingtalk 缺少必填配置: {', '.join(missing)}")
    _adapter = DingTalkAdapter(
        client_id=client_id,
        client_secret=client_secret,
    )
    await _adapter.start()
    _data_root = user_data_dir()
    _bridge = ChannelInboundBridge(
        channel_id="channel_dingtalk",
        input_adapter=_adapter.input_adapter,
        output_adapter=_adapter.output_adapter,
        get_capability=plugin.get_capability,
        store_path=(_data_root / "channels" / "channel_dingtalk.json") if _data_root else None,
    )
    await _bridge.start()
    logger.info("DingTalk channel adapter initialized")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup DingTalk channel adapter and inbound bridge on unload."""
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
