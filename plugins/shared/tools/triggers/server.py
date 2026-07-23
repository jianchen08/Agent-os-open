#!/usr/bin/env python3
"""触发器系统 MCP 服务端。

常驻系统插件，监听 Cron/事件/间隔，触发管道执行。
核心业务逻辑参考 0.1 src/triggers/manager.py。

[来源: docs/tasks/task_10_system_plugins.md AC-09-5]
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("trigger_service")

# 注册的触发器
_triggers: dict[str, dict[str, Any]] = {}


@plugin.tool(
    name="trigger.register",
    schema={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["cron", "event", "interval"]},
            "schedule": {"type": "string"},
            "action": {
                "type": "object",
                "properties": {
                    "pipeline": {"type": "string"},
                    "config": {"type": "object", "default": {}},
                },
                "required": ["pipeline"],
            },
            "tenant_id": {"type": "string"},
            "enabled": {"type": "boolean", "default": True},
        },
        "required": ["type", "schedule", "action"],
    },
    description="Register a new trigger (cron/event/interval) to start pipelines",
)
async def trigger_register(
    type: str,
    schedule: str,
    action: dict[str, Any],
    tenant_id: str = "default",
    enabled: bool = True,
) -> dict[str, Any]:
    """Register a trigger that starts a pipeline when fired."""
    trigger_id = f"trig_{uuid.uuid4().hex[:8]}"

    trigger = {
        "id": trigger_id,
        "type": type,
        "schedule": schedule,
        "action": action,
        "tenant_id": tenant_id,
        "enabled": enabled,
        "created_at": time.time(),
        "last_fired": None,
        "fire_count": 0,
    }

    _triggers[trigger_id] = trigger

    # DEBT: cron/event-bus 订阅未实现。ceiling: 当前仅内存注册。
    # upgrade: 引擎层集成后通过 event-bus 句柄订阅事件。
    # event_bus = plugin.get_capability("event-bus")
    # if type == "event":
    #     await event_bus.call("subscribe", {"event": schedule, "handler": trigger_id})

    return {"trigger_id": trigger_id, "status": "registered", "enabled": enabled}


@plugin.tool(
    name="trigger.cancel",
    schema={
        "type": "object",
        "properties": {
            "trigger_id": {"type": "string"},
        },
        "required": ["trigger_id"],
    },
    description="Cancel and remove a registered trigger",
)
async def trigger_cancel(trigger_id: str) -> dict[str, Any]:
    """Cancel a registered trigger."""
    trigger = _triggers.get(trigger_id)
    if trigger is None:
        return {"error": "trigger not found", "trigger_id": trigger_id}

    del _triggers[trigger_id]
    return {"trigger_id": trigger_id, "status": "cancelled"}


@plugin.tool(
    name="trigger.list",
    schema={
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["cron", "event", "interval"]},
            "enabled_only": {"type": "boolean", "default": False},
        },
    },
    description="List registered triggers, optionally filtered by type",
)
async def trigger_list(
    type: str | None = None,
    enabled_only: bool = False,
) -> dict[str, Any]:
    """List all registered triggers, optionally filtered."""
    results = []
    for trigger in _triggers.values():
        if type and trigger["type"] != type:
            continue
        if enabled_only and not trigger["enabled"]:
            continue
        results.append(trigger)

    return {"triggers": results, "count": len(results)}


@plugin.on_load
async def on_load(params: dict[str, Any]) -> None:
    """Initialize trigger service on load."""
    # DEBT: 从持久化存储恢复触发器未实现。ceiling: 当前仅内存状态。
    # upgrade: SQLite 存储集成后恢复持久化触发器。
    pass


@plugin.on_unload
async def on_unload(params: dict[str, Any]) -> None:
    """Cleanup on unload."""
    _triggers.clear()


if __name__ == "__main__":
    plugin.run()
