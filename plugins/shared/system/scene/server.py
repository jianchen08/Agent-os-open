#!/usr/bin/env python3
"""场景管理服务 MCP 服务端——纯接口适配层。

老代码从 0.1 src/scene/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §六 P2 scene]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from lingxi_plugin_sdk import AgentOSPlugin

# 直接导入同目录老代码
from manager import SceneManager
from templates import list_templates

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("scene_service")

_manager: SceneManager | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """初始化场景管理服务。"""
    global _manager
    _manager = SceneManager()
    logger.info("场景管理服务已加载")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """清理场景管理服务资源。"""
    global _manager
    _manager = None
    logger.info("场景管理服务已卸载")


@plugin.tool(
    name="scene.create",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "场景名称"},
            "description": {"type": "string", "default": ""},
            "template_id": {"type": "string", "description": "模板 ID（可选）"},
            "layout": {
                "type": "object",
                "description": "布局配置（可选，覆盖模板）",
            },
            "widgets": {
                "type": "array",
                "description": "组件列表（可选，覆盖模板）",
                "items": {"type": "object"},
            },
        },
        "required": ["name"],
    },
    description="Create a new scene",
)
async def scene_create(
    name: str,
    description: str = "",
    template_id: str | None = None,
    layout: dict[str, Any] | None = None,
    widgets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """创建新场景。"""
    if _manager is None:
        return {"success": False, "error": "服务未初始化"}

    try:
        from models import SceneLayoutConfig  # noqa: PLC0415

        scene_layout = None
        if layout:
            scene_layout = SceneLayoutConfig(**layout)

        scene = _manager.create_scene(
            name=name,
            description=description,
            template_id=template_id,
            layout=scene_layout,
            widgets=widgets,
        )
        return {"success": True, "scene": scene.model_dump(mode="json")}
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error("创建场景失败: %s", e)
        return {"success": False, "error": str(e)}


@plugin.tool(
    name="scene.switch",
    schema={
        "type": "object",
        "properties": {
            "scene_id": {"type": "string", "description": "目标场景 ID"},
        },
        "required": ["scene_id"],
    },
    description="Switch active scene",
)
async def scene_switch(scene_id: str) -> dict[str, Any]:
    """切换活跃场景。"""
    if _manager is None:
        return {"success": False, "error": "服务未初始化"}

    try:
        scene = _manager.switch_scene(scene_id)
        return {"success": True, "scene": scene.model_dump(mode="json")}
    except ValueError as e:
        return {"success": False, "error": str(e)}


@plugin.tool(
    name="scene.delete",
    schema={
        "type": "object",
        "properties": {
            "scene_id": {"type": "string", "description": "要删除的场景 ID"},
        },
        "required": ["scene_id"],
    },
    description="Delete a scene",
)
async def scene_delete(scene_id: str) -> dict[str, Any]:
    """删除场景。"""
    if _manager is None:
        return {"success": False, "error": "服务未初始化"}

    result = _manager.delete_scene(scene_id)
    return {"success": result}


@plugin.tool(
    name="scene.list",
    schema={"type": "object", "properties": {}},
    description="List all scenes",
)
async def scene_list() -> dict[str, Any]:
    """列出所有场景。"""
    if _manager is None:
        return {"success": False, "error": "服务未初始化"}

    scenes = _manager.list_scenes()
    return {
        "success": True,
        "scenes": [s.model_dump(mode="json") for s in scenes],
        "count": len(scenes),
    }


@plugin.tool(
    name="scene.get",
    schema={
        "type": "object",
        "properties": {
            "scene_id": {"type": "string", "description": "场景 ID"},
        },
        "required": ["scene_id"],
    },
    description="Get scene details",
)
async def scene_get(scene_id: str) -> dict[str, Any]:
    """获取场景详情。"""
    if _manager is None:
        return {"success": False, "error": "服务未初始化"}

    scene = _manager.get_scene(scene_id)
    if scene is None:
        return {"success": False, "error": f"场景不存在: {scene_id}"}
    return {"success": True, "scene": scene.model_dump(mode="json")}


@plugin.tool(
    name="scene.update",
    schema={
        "type": "object",
        "properties": {
            "scene_id": {"type": "string", "description": "场景 ID"},
            "name": {"type": "string", "description": "新名称（可选）"},
            "description": {"type": "string", "description": "新描述（可选）"},
        },
        "required": ["scene_id"],
    },
    description="Update scene properties",
)
async def scene_update(
    scene_id: str,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """更新场景属性。"""
    if _manager is None:
        return {"success": False, "error": "服务未初始化"}

    from models import SceneUpdateRequest  # noqa: PLC0415

    request = SceneUpdateRequest(name=name, description=description)
    scene = _manager.update_scene(scene_id, request)
    if scene is None:
        return {"success": False, "error": f"场景不存在: {scene_id}"}
    return {"success": True, "scene": scene.model_dump(mode="json")}


@plugin.tool(
    name="scene.get_active",
    schema={"type": "object", "properties": {}},
    description="Get the current active scene",
)
async def scene_get_active() -> dict[str, Any]:
    """获取当前活跃场景。"""
    if _manager is None:
        return {"success": False, "error": "服务未初始化"}

    scene = _manager.get_active_scene()
    if scene is None:
        return {"success": True, "scene": None}
    return {"success": True, "scene": scene.model_dump(mode="json")}


@plugin.tool(
    name="scene.list_templates",
    schema={"type": "object", "properties": {}},
    description="List preset scene templates",
)
async def scene_list_templates() -> dict[str, Any]:
    """列出所有预设场景模板。"""
    templates = list_templates()
    return {
        "success": True,
        "templates": [t.model_dump(mode="json") for t in templates],
        "count": len(templates),
    }


if __name__ == "__main__":
    plugin.run()
