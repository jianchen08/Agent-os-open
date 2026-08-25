#!/usr/bin/env python3
"""连接器服务 MCP 服务端——纯接口适配层。

老代码从 0.1 src/connectors/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §六 P2 connectors]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# 直接导入同目录老代码（文件就在旁边，通过 sys.path 可见）
from agentos_plugin_sdk.adapter_config import get_adapter_status_summary
from degradation import DegradationManager
from registry import ConnectorRegistry

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("connectors_service")

# 全局实例
_registry: ConnectorRegistry | None = None
_degradation: DegradationManager | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """初始化连接器服务。"""
    global _registry, _degradation
    _registry = ConnectorRegistry()
    _degradation = DegradationManager()
    logger.info("连接器服务已加载")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """清理连接器服务资源。"""
    global _registry, _degradation
    if _registry is not None:
        _registry.clear()
    _registry = None
    _degradation = None
    logger.info("连接器服务已卸载")


@plugin.tool(
    name="connector.register",
    schema={
        "type": "object",
        "properties": {
            "connector_type": {
                "type": "string",
                "description": "连接器类型标识，如 vscode",
            },
        },
        "required": ["connector_type"],
    },
    description="Register a connector by type (instantiates built-in connector)",
)
async def connector_register(connector_type: str) -> dict[str, Any]:
    """注册连接器。

    根据类型名称创建并注册连接器实例。目前内置支持 vscode。
    """
    if _registry is None:
        return {"success": False, "error": "服务未初始化"}

    if connector_type == "vscode":
        from vscode.connector import VSCodeConnector  # noqa: PLC0415

        connector = VSCodeConnector()
        _registry.register(connector)
        return {"success": True, "connector_type": connector_type}
    return {
        "success": False,
        "error": f"不支持的连接器类型: {connector_type}",
    }


@plugin.tool(
    name="connector.unregister",
    schema={
        "type": "object",
        "properties": {
            "connector_type": {
                "type": "string",
                "description": "要注销的连接器类型",
            },
        },
        "required": ["connector_type"],
    },
    description="Unregister a connector by type",
)
async def connector_unregister(connector_type: str) -> dict[str, Any]:
    """注销连接器。"""
    if _registry is None:
        return {"success": False, "error": "服务未初始化"}

    try:
        _registry.unregister(connector_type)
        return {"success": True}
    except KeyError:
        return {"success": False, "error": f"连接器不存在: {connector_type}"}


@plugin.tool(
    name="connector.list",
    schema={"type": "object", "properties": {}},
    description="List all registered connectors",
)
async def connector_list() -> dict[str, Any]:
    """列出所有已注册连接器。"""
    if _registry is None:
        return {"success": False, "error": "服务未初始化"}

    infos = _registry.list_connectors()
    return {
        "success": True,
        "connectors": [
            {
                "connector_type": info.connector_type,
                "display_name": info.display_name,
                "capabilities": info.capabilities,
                "priority": info.priority,
            }
            for info in infos
        ],
        "count": len(infos),
    }


@plugin.tool(
    name="connector.get_active",
    schema={"type": "object", "properties": {}},
    description="Get the active connector (highest priority among connected)",
)
async def connector_get_active() -> dict[str, Any]:
    """获取当前活跃连接器（按优先级排序）。"""
    if _registry is None:
        return {"success": False, "error": "服务未初始化"}

    conn = _registry.get_active_connector()
    if conn is None:
        return {"success": True, "connector": None}
    return {"success": True, "connector": conn.get_status()}


@plugin.tool(
    name="connector.get_status",
    schema={
        "type": "object",
        "properties": {
            "connector_type": {
                "type": "string",
                "description": "连接器类型（可选，不传则返回全部状态）",
            },
        },
    },
    description="Get connector status summary",
)
async def connector_get_status(connector_type: str = "") -> dict[str, Any]:
    """获取连接器状态信息。"""
    if _registry is None:
        return {"success": False, "error": "服务未初始化"}

    if connector_type:
        conn = _registry.get_connector(connector_type)
        if conn is None:
            return {"success": False, "error": f"连接器不存在: {connector_type}"}
        return {"success": True, "status": conn.get_status()}

    # 通过公共接口遍历，不访问 registry 内部数据结构
    infos = _registry.list_connectors()
    statuses = []
    for info in infos:
        conn = _registry.get_connector(info.connector_type)
        if conn is not None:
            statuses.append(conn.get_status())
    return {"success": True, "statuses": statuses, "count": len(statuses)}


@plugin.tool(
    name="connector.get_adapter_status",
    schema={"type": "object", "properties": {}},
    description="Get adapter configuration status summary",
)
async def connector_get_adapter_status() -> dict[str, Any]:
    """获取适配器配置状态摘要。"""
    try:
        summary = get_adapter_status_summary()
        return {"success": True, "adapters": summary}
    except Exception as e:
        logger.error("获取适配器状态失败: %s", e)
        return {"success": False, "error": str(e)}


@plugin.tool(
    name="connector.degrade",
    schema={
        "type": "object",
        "properties": {
            "action_type": {
                "type": "string",
                "description": "操作类型: open_file/get_selection/show_diff/insert_content/jump_to",
            },
            "params": {
                "type": "object",
                "description": "操作参数",
                "default": {},
            },
        },
        "required": ["action_type"],
    },
    description="Execute action with fallback degradation when no connector is available",
)
async def connector_degrade(
    action_type: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """降级执行操作。"""
    if _degradation is None:
        return {"success": False, "error": "服务未初始化"}

    result = _degradation.execute_with_fallback(
        action_type, params or {}
    )
    return {
        "success": result.success,
        "data": result.data,
        "error": result.error,
    }


if __name__ == "__main__":
    plugin.run()
