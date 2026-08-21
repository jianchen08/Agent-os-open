#!/usr/bin/env python3
"""工作空间服务 MCP 服务端——纯接口适配层。

老代码从 0.1 src/workspace/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §六 P2 workspace]

channel_api 拆迁批次 1（2026-08-21）：workspaces 域 11 端点侧车化——
原 channel_api/routes_workspaces.py 的 handler 搬入本插件，经
``http.handle`` 按 path 分发（协议与 agent_manager/monitoring 同款），
plugin.json ``http_endpoints`` 声明（/ext/workspace_service/workspaces/**）。
- ``_resolve_workspace_path`` 改走 tasks.service_access（M3 自包含；
  原 infrastructure.service_provider 在 0.2 已不存在，属死 import）。
- ``_get_connector_registry`` 进程内直接实例化 ConnectorRegistry 单例。
- pipeline-state state 聚合读取器在 ``_on_load`` 经 granted_capabilities
  注入（与 channel_api 原注入同一 set_state_reader 缝，回退 task_service
  只读镜像语义不变）。
[来源: docs/working/channel_api插件拆迁方案_20260821.md 批次 1]
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
_SYSTEM_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SYSTEM_DIR not in sys.path:
    sys.path.insert(0, _SYSTEM_DIR)

# 直接导入同目录老代码
from workspace_service import WorkspaceService, get_workspace_service, set_state_reader  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("workspace_service")

_service: WorkspaceService | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """初始化工作空间服务 + 注入 state 聚合读取器。"""
    global _service
    _service = get_workspace_service()
    # GAP-1 统一：注入 state 聚合读取器（父链/子链读面，channel_api on_load
    # 原注入同缝；能力未授予/未就绪时降级回退 task_service 只读镜像）。
    try:
        handle = plugin.get_capability("pipeline-state")

        async def _read_state_rows() -> list[dict[str, Any]]:
            rows = await handle.call("list", {})
            return rows if isinstance(rows, list) else []

        set_state_reader(_read_state_rows)
    except Exception as exc:  # noqa: BLE001 — 注入失败降级（回退路径）
        logger.warning("[workspace] pipeline-state 读面注入失败（回退 task_service 镜像）: %s", exc)
    logger.info("工作空间服务已加载")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """清理工作空间服务资源。"""
    global _service
    _service = None
    logger.info("工作空间服务已卸载")


# ══ http.handle 响应封装（内核 HttpHandleResponse 约定，与 agent_manager 同款）══


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


def _error(message: str, status: int = 503) -> dict[str, Any]:
    return {"success": False, "error": message, "data": _json_response({"error": message}, status)}


def _decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 或明文 JSON）为 dict。"""
    if not raw_body:
        return {}
    decoded = raw_body
    try:
        attempt = base64.b64decode(raw_body).decode("utf-8")
        if attempt.lstrip().startswith(("{", "[")):
            decoded = attempt
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        parsed = json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}


# ══ workspaces 域（channel_api 拆迁批次 1：routes_workspaces.py handler 迁入）══


def _get_project_root() -> Path:
    """获取项目根目录（本文件向上 5 级 = 仓库根）。

    channel_api 原实现按 0.1 布局（src/channels/api/）数 4 级；迁入
    plugins/shared/system/{plugin}/ 后 4 级只到 plugins/，属搬迁算术漂移
    （_local/相对路径 fallback 的解析基准错位）。前端 _local 场景传绝对路径，
    修正到仓库根不影响现网行为。
    """
    return Path(__file__).resolve().parent.parent.parent.parent.parent


_connector_registry: Any = None


def _get_connector_registry() -> Any:
    """获取全局 ConnectorRegistry 进程内单例。

    M3 自包含：直接实例化（原 ``infrastructure.service_provider`` 在 0.2 已
    删除，属死 import——channel_api 原实现每次调用必 ImportError 而走降级）。
    connectors 包内部用平铺兄弟导入（``from connector_types import ...``），
    需 connectors/ 目录本身在 sys.path 上——这里自举补入（与
    tasks/service_access 把 tasks/ 补入 sys.path 同款模式）。
    """
    global _connector_registry  # noqa: PLW0603
    if _connector_registry is None:
        _conn_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "connectors"))
        if _conn_dir not in sys.path:
            sys.path.insert(0, _conn_dir)
        from connectors.registry import ConnectorRegistry  # noqa: PLC0415

        _connector_registry = ConnectorRegistry()
    return _connector_registry


async def open_file_in_ide(body: dict[str, Any]) -> dict[str, Any]:
    """在 IDE 中打开指定文件（body: file_path/line/column）。"""
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

    from connectors.connector_types import ConnectorAction  # noqa: PLC0415

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
    except Exception as e:  # noqa: BLE001
        logger.warning("通过连接器打开文件失败: %s", e)
        return {
            "success": False,
            "message": f"打开文件失败: {e}",
            "file_path": file_path,
        }


async def get_workspace(container_task_id: str) -> dict[str, Any]:
    """获取工作空间详情（不存在时自动创建）。"""
    service = get_workspace_service()
    workspace = await service.get_or_create_workspace(container_task_id)
    return workspace.to_dict()


async def get_workspace_artifacts(container_task_id: str) -> dict[str, Any]:
    """获取工作空间下所有制品（聚合容器任务下所有子任务的制品）。"""
    service = get_workspace_service()
    return await service.list_artifacts_by_workspace(container_task_id)


async def get_file_tree(container_task_id: str) -> dict[str, Any]:
    """获取工作空间的文件目录树。"""
    workspace_path = await _resolve_workspace_path(container_task_id)
    service = get_workspace_service()
    return await service.get_file_tree(container_task_id, base_path=workspace_path)


async def get_file_content(container_task_id: str, path: str) -> dict[str, Any]:
    """读取指定文件的内容。

    优先通过 container_task_id 解析工作空间根路径（兼容文件树点击场景），
    解析失败时直接按传入的路径读取（兼容交互场景的绝对路径）。
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
    except Exception as e:  # noqa: BLE001
        logger.warning("读取文件失败: %s | path=%s", e, path)
        return {
            "success": False,
            "message": f"读取文件失败: {e}",
        }


async def save_file_content(
    container_task_id: str,
    path: str,
    body: dict[str, Any] | None = None,
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
    except Exception as e:  # noqa: BLE001
        logger.warning("保存工作空间文件失败: %s | path=%s", e, path)
        return {"success": False, "message": f"保存文件失败: {e}"}


def _validate_path_in_workspace(workspace_path: Path, rel_path: str) -> Path | None:
    """验证相对路径在工作空间范围内，防止路径穿越攻击。"""
    full_path = (workspace_path / rel_path).resolve()
    if not str(full_path).startswith(str(workspace_path)):
        return None
    return full_path


async def create_entry(container_task_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """在工作空间中创建文件或文件夹（body: path/type）。"""
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
    except Exception as e:  # noqa: BLE001
        logger.warning("创建文件/文件夹失败: %s | path=%s", e, path)
        return {"success": False, "message": f"创建失败: {e}"}


async def delete_entry(container_task_id: str, path: str) -> dict[str, Any]:
    """删除工作空间中的文件或文件夹。"""
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
    except Exception as e:  # noqa: BLE001
        logger.warning("删除文件/文件夹失败: %s | path=%s", e, path)
        return {"success": False, "message": f"删除失败: {e}"}


async def rename_entry(container_task_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """重命名工作空间中的文件或文件夹（body: old_path/new_name）。"""
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
    except Exception as e:  # noqa: BLE001
        logger.warning("重命名文件/文件夹失败: %s | old_path=%s", e, old_path)
        return {"success": False, "message": f"重命名失败: {e}"}


async def move_entry(container_task_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """移动工作空间中的文件或文件夹到指定目录（body: source_path/destination_dir）。"""
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
    except Exception as e:  # noqa: BLE001
        logger.warning("移动文件/文件夹失败: %s | source=%s", e, source_path)
        return {"success": False, "message": f"移动失败: {e}"}


async def open_workspace_in_ide(container_task_id: str) -> dict[str, Any]:
    """在 IDE 中打开指定任务的工作空间目录。"""
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
    from connectors.connector_types import ConnectorAction  # noqa: PLC0415

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
    except Exception as e:  # noqa: BLE001
        logger.warning("通过连接器打开工作空间失败: %s", e)
        return {
            "success": False,
            "message": f"打开工作空间失败: {e}",
            "path": host_path,
        }


def _container_to_host_path(container_path: str) -> str:
    """容器路径 → 宿主机路径转换。

    Agent 跑在宿主机时，工作空间路径本身就是宿主机路径（如 D:/myproject/xxx），
    直接返回原路径即可。
    """
    return container_path


async def _resolve_workspace_path(container_task_id: str) -> str | None:
    """从任务 metadata 中解析工作空间路径。

    通过 TaskService 获取任务实例，从其 metadata.ws_meta.path 字段提取
    工作空间路径（M3 自包含：改走 tasks.service_access，不再依赖 0.2 已
    删除的 infrastructure.service_provider）。

    特殊处理 _local: 返回项目根目录（本文件向上 4 级），
    确保 fileOpener 发起的非任务文件读取能正确解析相对路径。
    """
    # 特殊处理 _local 工作空间
    if container_task_id == "_local":
        return str(_get_project_root())

    try:
        import asyncio  # noqa: PLC0415

        from tasks.service_access import get_task_service  # noqa: PLC0415

        task_service = get_task_service()
        if task_service is None:
            return None

        task = await asyncio.to_thread(task_service.get_task, container_task_id)
        if not task:
            return None

        _metadata = getattr(task, "metadata", None) or {}
        _ws_meta = _metadata.get("ws_meta", {}) or {}
        return _ws_meta.get("path")

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "解析工作空间路径失败 | container_task_id=%s err=%s",
            container_task_id,
            exc,
            exc_info=True,
        )
        return None


def _open_in_system_file_manager(directory_path: str) -> bool:
    """使用系统文件管理器打开指定目录。

    Windows 使用 explorer.exe，macOS 使用 open，Linux 使用 xdg-open。
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
    except Exception as e:  # noqa: BLE001
        logger.warning("打开系统文件管理器失败: %s", e)
        return False


# ══ http.handle 分发（/ext/workspace_service/workspaces/** 入口）══

_PREFIX = "/ext/workspace_service/workspaces"


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
    description="HTTP endpoint handler for /ext/workspace_service/** (workspaces domain, channel_api 拆迁批次 1)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到 workspaces 域 11 端点（语义对齐原 /ext/channel_api/workspaces/**）。

    业务函数全 async、全 dict body；path-param {container_task_id}；
    file-content/entries 用 query(path=)。认证由 http_endpoints auth=user 声明
    （dispatcher 层），handler 不读 _user。
    """
    try:
        q = query or {}

        # POST /open-file（body: file_path/line/column）
        if path == f"{_PREFIX}/open-file" and method == "POST":
            body = _decode_body(raw_body) or {}
            return _ok(_json_response(await open_file_in_ide(body)))

        # /{container_task_id} 系列路由
        if path.startswith(_PREFIX + "/"):
            rest = path[len(_PREFIX) + 1:]  # "{id}" 或 "{id}/file-tree" 等
            if not rest or "/" not in rest:
                if rest and method == "GET":  # 单级：/{id} GET workspace
                    return _ok(_json_response(await get_workspace(rest)))
            else:
                cid, action = rest.split("/", 1)
                if action == "artifacts" and method == "GET":
                    return _ok(_json_response(await get_workspace_artifacts(cid)))
                if action == "file-tree" and method == "GET":
                    return _ok(_json_response(await get_file_tree(cid)))
                if action == "file-content" and method == "GET":
                    return _ok(_json_response(await get_file_content(
                        cid, path=q.get("path", ""),
                    )))
                if action == "file-content" and method == "PUT":
                    body = _decode_body(raw_body)
                    return _ok(_json_response(await save_file_content(
                        cid, path=q.get("path", ""), body=body,
                    )))
                if action == "create-entry" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await create_entry(cid, body)))
                if action == "entries" and method == "DELETE":
                    return _ok(_json_response(await delete_entry(
                        cid, path=q.get("path", ""),
                    )))
                if action == "rename-entry" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await rename_entry(cid, body)))
                if action == "move-entry" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await move_entry(cid, body)))
                if action == "open" and method == "POST":
                    return _ok(_json_response(await open_workspace_in_ide(cid)))

        logger.warning("workspaces http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        logger.exception("workspaces http.handle failed: %s", exc)
        return _error(f"workspace service error: {exc}", 500)


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