"""sync_relay 路由只读视图 —— 2c 分布式路由的唯一输入数据源组装（ADR 2c 草案 D8）。

定位（D7 边界）：relay 是路由的**数据源**不是**决策者**——本模块只把设备注册表
组装成发起端编排脑可消费的只读视图（位阶展开 + 在线判定 + 能力面透传），不做
任何路由裁决。物理存储零改动（capabilities_json 自由 JSON 承载节点模型扩展子键：
plugins/degraded/resource/exec_channel，见 ADR 2c 草案 D8）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from relay_core import TIER_RANK, UNKNOWN_TIER_RANK, is_online


def build_routing_view(
    store: Any,
    *,
    now: datetime,
    window_secs: float,
    requested_by: str,
    hub_device_id: str = "",
) -> dict[str, Any]:
    """组装路由表视图：注册表全量 + 位阶序 + 在线结构化 + 能力面透传。

    排序 = (tier_rank, device_id)——位阶权威序（层级权威收窄三用之③路由输入，
    ADR 2a D2）优先，同位阶按 device_id 字典序保确定性（同输入同视图，可对账）。

    address/storage 直连面透传（ADR 2026-10-21-device-card-direct-connect：卡上
    直连/探活数据源，只增字段旧消费者忽略）；is_hub 仅在配置 hub_device_id 且
    命中该设备时出现（中枢卡拉取证据优先的心跳冗余面，未配置零噪音）。
    """
    devices = []
    for d in store.list_devices():
        entry: dict[str, Any] = {
            "device_id": d["device_id"],
            "tier": d["tier"],
            "tier_rank": TIER_RANK.get(d["tier"], UNKNOWN_TIER_RANK),
            "app_version": d["app_version"],
            "online": is_online(d["last_heartbeat_at"], now, window_secs),
            "last_heartbeat_at": d["last_heartbeat_at"],
            "capabilities": d["capabilities"],
        }
        address = str(d.get("address") or "")
        if address:
            entry["address"] = address
        storage = d.get("storage")
        if isinstance(storage, dict) and storage:
            entry["storage"] = storage
        if hub_device_id and d["device_id"] == hub_device_id:
            entry["is_hub"] = True
        devices.append(entry)
    devices.sort(key=lambda d: (d["tier_rank"], d["device_id"]))
    return {
        "devices": devices,
        "online_window_secs": window_secs,
        "requested_by": requested_by,
    }
