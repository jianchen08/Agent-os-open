"""场景管理 API 路由。

提供场景的 CRUD、切换和模板查询接口。

端点：
    - POST /api/v1/scenes - 创建场景
    - GET /api/v1/scenes - 列出场景
    - GET /api/v1/scenes/templates - 获取模板列表
    - GET /api/v1/scenes/{scene_id} - 获取场景详情
    - PUT /api/v1/scenes/{scene_id} - 更新场景
    - DELETE /api/v1/scenes/{scene_id} - 删除场景
    - POST /api/v1/scenes/{scene_id}/switch - 切换场景
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from channels.api.deps import APIError, require_auth
from scene import SceneManager
from scene.models import SceneCreateRequest, SceneUpdateRequest
from scene.templates import list_templates

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/scenes",
    tags=["场景管理"],
    dependencies=[Depends(require_auth)],
)

# 全局 SceneManager 实例
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


@router.post(
    "",
    summary="创建场景",
)
def create_scene(
    request: SceneCreateRequest,
    _user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """创建新场景，可基于模板创建。

    Args:
        request: 创建场景请求体

    Returns:
        创建的场景数据

    Raises:
        APIError: 模板不存在 (400)
    """
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
        raise APIError(
            status_code=400,
            error_code="SCENE_4001",
            message=str(exc),
        ) from exc

    return scene.model_dump(mode="json")


@router.get(
    "",
    summary="列出所有场景",
)
def list_scenes(
    _user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """获取所有场景列表。

    Returns:
        包含 items 和 total 的字典
    """
    manager = _get_manager()
    scenes = manager.list_scenes()
    items = [s.model_dump(mode="json") for s in scenes]
    return {"items": items, "total": len(items)}


@router.get(
    "/templates",
    summary="获取场景模板列表",
)
def get_templates(
    _user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """获取所有预设场景模板。

    Returns:
        包含 items 和 total 的字典
    """
    templates = list_templates()
    items = [t.model_dump(mode="json") for t in templates]
    return {"items": items, "total": len(items)}


@router.get(
    "/{scene_id}",
    summary="获取场景详情",
)
def get_scene(
    scene_id: str,
    _user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """根据 ID 获取单个场景的详情。

    Args:
        scene_id: 场景唯一标识

    Returns:
        场景数据

    Raises:
        APIError: 场景不存在 (404)
    """
    manager = _get_manager()
    scene = manager.get_scene(scene_id)
    if scene is None:
        raise APIError(
            status_code=404,
            error_code="SCENE_4004",
            message=f"场景 '{scene_id}' 不存在",
        )
    return scene.model_dump(mode="json")


@router.put(
    "/{scene_id}",
    summary="更新场景",
)
def update_scene(
    scene_id: str,
    request: SceneUpdateRequest,
    _user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """更新指定场景的属性。

    Args:
        scene_id: 场景 ID
        request: 更新请求体

    Returns:
        更新后的场景数据

    Raises:
        APIError: 场景不存在 (404)
    """
    manager = _get_manager()
    scene = manager.update_scene(scene_id, request)
    if scene is None:
        raise APIError(
            status_code=404,
            error_code="SCENE_4004",
            message=f"场景 '{scene_id}' 不存在",
        )
    return scene.model_dump(mode="json")


@router.delete(
    "/{scene_id}",
    summary="删除场景",
)
def delete_scene(
    scene_id: str,
    _user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """删除指定场景及其关联数据。

    Args:
        scene_id: 场景 ID

    Returns:
        操作结果

    Raises:
        APIError: 场景不存在 (404)
    """
    manager = _get_manager()
    result = manager.delete_scene(scene_id)
    if not result:
        raise APIError(
            status_code=404,
            error_code="SCENE_4004",
            message=f"场景 '{scene_id}' 不存在",
        )
    return {"success": True, "message": "场景已删除"}


@router.post(
    "/{scene_id}/switch",
    summary="切换场景",
)
def switch_scene(
    scene_id: str,
    _user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """切换当前活跃场景，自动保存前一场景状态。

    Args:
        scene_id: 目标场景 ID

    Returns:
        切换后的活跃场景数据

    Raises:
        APIError: 场景不存在 (404)
    """
    manager = _get_manager()
    try:
        scene = manager.switch_scene(scene_id)
    except ValueError as exc:
        raise APIError(
            status_code=404,
            error_code="SCENE_4004",
            message=str(exc),
        ) from exc

    return scene.model_dump(mode="json")
