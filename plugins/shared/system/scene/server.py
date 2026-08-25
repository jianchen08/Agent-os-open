#!/usr/bin/env python3
"""场景管理服务 MCP 服务端——纯接口适配层。

老代码从 0.1 src/scene/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

同时承载 scenes 域 HTTP 面：
``http.handle`` 按 path 分发（协议与 agent_manager/monitoring 同款），
plugin.json ``http_endpoints`` 声明（/ext/scene_service/scenes/**）；
业务函数在 ``routes_scene.py``。

[来源: docs/working/module_migration_plan.md §六 P2 scene]
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
_SYSTEM_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SYSTEM_DIR not in sys.path:
    sys.path.insert(0, _SYSTEM_DIR)

# 直接导入同目录老代码（sys.path 自举后导入——E402 依 workspace 迁移同款）
from scene.manager import SceneManager  # noqa: E402
from scene.templates import list_templates  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

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
        from scene.models import SceneLayoutConfig  # noqa: PLC0415

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

    from scene.models import SceneUpdateRequest  # noqa: PLC0415

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


# ══ http.handle 响应封装（内核 HttpHandleResponse 约定，与 workspace/monitoring 同款）══


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """包成内核期望的 HttpHandleResponse（body base64）。"""
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    body_b64 = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": body_b64,
        "body_encoding": "base64",
    }


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 或明文 JSON）为 dict。"""
    if not raw_body:
        return {}
    try:
        try:
            decoded = base64.b64decode(raw_body).decode("utf-8")
            if not decoded.lstrip().startswith(("{", "[")):
                decoded = raw_body
        except Exception:  # noqa: BLE001
            decoded = raw_body
        return json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc


# ══ scenes 域 ══

_PREFIX = "/ext/scene_service/scenes"


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/scene_service/** (scenes domain)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到 scenes 域 7 端点（语义对齐原 /ext/channel_api/scenes/**）。

    业务函数全 dict body；path-param {scene_id}；auth 由 http_endpoints
    auth=user 声明（dispatcher 层），handler 不读 _user。业务异常
    SceneHTTPError（status_code）转对应 HTTP 状态，404/400 body 形态与
    FastAPI 版一致（``{"detail": ...}``）。
    """
    if not path.startswith(_PREFIX):
        return _ok(_json_response({"error": "not a scenes path", "path": path}, 404))

    import routes_scene as rsc  # noqa: PLC0415

    sub = path[len(_PREFIX):]  # "" / "/templates" / "/{scene_id}" / "/{scene_id}/switch"

    try:
        # GET "" (list)
        if sub in ("", "/") and method == "GET":
            return _ok(_json_response(rsc.list_scenes()))
        # POST "" (create, dict body)
        if sub in ("", "/") and method == "POST":
            return _ok(_json_response(rsc.create_scene(_decode_body(raw_body))))
        # GET /templates
        if sub == "/templates" and method == "GET":
            return _ok(_json_response(rsc.get_templates()))
        # /{scene_id} 系列
        if sub.startswith("/") and "/" not in sub[1:]:
            scene_id = sub[1:]
            if method == "GET":
                return _ok(_json_response(rsc.get_scene(scene_id)))
            if method == "PUT":
                return _ok(_json_response(rsc.update_scene(scene_id, _decode_body(raw_body))))
            if method == "DELETE":
                return _ok(_json_response(rsc.delete_scene(scene_id)))
        # /{scene_id}/switch
        if sub.endswith("/switch") and method == "POST":
            scene_id = sub[1:].rsplit("/switch", 1)[0]
            return _ok(_json_response(rsc.switch_scene(scene_id)))

        logger.warning("scenes http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code"):
            status = int(getattr(exc, "status_code", 500) or 500)
            message = getattr(exc, "message", None) or str(exc)
            return _ok(_json_response({"detail": message}, status))
        logger.error("scenes http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


if __name__ == "__main__":
    plugin.run()
