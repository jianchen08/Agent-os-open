#!/usr/bin/env python3
"""工作空间服务 MCP 服务端——纯接口适配层。

老代码从 0.1 src/workspace/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §六 P2 workspace]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin

# 直接导入同目录老代码
from workspace_service import WorkspaceService

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("workspace_service")

_service: WorkspaceService | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """初始化工作空间服务。"""
    global _service
    _service = WorkspaceService()
    logger.info("工作空间服务已加载")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """清理工作空间服务资源。"""
    global _service
    _service = None
    logger.info("工作空间服务已卸载")


@plugin.tool(
    name="workspace.get_or_create",
    schema={
        "type": "object",
        "properties": {
            "container_task_id": {
                "type": "string",
                "description": "容器任务 ID",
            },
            "session_id": {
                "type": "string",
                "description": "关联会话 ID",
                "default": "",
            },
            "title": {
                "type": "string",
                "description": "工作空间标题",
                "default": "",
            },
            "description": {
                "type": "string",
                "description": "工作空间描述",
                "default": "",
            },
        },
        "required": ["container_task_id"],
    },
    description="Get or create a workspace for a container task",
)
async def workspace_get_or_create(
    container_task_id: str,
    session_id: str = "",
    title: str = "",
    description: str = "",
) -> dict[str, Any]:
    """获取或创建工作空间。"""
    if _service is None:
        return {"success": False, "error": "服务未初始化"}

    ws = await _service.get_or_create_workspace(
        container_task_id=container_task_id,
        session_id=session_id,
        title=title,
        description=description,
    )
    return {"success": True, "workspace": ws.to_dict()}


@plugin.tool(
    name="workspace.get",
    schema={
        "type": "object",
        "properties": {
            "container_task_id": {
                "type": "string",
                "description": "容器任务 ID",
            },
        },
        "required": ["container_task_id"],
    },
    description="Get workspace details",
)
async def workspace_get(container_task_id: str) -> dict[str, Any]:
    """获取工作空间详情。"""
    if _service is None:
        return {"success": False, "error": "服务未初始化"}

    ws = await _service.get_workspace(container_task_id)
    if ws is None:
        return {"success": False, "error": f"工作空间不存在: {container_task_id}"}
    return {"success": True, "workspace": ws.to_dict()}


@plugin.tool(
    name="workspace.get_file_tree",
    schema={
        "type": "object",
        "properties": {
            "container_task_id": {
                "type": "string",
                "description": "容器任务 ID",
            },
            "base_path": {
                "type": "string",
                "description": "基础路径（可选，用于扫描真实文件目录）",
            },
        },
        "required": ["container_task_id"],
    },
    description="Generate file directory tree for a workspace",
)
async def workspace_get_file_tree(
    container_task_id: str,
    base_path: str | None = None,
) -> dict[str, Any]:
    """生成文件目录树。"""
    if _service is None:
        return {"success": False, "error": "服务未初始化"}

    result = await _service.get_file_tree(
        container_task_id=container_task_id,
        base_path=base_path,
    )
    return {"success": True, "tree": result.get("tree", [])}


if __name__ == "__main__":
    plugin.run()
