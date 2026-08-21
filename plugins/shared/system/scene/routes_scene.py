"""场景管理 API 路由（scenes 域）——自持版，由 scene_service http.handle 分发。

迁移自 channel_api/routes_scene.py（channel_api 退役方案批次 1：scenes 域 → scene 插件）：

- 业务函数原样保留（SceneManager 实例化/JSON 持久化路径不变），响应形态与
  /ext/channel_api/scenes/** 逐项对齐（前端直接消费）；
- 剥离 FastAPI 依赖：无 APIRouter/Depends/require_auth，请求体由 server.py
  http.handle 解码为 dict 后经 pydantic 模型（SceneCreateRequest/SceneUpdateRequest）
  还原校验——语义与 FastAPI 路由一致；
- 出错抛 :class:`SceneHTTPError`（status_code/error_code/message），由 server.py
  http.handle 统一捕获转对应 HTTP 状态（404/400 形态与旧版一致：body
  ``{"detail": ...}``）；
- 鉴权由内核 dispatcher 按 http_endpoints.auth=user 完成，handler 不读身份。

[来源: docs/working/channel_api插件拆迁方案_20260821.md 批次 1]
"""

from __future__ import annotations

import logging
from typing import Any

from scene.manager import SceneManager
from scene.models import SceneCreateRequest, SceneUpdateRequest
from scene.templates import list_templates

logger = logging.getLogger(__name__)


class SceneHTTPError(Exception):
    """scenes 域业务异常，携带 HTTP 状态码与错误码（server.py 捕获转 HTTP 响应）。"""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        super().__init__(message)


# 全局 SceneManager 实例（与 channel_api 同款模块级单例；测试可直接替换）
_scene_manager: SceneManager | None = None


def _get_manager() -> SceneManager:
    """获取或创建 SceneManager 单例。

    Returns:
        SceneManager 实例
    """
    global _scene_manager  # noqa: PLW0603
    if _scene_manager is None:
        _scene_manager = SceneManager()
    return _scene_manager


def create_scene(body: dict[str, Any]) -> dict[str, Any]:
    """创建新场景，可基于模板创建。

    Args:
        body: 创建场景请求体（server.py 解码，等价原 SceneCreateRequest）

    Returns:
        创建的场景数据

    Raises:
        SceneHTTPError: 模板不存在 (400)
    """
    request = SceneCreateRequest(**body)
    manager = _get_manager()
    try:
        scene = manager.create_scene(
            name=request.name,
            description=request.description,
            template_id=request.template_id,
            layout=request.layout,
            widgets=([w.model_dump(mode="json") for w in request.widgets] if request.widgets else None),
        )
    except ValueError as exc:
        raise SceneHTTPError(
            status_code=400,
            error_code="SCENE_4001",
            message=str(exc),
        ) from exc

    return scene.model_dump(mode="json")


def list_scenes() -> dict[str, Any]:
    """获取所有场景列表。

    Returns:
        包含 items 和 total 的字典
    """
    manager = _get_manager()
    scenes = manager.list_scenes()
    items = [s.model_dump(mode="json") for s in scenes]
    return {"items": items, "total": len(items)}


def get_templates() -> dict[str, Any]:
    """获取所有预设场景模板。

    Returns:
        包含 items 和 total 的字典
    """
    templates = list_templates()
    items = [t.model_dump(mode="json") for t in templates]
    return {"items": items, "total": len(items)}


def get_scene(scene_id: str) -> dict[str, Any]:
    """根据 ID 获取单个场景的详情。

    Args:
        scene_id: 场景唯一标识

    Returns:
        场景数据

    Raises:
        SceneHTTPError: 场景不存在 (404)
    """
    manager = _get_manager()
    scene = manager.get_scene(scene_id)
    if scene is None:
        raise SceneHTTPError(
            status_code=404,
            error_code="SCENE_4004",
            message=f"场景 '{scene_id}' 不存在",
        )
    return scene.model_dump(mode="json")


def update_scene(scene_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """更新指定场景的属性。

    Args:
        scene_id: 场景 ID
        body: 更新请求体（server.py 解码，等价原 SceneUpdateRequest）

    Returns:
        更新后的场景数据

    Raises:
        SceneHTTPError: 场景不存在 (404)
    """
    request = SceneUpdateRequest(**body)
    manager = _get_manager()
    scene = manager.update_scene(scene_id, request)
    if scene is None:
        raise SceneHTTPError(
            status_code=404,
            error_code="SCENE_4004",
            message=f"场景 '{scene_id}' 不存在",
        )
    return scene.model_dump(mode="json")


def delete_scene(scene_id: str) -> dict[str, Any]:
    """删除指定场景及其关联数据。

    Args:
        scene_id: 场景 ID

    Returns:
        操作结果

    Raises:
        SceneHTTPError: 场景不存在 (404)
    """
    manager = _get_manager()
    result = manager.delete_scene(scene_id)
    if not result:
        raise SceneHTTPError(
            status_code=404,
            error_code="SCENE_4004",
            message=f"场景 '{scene_id}' 不存在",
        )
    return {"success": True, "message": "场景已删除"}


def switch_scene(scene_id: str) -> dict[str, Any]:
    """切换当前活跃场景，自动保存前一场景状态。

    Args:
        scene_id: 目标场景 ID

    Returns:
        切换后的活跃场景数据

    Raises:
        SceneHTTPError: 场景不存在 (404)
    """
    manager = _get_manager()
    try:
        scene = manager.switch_scene(scene_id)
    except ValueError as exc:
        raise SceneHTTPError(
            status_code=404,
            error_code="SCENE_4004",
            message=str(exc),
        ) from exc

    return scene.model_dump(mode="json")
