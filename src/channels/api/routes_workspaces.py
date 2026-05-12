"""工作空间 API 路由。

提供工作空间的查询、制品聚合、文件目录树和 IDE 打开操作 REST API 端点。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any

from pathlib import Path

from fastapi import APIRouter, Depends, Query

from channels.api.deps import require_auth
from workspace.workspace_service import get_workspace_service

logger = logging.getLogger(__name__)

workspaces_router = APIRouter(prefix="/api/v1/workspaces", tags=["工作空间"])


def _get_connector_registry() -> Any:
    """获取全局 ConnectorRegistry 单例。

    通过 ServiceProvider 获取或创建 ConnectorRegistry 实例，
    确保整个应用共享同一个连接器注册表。

    Returns:
        ConnectorRegistry 实例
    """
    from connectors.registry import ConnectorRegistry
    from infrastructure.service_provider import get_service_provider

    provider = get_service_provider()
    return provider.get_or_create(
        "connector_registry",
        lambda: ConnectorRegistry(),
    )


@workspaces_router.post("/open-file", summary="在IDE中打开文件")
async def open_file_in_ide(
    body: dict[str, Any],
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """在 IDE 中打开指定文件。

    通过活跃连接器的 open_file 能力在 IDE 中打开指定路径的文件，
    可选跳转到指定行和列。

    Args:
        body: 请求体，包含 file_path（必需）、line（可选）、column（可选）
        _user: 已认证用户信息

    Returns:
        包含 success、message、file_path 的操作结果字典
    """
    file_path = body.get("file_path", "")
    if not file_path:
        return {
            "success": False,
            "message": "file_path 参数不能为空",
            "file_path": None,
        }

    line = body.get("line")
    column = body.get("column")

    registry = _get_connector_registry()
    connector = registry.get_best_connector_for("open_file")

    if connector is None:
        return {
            "success": False,
            "message": "当前没有可用的 IDE 连接器，请确保 VSCode 扩展已启动并连接",
            "file_path": file_path,
        }

    from connectors.types import ConnectorAction

    params: dict[str, Any] = {"file_path": file_path}
    if line is not None:
        params["line"] = line
    if column is not None:
        params["column"] = column

    action = ConnectorAction(
        action_type="open_file",
        parameters=params,
    )
    try:
        result = await connector.execute_action(action)
        if result.success:
            return {
                "success": True,
                "message": f"已在 {connector.connector_type} 中打开文件: {file_path}",
                "file_path": file_path,
            }
        return {
            "success": False,
            "message": f"连接器执行失败: {result.error}",
            "file_path": file_path,
        }
    except Exception as e:
        logger.warning("通过连接器打开文件失败: %s", e)
        return {
            "success": False,
            "message": f"打开文件失败: {e}",
            "file_path": file_path,
        }


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
    workspace_path = await _resolve_workspace_path(container_task_id)
    service = get_workspace_service()
    return await service.get_file_tree(container_task_id, base_path=workspace_path)


@workspaces_router.get("/{container_task_id}/file-content", summary="读取文件内容")
async def get_file_content(
    container_task_id: str,
    path: str = Query(..., description="文件在工作空间中的相对路径"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """读取工作空间中指定文件的内容。

    通过容器任务的 metadata.ws_meta.path 定位工作空间根目录，
    然后拼接相对路径读取文件内容。限制只读取文本文件，最大 1MB。

    Args:
        container_task_id: 容器任务 ID
        path: 文件相对路径
        _user: 已认证用户信息

    Returns:
        包含 success、content、path、size 的字典
    """
    # 1. 解析工作空间根路径
    workspace_path_str = await _resolve_workspace_path(container_task_id)
    if not workspace_path_str:
        return {
            "success": False,
            "message": "未找到工作空间路径",
        }

    workspace_path = Path(workspace_path_str).resolve()

    # 2. 拼接完整路径并做安全检查（防止路径穿越）
    full_path = (workspace_path / path).resolve()
    if not str(full_path).startswith(str(workspace_path)):
        return {
            "success": False,
            "message": "路径超出工作空间范围",
        }

    # 3. 检查文件存在且为普通文件
    if not full_path.is_file():
        return {
            "success": False,
            "message": f"文件不存在或不是普通文件: {path}",
        }

    # 4. 检查文件大小（限制 1MB）
    MAX_SIZE = 1 * 1024 * 1024  # 1MB
    file_size = full_path.stat().st_size
    if file_size > MAX_SIZE:
        return {
            "success": False,
            "message": f"文件过大（{file_size} 字节），超过 1MB 限制",
        }

    # 5. 读取文件内容
    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
        return {
            "success": True,
            "content": content,
            "path": path,
            "size": file_size,
        }
    except Exception as e:
        logger.warning("读取工作空间文件失败: %s | path=%s", e, path)
        return {
            "success": False,
            "message": f"读取文件失败: {e}",
        }


@workspaces_router.put("/{container_task_id}/file-content", summary="保存文件内容")
async def save_file_content(
    container_task_id: str,
    path: str = Query(..., description="文件在工作空间中的相对路径"),
    body: dict[str, Any] = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """保存文件内容到工作空间。"""
    if body is None:
        body = {}
    content = body.get("content", "")
    workspace_path_str = await _resolve_workspace_path(container_task_id)
    if not workspace_path_str:
        return {"success": False, "message": "未找到工作空间路径"}

    workspace_path = Path(workspace_path_str).resolve()
    full_path = (workspace_path / path).resolve()
    if not str(full_path).startswith(str(workspace_path)):
        return {"success": False, "message": "路径超出工作空间范围"}

    MAX_SIZE = 1 * 1024 * 1024
    if len(content.encode("utf-8")) > MAX_SIZE:
        return {"success": False, "message": "内容过大，超过 1MB 限制"}

    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return {"success": True, "path": path, "size": len(content.encode("utf-8"))}
    except Exception as e:
        logger.warning("保存工作空间文件失败: %s | path=%s", e, path)
        return {"success": False, "message": f"保存文件失败: {e}"}


@workspaces_router.post("/{container_task_id}/open", summary="在IDE中打开工作空间")
async def open_workspace_in_ide(
    container_task_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """在 IDE 中打开指定任务的工作空间目录。

    通过容器任务 ID 查询关联的工作空间路径，
    并调用活跃连接器的 open_folder 能力在 IDE 中打开。

    Args:
        container_task_id: 容器任务 ID
        _user: 已认证用户信息

    Returns:
        包含 success、message、path 的操作结果字典
    """
    # 1. 从任务 metadata 中获取工作空间路径
    workspace_path = await _resolve_workspace_path(container_task_id)
    if not workspace_path:
        return {
            "success": False,
            "message": f"未找到任务 {container_task_id[:8]} 的工作空间路径，请确认任务已分配工作空间",
            "path": None,
        }

    # 2. 查找支持 open_folder 能力的活跃连接器
    registry = _get_connector_registry()
    connector = registry.get_best_connector_for("open_folder")

    if connector is None:
        # 无 IDE 连接器时，fallback 到系统文件管理器
        opened = _open_in_system_file_manager(workspace_path)
        if opened:
            return {
                "success": True,
                "message": "已在系统文件管理器中打开工作空间",
                "path": workspace_path,
            }
        return {
            "success": False,
            "message": "当前没有可用的 IDE 连接器，且无法启动系统文件管理器",
            "path": workspace_path,
        }

    # 3. 通过连接器发送 open_folder 操作
    from connectors.types import ConnectorAction

    action = ConnectorAction(
        action_type="open_folder",
        parameters={"path": workspace_path},
    )
    try:
        result = await connector.execute_action(action)
        if result.success:
            return {
                "success": True,
                "message": f"已在 {connector.connector_type} 中打开工作空间",
                "path": workspace_path,
            }
        return {
            "success": False,
            "message": f"连接器执行失败: {result.error}",
            "path": workspace_path,
        }
    except Exception as e:
        logger.warning("通过连接器打开工作空间失败: %s", e)
        return {
            "success": False,
            "message": f"打开工作空间失败: {e}",
            "path": workspace_path,
        }


async def _resolve_workspace_path(container_task_id: str) -> str | None:
    """从任务 metadata 中解析工作空间路径。

    通过 TaskService 获取任务实例，从其 metadata.ws_meta.path 字段提取
    工作空间路径。

    Args:
        container_task_id: 容器任务 ID

    Returns:
        工作空间路径字符串，未找到时返回 None
    """
    try:
        from infrastructure.service_provider import get_service_provider

        provider = get_service_provider()
        task_service = provider.get_or_create(
            "task_service",
            lambda: __import__("tasks.service", fromlist=["TaskService"]).TaskService(),
        )

        task = task_service.get_task(container_task_id)
        if not task:
            return None

        # 安全提取 metadata.ws_meta.path
        _metadata = getattr(task, "metadata", None) or {}
        _ws_meta = _metadata.get("ws_meta", {}) or {}
        return _ws_meta.get("path")

    except Exception:
        logger.warning(
            "解析工作空间路径失败 | container_task_id=%s",
            container_task_id,
        )
        return None


def _open_in_system_file_manager(directory_path: str) -> bool:
    """使用系统文件管理器打开指定目录。

    Windows 使用 explorer.exe，macOS 使用 open，Linux 使用 xdg-open。

    Args:
        directory_path: 要打开的目录路径

    Returns:
        是否成功启动文件管理器
    """
    try:
        resolved = Path(directory_path).resolve()
        if not resolved.exists():
            logger.warning("目录不存在，无法打开: %s", resolved)
            return False

        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(resolved)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(resolved)])
        else:
            subprocess.Popen(["xdg-open", str(resolved)])

        logger.info("已在系统文件管理器中打开: %s", resolved)
        return True
    except Exception as e:
        logger.warning("打开系统文件管理器失败: %s", e)
        return False
