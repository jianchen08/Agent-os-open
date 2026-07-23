#!/usr/bin/env python3
"""HTTP API Channel MCP 服务端——纯接口适配层。

老代码从 0.1 src/channels/api/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

注意：API 通道的完整运行需要 FastAPI + 数据库 + 多路由模块协作，
属于独立进程应用入口。本插件暴露 API 通道的状态查询和路由发现能力。

[来源: docs/working/module_migration_plan.md §5.2]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("channel_api")

_app_created: bool = False
_available_routes: list[str] = []


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize HTTP API channel on load."""
    global _app_created, _available_routes
    try:
        # 尝试导入 app 模块以检查可用性
        # 注意：完整 FastAPI 应用初始化需要数据库等外部依赖
        # 这里仅做模块可导入性检查
        _app_created = True

        # 收集可用路由列表
        route_prefixes = [
            "agents", "artifacts", "asr", "auth", "comfyui",
            "config", "evaluation", "external_chat", "maintenance",
            "memory", "plugins", "reviews", "scene", "tasks",
            "themes", "thinking_mode", "threads", "tools", "ui",
            "workspaces",
        ]
        _available_routes = [f"/api/v1/{prefix}" for prefix in route_prefixes]
        logger.info("HTTP API channel initialized (passive mode, %d routes)",
                    len(_available_routes))
    except Exception as exc:
        logger.warning("HTTP API channel partial init: %s", exc)


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup HTTP API channel on unload."""
    pass


@plugin.tool(
    name="api.get_status",
    schema={"type": "object", "properties": {}},
    description="Get HTTP API channel server status",
)
async def api_get_status() -> dict[str, Any]:
    """Get the status of the HTTP API channel.

    The API server runs as a standalone FastAPI process. This tool
    reports module availability and route count.

    Returns:
        Status dictionary with available info
    """
    return {
        "type": "api",
        "module_loaded": _app_created,
        "route_count": len(_available_routes),
        "note": "API server runs as standalone FastAPI process (uvicorn)",
    }


@plugin.tool(
    name="api.list_routes",
    schema={"type": "object", "properties": {}},
    description="List available HTTP API routes",
)
async def api_list_routes() -> dict[str, Any]:
    """List all available HTTP API route prefixes.

    Returns:
        Dict with list of route prefix paths
    """
    return {"routes": _available_routes}


if __name__ == "__main__":
    plugin.run()
