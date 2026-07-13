"""工作空间 API 路由。

提供工作空间的查询、制品聚合、文件目录树和 IDE 打开操作 REST API 端点。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

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
    from connectors.registry import ConnectorRegistry  # noqa: PLC0415
    from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

    provider = get_service_provider()
    return provider.get_or_create(
        "connector_registry",
        ConnectorRegistry,
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

    from connectors.types import ConnectorAction  # noqa: PLC0415

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
    path: str = Query(..., description="文件路径（绝对路径或相对路径）"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """读取指定文件的内容。

    优先通过 container_task_id 解析工作空间根路径（兼容文件树点击场景），
    解析失败时直接按传入的路径读取（兼容交互场景的绝对路径）。

    Args:
        container_task_id: 容器任务 ID（传入 _local 等非任务 ID 时忽略）
        path: 文件路径（绝对路径或相对于工作空间的相对路径）
        _user: 已认证用户信息

    Returns:
        包含 success、content、path、size 的字典
    """
    workspace_path_str = await _resolve_workspace_path(container_task_id)
    raw_path = Path(path)
    project_root = _get_project_root()

    if raw_path.is_absolute():
        full_path = raw_path.resolve()
    elif workspace_path_str:
        workspace_root = Path(workspace_path_str).resolve()
        full_path = (workspace_root / path).resolve()
        # 相对路径 + 有工作空间时，确保不超出工作空间范围
        if not full_path.is_relative_to(workspace_root):
            return {
                "success": False,
                "message": "路径超出工作空间范围",
            }
    else:
        full_path = (project_root / path).resolve()
        # 相对路径且无工作空间时，确保不超出项目根
        if not full_path.is_relative_to(project_root):
            return {
                "success": False,
                "message": "路径超出工作空间范围",
            }

    if not full_path.is_file():
        return {
            "success": False,
            "message": f"文件不存在或不是普通文件: {path}",
        }

    MAX_SIZE = 10 * 1024 * 1024  # noqa: N806
    file_size = full_path.stat().st_size
    if file_size > MAX_SIZE:
        return {
            "success": False,
            "message": f"文件过大（{file_size} 字节），超过 10MB 限制",
        }

    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
        return {
            "success": True,
            "content": content,
            "path": path,
            "size": file_size,
        }
    except Exception as e:
        logger.warning("读取文件失败: %s | path=%s", e, path)
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

    MAX_SIZE = 10 * 1024 * 1024  # noqa: N806
    if len(content.encode("utf-8")) > MAX_SIZE:
        return {
            "success": False,
            "message": f"内容过大（{len(content.encode('utf-8'))} 字节），超过 {MAX_SIZE // (1024 * 1024)}MB 限制",
        }

    if not full_path.parent.exists():
        return {"success": False, "message": f"目标目录不存在: {full_path.parent}"}

    try:
        full_path.write_text(content, encoding="utf-8")
        return {"success": True, "path": path, "size": len(content.encode("utf-8"))}
    except Exception as e:
        logger.warning("保存工作空间文件失败: %s | path=%s", e, path)
        return {"success": False, "message": f"保存文件失败: {e}"}


def _validate_path_in_workspace(workspace_path: Path, rel_path: str) -> Path | None:
    """验证相对路径在工作空间范围内，防止路径穿越攻击。

    Args:
        workspace_path: 工作空间根路径（已 resolve）
        rel_path: 相对路径字符串

    Returns:
        安全的完整路径，不安全时返回 None
    """
    full_path = (workspace_path / rel_path).resolve()
    if not str(full_path).startswith(str(workspace_path)):
        return None
    return full_path


@workspaces_router.post("/{container_task_id}/create-entry", summary="创建文件或文件夹")
async def create_entry(  # noqa: PLR0911
    container_task_id: str,
    body: dict[str, Any],
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """在工作空间中创建文件或文件夹。

    Args:
        container_task_id: 容器任务 ID
        body: 请求体，包含 path（必需）和 type（必需：file 或 directory）
        _user: 已认证用户信息

    Returns:
        包含 success、message、path 的操作结果字典
    """
    path = body.get("path", "")
    entry_type = body.get("type", "")

    if not path:
        return {"success": False, "message": "path 参数不能为空"}

    if entry_type not in ("file", "directory"):
        return {"success": False, "message": "type 参数必须为 file 或 directory"}

    workspace_path_str = await _resolve_workspace_path(container_task_id)
    if not workspace_path_str:
        return {"success": False, "message": "未找到工作空间路径"}

    workspace_path = Path(workspace_path_str).resolve()
    full_path = _validate_path_in_workspace(workspace_path, path)
    if full_path is None:
        return {"success": False, "message": "路径超出工作空间范围"}

    if full_path.exists():
        return {"success": False, "message": f"路径已存在: {path}"}

    try:
        if entry_type == "directory":
            full_path.mkdir(parents=True, exist_ok=False)
        else:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text("", encoding="utf-8")

        return {"success": True, "message": f"创建成功: {path}", "path": path}
    except Exception as e:
        logger.warning("创建文件/文件夹失败: %s | path=%s", e, path)
        return {"success": False, "message": f"创建失败: {e}"}


@workspaces_router.delete("/{container_task_id}/entries", summary="删除文件或文件夹")
async def delete_entry(  # noqa: PLR0911
    container_task_id: str,
    path: str = Query("", description="要删除的文件或文件夹相对路径（query 参数，兼容旧调用）"),
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """删除工作空间中的文件或文件夹。

    Args:
        container_task_id: 容器任务 ID
        path: query 参数，要删除的相对路径（兼容旧调用）
        body: 请求体，含 {"path": "相对路径"}（与 create-entry/rename-entry/move-entry 一致）
        _user: 已认证用户信息

    Returns:
        包含 success、message 的操作结果字典

    Note:
        path 优先从 body 读取（与其它 entry 操作一致）；body 缺失时回退到 query 参数，
        以保持对历史 query 调用方的向后兼容。
    """
    # 优先 body（与 create/rename/move 一致），fallback 到 query 参数
    path = (body or {}).get("path", "") or path
    if not path:
        return {"success": False, "message": "path 参数不能为空"}

    workspace_path_str = await _resolve_workspace_path(container_task_id)
    if not workspace_path_str:
        return {"success": False, "message": "未找到工作空间路径"}

    workspace_path = Path(workspace_path_str).resolve()
    full_path = _validate_path_in_workspace(workspace_path, path)
    if full_path is None:
        return {"success": False, "message": "路径超出工作空间范围"}

    # 禁止删除根目录
    if full_path == workspace_path:
        return {"success": False, "message": "禁止删除工作空间根目录"}

    if not full_path.exists():
        return {"success": False, "message": f"路径不存在: {path}"}

    try:
        if full_path.is_dir():
            import shutil  # noqa: PLC0415

            shutil.rmtree(full_path)
        else:
            full_path.unlink()

        return {"success": True, "message": f"删除成功: {path}"}
    except Exception as e:
        logger.warning("删除文件/文件夹失败: %s | path=%s", e, path)
        return {"success": False, "message": f"删除失败: {e}"}


@workspaces_router.post("/{container_task_id}/rename-entry", summary="重命名文件或文件夹")
async def rename_entry(  # noqa: PLR0911
    container_task_id: str,
    body: dict[str, Any],
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """重命名工作空间中的文件或文件夹。

    Args:
        container_task_id: 容器任务 ID
        body: 请求体，包含 old_path（必需）和 new_name（必需）
        _user: 已认证用户信息

    Returns:
        包含 success、message、old_path、new_path 的操作结果字典
    """
    old_path = body.get("old_path", "")
    new_name = body.get("new_name", "")

    if not old_path:
        return {"success": False, "message": "old_path 参数不能为空"}
    if not new_name:
        return {"success": False, "message": "new_name 参数不能为空"}

    # new_name 不能包含路径分隔符（防止路径穿越）
    if "/" in new_name or "\\" in new_name:
        return {"success": False, "message": "new_name 不能包含路径分隔符"}

    workspace_path_str = await _resolve_workspace_path(container_task_id)
    if not workspace_path_str:
        return {"success": False, "message": "未找到工作空间路径"}

    workspace_path = Path(workspace_path_str).resolve()
    full_old_path = _validate_path_in_workspace(workspace_path, old_path)
    if full_old_path is None:
        return {"success": False, "message": "路径超出工作空间范围"}

    if not full_old_path.exists():
        return {"success": False, "message": f"路径不存在: {old_path}"}

    # 计算新路径：在同一个目录下替换文件/目录名
    full_new_path = full_old_path.parent / new_name
    # 确保新路径也在工作空间范围内
    if not str(full_new_path).startswith(str(workspace_path)):
        return {"success": False, "message": "目标路径超出工作空间范围"}

    if full_new_path.exists():
        return {"success": False, "message": f"目标名称已存在: {new_name}"}

    # 计算新的相对路径
    new_rel_path = str(Path(old_path).parent / new_name) if Path(old_path).parent != Path() else new_name

    try:
        full_old_path.rename(full_new_path)
        return {
            "success": True,
            "message": f"重命名成功: {old_path} -> {new_rel_path}",
            "old_path": old_path,
            "new_path": new_rel_path,
        }
    except Exception as e:
        logger.warning("重命名文件/文件夹失败: %s | old_path=%s", e, old_path)
        return {"success": False, "message": f"重命名失败: {e}"}


@workspaces_router.post("/{container_task_id}/move-entry", summary="移动文件或文件夹")
async def move_entry(  # noqa: PLR0911
    container_task_id: str,
    body: dict[str, Any],
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """移动工作空间中的文件或文件夹到指定目录。

    Args:
        container_task_id: 容器任务 ID
        body: 请求体，包含 source_path（必需）和 destination_dir（必需）
        _user: 已认证用户信息

    Returns:
        包含 success、message、source_path、destination_path 的操作结果字典
    """
    source_path = body.get("source_path", "")
    destination_dir = body.get("destination_dir", "")

    if not source_path:
        return {"success": False, "message": "source_path 参数不能为空"}
    if not destination_dir:
        return {"success": False, "message": "destination_dir 参数不能为空"}

    workspace_path_str = await _resolve_workspace_path(container_task_id)
    if not workspace_path_str:
        return {"success": False, "message": "未找到工作空间路径"}

    workspace_path = Path(workspace_path_str).resolve()
    full_source = _validate_path_in_workspace(workspace_path, source_path)
    if full_source is None:
        return {"success": False, "message": "源路径超出工作空间范围"}

    full_dest_dir = _validate_path_in_workspace(workspace_path, destination_dir)
    if full_dest_dir is None:
        return {"success": False, "message": "目标路径超出工作空间范围"}

    if not full_source.exists():
        return {"success": False, "message": f"源路径不存在: {source_path}"}

    if not full_dest_dir.is_dir():
        return {"success": False, "message": f"目标目录不存在或不是目录: {destination_dir}"}

    # 禁止移动到自身子目录
    if str(full_dest_dir).startswith(str(full_source) + os.sep):
        return {"success": False, "message": "不能将目录移动到其自身子目录中"}

    dest_full_path = full_dest_dir / full_source.name
    if dest_full_path.exists():
        return {"success": False, "message": f"目标位置已存在同名文件: {full_source.name}"}

    # 确保目标路径在工作空间内
    if not str(dest_full_path).startswith(str(workspace_path)):
        return {"success": False, "message": "目标路径超出工作空间范围"}

    new_rel_path = str(Path(destination_dir) / full_source.name)

    try:
        import shutil  # noqa: PLC0415

        shutil.move(str(full_source), str(dest_full_path))
        return {
            "success": True,
            "message": f"移动成功: {source_path} -> {new_rel_path}",
            "source_path": source_path,
            "destination_path": new_rel_path,
        }
    except Exception as e:
        logger.warning("移动文件/文件夹失败: %s | source=%s", e, source_path)
        return {"success": False, "message": f"移动失败: {e}"}


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

    # 1.5 容器路径 → 宿主机路径转换
    # 连接器（VSCode 扩展）和系统文件管理器运行在宿主机上，需要宿主机路径
    host_path = _container_to_host_path(workspace_path)

    # 2. 查找支持 open_folder 能力的活跃连接器
    registry = _get_connector_registry()
    connector = registry.get_best_connector_for("open_folder")

    if connector is None:
        # 无 IDE 连接器时，fallback 到系统文件管理器
        # 注意：_open_in_system_file_manager 在容器内运行，必须用容器路径
        opened = _open_in_system_file_manager(workspace_path)
        if opened:
            return {
                "success": True,
                "message": "已在系统文件管理器中打开工作空间",
                "path": host_path,
            }
        # 容器内无法打开文件管理器（无 explorer/xdg-open），
        # 返回宿主机路径给前端，用户可手动复制到资源管理器打开
        return {
            "success": False,
            "message": "当前没有可用的 IDE 连接器，且无法启动系统文件管理器",
            "path": host_path,
        }

    # 3. 通过连接器发送 open_folder 操作
    from connectors.types import ConnectorAction  # noqa: PLC0415

    action = ConnectorAction(
        action_type="open_folder",
        parameters={"path": host_path},
    )
    try:
        result = await connector.execute_action(action)
        if result.success:
            return {
                "success": True,
                "message": f"已在 {connector.connector_type} 中打开工作空间",
                "path": host_path,
            }
        return {
            "success": False,
            "message": f"连接器执行失败: {result.error}",
            "path": host_path,
        }
    except Exception as e:
        logger.warning("通过连接器打开工作空间失败: %s", e)
        return {
            "success": False,
            "message": f"打开工作空间失败: {e}",
            "path": host_path,
        }


def _get_project_root() -> Path:
    """获取项目根目录（本文件向上4级）。

    Returns:
        项目根目录的 Path 对象
    """
    return Path(__file__).resolve().parent.parent.parent.parent


def _container_to_host_path(container_path: str) -> str:
    """容器路径 → 宿主机路径转换。

    Agent 跑在宿主机时，工作空间路径本身就是宿主机路径（如 D:/myproject/xxx），
    直接返回原路径即可。
    """
    return container_path

    return container_path


async def _resolve_workspace_path(container_task_id: str) -> str | None:
    """从任务 metadata 中解析工作空间路径。

    通过 TaskService 获取任务实例，从其 metadata.ws_meta.path 字段提取
    工作空间路径。

    特殊处理 _local: 返回项目根目录（本文件向上4级），
    确保 fileOpener 发起的非任务文件读取能正确解析相对路径。

    Args:
        container_task_id: 容器任务 ID

    Returns:
        工作空间路径字符串，未找到时返回 None
    """
    # 特殊处理 _local 工作空间
    if container_task_id == "_local":
        return str(_get_project_root())

    try:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        provider = get_service_provider()
        task_service = provider.get_or_create(
            "task_service",
            lambda: __import__("tasks.service", fromlist=["TaskService"]).TaskService(),
        )

        task = task_service.get_task(container_task_id)
        if not task:
            return None

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
