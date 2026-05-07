"""工作空间 API 路由。

提供工作空间的查询、制品聚合和文件目录树 REST API 端点。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from channels.api.deps import require_auth
from workspace.workspace_service import get_workspace_service

logger = logging.getLogger(__name__)

workspaces_router = APIRouter(prefix="/api/v1/workspaces", tags=["工作空间"])


@workspaces_router.get("/{container_task_id}", summary="获取工作空间详情")
async def get_workspace(
    container_task_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取工作空间详情。

    如果工作空间不存在，自动创建。
    """
    service = get_workspace_service()
    workspace = await service.get_or_create_workspace(container_task_id)
    return workspace.to_dict()


@workspaces_router.get("/{container_task_id}/artifacts", summary="获取工作空间下所有制品")
async def get_workspace_artifacts(
    container_task_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取工作空间下所有制品（聚合容器任务下所有子任务的制品）。"""
    service = get_workspace_service()
    return await service.list_artifacts_by_workspace(container_task_id)


@workspaces_router.get("/{container_task_id}/file-tree", summary="获取文件目录树")
async def get_file_tree(
    container_task_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取工作空间的文件目录树。"""
    service = get_workspace_service()
    return await service.get_file_tree(container_task_id)
