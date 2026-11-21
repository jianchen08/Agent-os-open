#!/usr/bin/env python3
"""工作空间服务 MCP 服务端——纯接口适配层。

老代码从 0.1 src/workspace/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §六 P2 workspace]

workspaces 域 11 端点经 ``http.handle`` 按 path 分发（协议与
agent_manager/monitoring 同款），plugin.json ``http_endpoints`` 声明
（/ext/workspace_service/workspaces/**）。
- 工作区坐标解析收敛为 state 单一真相源（用户裁定 2026-10-08）：任务/会话
  管道 id 走 ``_resolve_workspace_path``（state 聚合行直读 + U6 归属闸）；
  ``_local`` 与项目登记为显式独立入口，经 ``_resolve_container_path`` 按
  id 类别路由，互不回退。
- 打开文件/工作空间为宿主原生直开（用户裁定 2026-10-08）：Windows
  ``os.startfile``（explorer/默认程序接管），其余平台 ``webbrowser.open``
  file URI；失败面 = 路径不存在/OS 调用异常，如实报 ``success: false``。
- pipeline-state state 聚合读取器在 ``_on_load``
  注入（读面缺失 = 无坐标，如实 no_workspace，无兜底通道）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根（http_json）入 sys.path

# system/ 根：本文件懒加载的 `from tasks.service_access import …`（tasks 包）
# 解析前提——组根不随 bootstrap 注入（ADR 2026-09-08-plugin-bootstrap-sink 决策 1）
if _paths.group_root not in sys.path:
    sys.path.insert(0, _paths.group_root)

from _root_api import (  # noqa: E402
    decode_body as _decode_body,
    decode_kernel_token as _decode_kernel_token,  # noqa: E402
    error as _error,
    json_response as _json_response,
    ok as _ok,
)

# 同目录平铺实现模块（workspace_service 等）
from workspace_service import (  # noqa: E402
    WorkspaceService,
    get_workspace_service,
    set_state_reader,
)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("workspace_service")

_service: WorkspaceService | None = None


# ══ caller 身份解析与归属闸（U6；tasks/agent_manager 同款 token 解析） ══


class WorkspaceAccessDenied(Exception):
    """工作空间归属闸拒绝（HTTP 404）。

    触发：caller 未认证 / 与目标任务归属不一致 / 目标缺归属元数据
    （fail-closed）。message 直接面向 HTTP 响应体。
    """


# 防存在性探测统一话术：未认证与归属不一致不可区分，也不泄露目标是否存在。
_DENIED_MESSAGE = "工作空间不存在或无权访问"
# 缺归属元数据是数据完整性问题而非保密问题：按仓规 fail-closed 拒绝并说明原因。
_MISSING_OWNER_MESSAGE = (
    "目标任务归属元数据缺失（task.submitted_by），拒绝访问；存量数据需回填归属后可达"
)


def _resolve_caller(headers: dict[str, str] | None) -> dict[str, Any]:
    """从请求头解析可信 caller 身份（sub/username）。无/无效 token → {}。"""
    authz = ""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "authorization" and v:
            authz = str(v)
            break
    token = authz[7:] if authz.lower().startswith("bearer ") else ""
    if not token:
        return {}
    decoded = _decode_kernel_token(token)
    if decoded is None:
        return {}
    user_id, username, exp = decoded
    if int(time.time()) >= exp:
        return {}
    return {"sub": user_id, "username": username}


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """初始化工作空间服务 + 注入 state 聚合读取器。"""
    global _service
    _service = get_workspace_service()
    # GAP-1 统一：注入 state 聚合读取器（工作区坐标唯一解析通道；读面缺失
    # = 无坐标，如实 no_workspace，无兜底）。
    try:
        handle = plugin.get_capability("pipeline-state")

        async def _read_state_rows() -> list[dict[str, Any]]:
            rows = await handle.call("list", {})
            return rows if isinstance(rows, list) else []

        set_state_reader(_read_state_rows)
    except Exception as exc:  # noqa: BLE001 — 注入失败降级（无读面即无坐标）
        logger.warning("[workspace] pipeline-state 读面注入失败（无坐标解析能力）: %s", exc)
    logger.info("工作空间服务已加载")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """清理工作空间服务资源。"""
    global _service
    _service = None
    logger.info("工作空间服务已卸载")


# ══ http.handle 响应封装：公共实现 plugins/shared/http_json.py（文件头已导入）══


# ══ workspaces 域 ══


def _get_project_root() -> Path:
    """获取项目根目录（本文件向上 4 级 = 仓库根）。

    按本文件相对仓库根的固定深度数 4 级（plugins/shared/{plugin}/ 拍平布局
    在仓库根下 4 层，ADR 2026-10-13-plugin-flat-layout）；前端 _local 场景传
    绝对路径，修正到仓库根不影响现网行为。
    """
    return Path(__file__).resolve().parent.parent.parent.parent


async def open_file_in_ide(body: dict[str, Any]) -> dict[str, Any]:
    """用宿主默认程序打开指定文件（body: file_path）。"""
    file_path = body.get("file_path", "")
    if not file_path:
        return {
            "success": False,
            "message": "file_path 参数不能为空",
            "file_path": None,
        }

    if _open_on_host(file_path):
        return {
            "success": True,
            "message": f"已打开文件: {file_path}",
            "file_path": file_path,
        }
    return {
        "success": False,
        "message": f"打开文件失败（路径不存在或系统调用异常）: {file_path}",
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


# ══ fs-tree：多端目录树索引（任意绝对目录单层列举）══

# 单层条目数封顶（与 WorkspaceService._LAYER_MAX_ENTRIES 同口径：防御性预算，
# node_modules 等巨目录单层也可能撑爆响应体）
_FS_LAYER_MAX_ENTRIES = 500


def _fs_root_layer() -> list[dict[str, Any]]:
    """根层条目 = 本地盘（Windows 存在盘符枚举 / POSIX 单根 ``/``）。

    设备分支不特判：挂载锚是盘根下以设备名命名的目录（R26b 定版：设备名
    一层根下直挂），下一层单层列举自然呈现——树即文件系统，无设备专用枚举。
    """
    if sys.platform == "win32":
        import string

        return [
            {"name": f"{letter}:\\", "path": f"{letter}:\\", "type": "directory"}
            for letter in string.ascii_uppercase
            if Path(f"{letter}:\\").exists()
        ]
    return [{"name": "/", "path": "/", "type": "directory"}]


def _fs_list_layer(target: str) -> dict[str, Any]:
    """列出任意绝对目录的一层子项（与工作区文件树同口径）。

    同口径三件：单层封顶 truncated 标记、点前缀/``__pycache__`` 隐藏条目过滤、
    类型判定复用 ``WorkspaceService._entry_type``（链接优先于目录——junction/
    symlink 是设备分支的预期形态，isdir 会穿透链接，判定次序不可换）。
    目录不可读 = 业务级错误信封（组件渲染在层错误位），不伪装空目录。
    """
    try:
        names = sorted(os.listdir(target))  # noqa: PTH208
    except (PermissionError, OSError) as exc:
        logger.warning("[workspace] 目录不可读: %s | %s", target, exc)
        return {
            "entries": [],
            "truncated": False,
            "workspace_status": "fs_error",
            "error": f"目录不可读: {target}",
        }

    entry_type = WorkspaceService._entry_type
    entries: list[dict[str, Any]] = []
    truncated = False
    for name in names:
        if name == "__pycache__" or name.startswith("."):
            continue
        if len(entries) >= _FS_LAYER_MAX_ENTRIES:
            truncated = True
            break
        full = os.path.join(target, name)
        entries.append({"name": name, "type": entry_type(full), "path": full})
    return {"entries": entries, "truncated": truncated}


def _fs_error(message: str) -> dict[str, Any]:
    """业务级错误信封：``workspace_status=fs_error``（200 信封内携带，前端
    FileTreeWidget 据此渲染诚实错误态——与工作区文件树同字段契约）。"""
    return {"entries": [], "truncated": False, "workspace_status": "fs_error", "error": message}


async def fs_tree(path: str = "") -> dict[str, Any]:
    """多端目录树索引（单层列举，前端 fsbrowse:// 懒加载供数）。

    ``path`` 空 = 根层（本地盘枚举）；非空 = 该**绝对目录**的一层子项。目录
    来源可插拔性承载在前端 dataSource ref（fsbrowse://），本端点只按路径
    列举：本地盘与设备分支挂载锚（如 ``C:\\VM-0-16-ubuntu``，WinFsp 挂载
    读写全通）同一路径语言，无设备专用语义。
    """
    target = (path or "").strip()
    if not target:
        return {"entries": _fs_root_layer(), "truncated": False}
    candidate = Path(target)
    if not candidate.is_absolute():
        return _fs_error(f"仅接受绝对目录: {target}")
    if not candidate.is_dir():
        return _fs_error(f"目录不存在: {target}")
    return _fs_list_layer(str(candidate))


async def get_file_tree(
    container_task_id: str, path: str = "", caller: dict[str, Any] | None = None
) -> dict[str, Any]:
    """列出工作空间文件树指定层级（单层列举，懒加载文件树供数）。

    ``path`` 为自工作区根起的相对路径（posix，缺省根层），越界/非法由服务层
    词法归一拒绝（error 信封）。``workspace_status`` 业务状态字段（200 信封内
    携带，前端据此渲染可读错误而非"暂无数据"空态）：``no_workspace``（无坐
    标）/ ``dir_missing``（坐标在目录不在）；正常时字段缺省。归属闸见
    _resolve_workspace_path（经 _resolve_container_path 路由）。
    """
    workspace_path = await _resolve_container_path(container_task_id, caller)
    if not workspace_path:
        return {
            "entries": [],
            "truncated": False,
            "workspace_status": "no_workspace",
            "error": f"{container_task_id[:8]} 无工作区坐标（任务未分配工作区，或为主会话管道）",
        }
    if not Path(workspace_path).is_dir():
        return {
            "entries": [],
            "truncated": False,
            "workspace_status": "dir_missing",
            "error": f"工作区目录不存在: {workspace_path}",
        }
    service = get_workspace_service()
    return await service.get_file_tree(container_task_id, base_path=workspace_path, sub_path=path)


def _read_to_end(full_path: Path, start: int) -> str:
    """自 start 字节位读到文件尾并解码（纯同步体，由 ``to_thread`` 承载）。

    与整文件读同属无上限读（打开面不限大小），多语句读体独立成函数以便整体
    出事件循环。
    """
    with open(full_path, "rb") as f:  # noqa: PTH123
        f.seek(start)
        chunk = f.read()
    return chunk.decode("utf-8", errors="replace")


async def get_file_content(
    container_task_id: str,
    path: str,
    caller: dict[str, Any] | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """读取指定文件的内容（打开面：无路径/大小限制 + 按需分页）。

    优先通过 container_task_id 解析工作空间根路径（兼容文件树点击场景），
    解析失败时直接按传入的路径读取（兼容交互场景的绝对路径）。

    路径形态裁定（用户裁定 2026-09-28 + 2026-10-24 延伸，ADR
    2026-09-28-file-content-absolute-direct-read）：绝对路径直读；相对路径
    锚定工作空间/项目根——锚定仅用于**定位**，不做越界拒绝。本端点消费方
    是经内核 token 鉴权的前端用户**主动打开面**（能定位到即打开，用户裁定
    "不通过路径限制用户的访问"），agent 文件读链走 fs_tools 自有守卫，不经
    此端点；写面（save_file_content）仍保留越界守卫。

    按需加载（用户裁定 2026-10-24）：offset/limit 为字节窗口，limit 缺省=
    自 offset 读到文件尾；返回 loaded（窗口末字节位）与 has_more，供前端
    「加载更多/加载全部」分页。不设大小上限（打开面不限；写面仍有 10MB
    保护，防部分加载内容写回截断由前端禁编辑兜底）。
    """
    workspace_path_str = await _resolve_container_path(container_task_id, caller)
    raw_path = Path(path)
    project_root = _get_project_root()

    if raw_path.is_absolute():
        full_path = raw_path.resolve()
    else:
        anchor = Path(workspace_path_str).resolve() if workspace_path_str else project_root
        full_path = (anchor / path).resolve()

    if not full_path.is_file():
        # 已合并 worktree 的死路径重定位：任务过程产出的 worktree 绝对路径
        # （文件卡片等）在合并清理后失效，按 state 的 worktree 元数据映射回
        # project_root 读取；返回体仍携带原请求路径，前端契约不变。
        remapped = await get_workspace_service().resolve_merged_worktree_target(
            str(full_path)
        )
        if remapped and Path(remapped).is_file():
            logger.info("已合并 worktree 路径重定位 | %s -> %s", full_path, remapped)
            full_path = Path(remapped)

    if not full_path.is_file():
        return {
            "success": False,
            "message": f"文件不存在或不是普通文件: {path}",
        }

    file_size = full_path.stat().st_size
    start = max(0, offset)
    try:
        if limit is None:
            if start == 0:
                # 整文件读无上限（打开面不限大小），大文件解码秒级——出事件循环
                # 免饿死合宿宿主（审计 P2-2）；异常语义由同层 except 原样承接。
                content = await asyncio.to_thread(
                    full_path.read_text, encoding="utf-8", errors="replace"
                )
            else:
                # 同属无上限读（自 offset 读到文件尾），同样出事件循环。
                content = await asyncio.to_thread(_read_to_end, full_path, start)
            loaded = file_size
        else:
            with open(full_path, "rb") as f:  # noqa: PTH123
                f.seek(start)
                chunk = f.read(limit)
            content = chunk.decode("utf-8", errors="replace")
            loaded = start + len(chunk)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取文件失败: %s | path=%s", e, path)
        return {
            "success": False,
            "message": f"读取文件失败: {e}",
        }

    return {
        "success": True,
        "content": content,
        "path": path,
        "size": file_size,
        "loaded": loaded,
        "has_more": loaded < file_size,
    }


async def save_file_content(
    container_task_id: str,
    path: str,
    body: dict[str, Any] | None = None,
    caller: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """保存文件内容到工作空间。"""
    if body is None:
        body = {}
    content = body.get("content", "")
    workspace_path_str = await _resolve_container_path(container_task_id, caller)
    if not workspace_path_str:
        return {"success": False, "message": "未找到工作空间路径"}

    workspace_path = Path(workspace_path_str).resolve()
    full_path = (workspace_path / path).resolve()
    # is_relative_to 要求真实路径包含关系：字符串前缀比较会把兄弟目录
    # （workspace=…/ws 时 …/ws-backup）误判为界内
    if not full_path.is_relative_to(workspace_path):
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
    # is_relative_to 防穿越：字符串前缀比较放行兄弟目录（…/ws vs …/ws-backup）
    if not full_path.is_relative_to(workspace_path):
        return None
    return full_path


async def create_entry(
    container_task_id: str, body: dict[str, Any], caller: dict[str, Any] | None = None
) -> dict[str, Any]:
    """在工作空间中创建文件或文件夹（body: path/type）。"""
    path = body.get("path", "")
    entry_type = body.get("type", "")

    if not path:
        return {"success": False, "message": "path 参数不能为空"}

    if entry_type not in ("file", "directory"):
        return {"success": False, "message": "type 参数必须为 file 或 directory"}

    workspace_path_str = await _resolve_container_path(container_task_id, caller)
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


async def delete_entry(
    container_task_id: str, path: str, caller: dict[str, Any] | None = None
) -> dict[str, Any]:
    """删除工作空间中的文件或文件夹。"""
    if not path:
        return {"success": False, "message": "path 参数不能为空"}

    workspace_path_str = await _resolve_container_path(container_task_id, caller)
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

            # 删大目录（node_modules/构建产物）十秒~分钟级，必须出事件循环
            # 免饿死合宿宿主（审计 A-7）；异常语义由同层 except 原样承接。
            await asyncio.to_thread(shutil.rmtree, full_path)
        else:
            full_path.unlink()

        return {"success": True, "message": f"删除成功: {path}"}
    except Exception as e:  # noqa: BLE001
        logger.warning("删除文件/文件夹失败: %s | path=%s", e, path)
        return {"success": False, "message": f"删除失败: {e}"}


async def rename_entry(
    container_task_id: str, body: dict[str, Any], caller: dict[str, Any] | None = None
) -> dict[str, Any]:
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

    workspace_path_str = await _resolve_container_path(container_task_id, caller)
    if not workspace_path_str:
        return {"success": False, "message": "未找到工作空间路径"}

    workspace_path = Path(workspace_path_str).resolve()
    full_old_path = _validate_path_in_workspace(workspace_path, old_path)
    if full_old_path is None:
        return {"success": False, "message": "路径超出工作空间范围"}

    if not full_old_path.exists():
        return {"success": False, "message": f"路径不存在: {old_path}"}

    # 计算新路径：在同一个目录下替换文件/目录名
    full_new_path = (full_old_path.parent / new_name).resolve()
    # 确保新路径也在工作空间范围内（resolve 后再比较——is_relative_to
    # 判真实包含关系，new_name 为 `..` 等单段特殊名时同样被拦）
    if not full_new_path.is_relative_to(workspace_path):
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


async def move_entry(
    container_task_id: str, body: dict[str, Any], caller: dict[str, Any] | None = None
) -> dict[str, Any]:
    """移动工作空间中的文件或文件夹到指定目录（body: source_path/destination_dir）。"""
    source_path = body.get("source_path", "")
    destination_dir = body.get("destination_dir", "")

    if not source_path:
        return {"success": False, "message": "source_path 参数不能为空"}
    if not destination_dir:
        return {"success": False, "message": "destination_dir 参数不能为空"}

    workspace_path_str = await _resolve_container_path(container_task_id, caller)
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

    # 确保目标路径在工作空间内（is_relative_to 判真实包含关系）
    if not dest_full_path.is_relative_to(workspace_path):
        return {"success": False, "message": "目标路径超出工作空间范围"}

    new_rel_path = str(Path(destination_dir) / full_source.name)

    try:
        import shutil  # noqa: PLC0415

        # 目录移动跨卷时是「整树复制 + 删除」（分钟级，与 A-9 move 同族），
        # 出事件循环免饿死合宿宿主。
        await asyncio.to_thread(shutil.move, str(full_source), str(dest_full_path))
        return {
            "success": True,
            "message": f"移动成功: {source_path} -> {new_rel_path}",
            "source_path": source_path,
            "destination_path": new_rel_path,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("移动文件/文件夹失败: %s | source=%s", e, source_path)
        return {"success": False, "message": f"移动失败: {e}"}


async def open_workspace_in_ide(
    container_task_id: str, caller: dict[str, Any] | None = None
) -> dict[str, Any]:
    """在宿主中打开指定任务的工作空间目录。"""
    # 1. 从任务 metadata 中获取工作空间路径
    workspace_path = await _resolve_container_path(container_task_id, caller)
    if not workspace_path:
        logger.warning(
            "打开工作空间失败：无工作区坐标 | container_task_id=%s",
            container_task_id,
        )
        return {
            "success": False,
            "message": f"未找到任务 {container_task_id[:8]} 的工作空间路径，请确认任务已分配工作空间",
            "path": None,
        }

    # 2. 宿主原生直开（explorer/默认程序接管；失败面 = 路径不存在/OS 调用
    # 异常，WARNING 留痕与用户提示分离）
    host_path = _container_to_host_path(workspace_path)
    if not _open_on_host(host_path):
        logger.warning(
            "打开工作空间失败 | container_task_id=%s | path=%s",
            container_task_id,
            host_path,
        )
        return {
            "success": False,
            "message": f"打开工作空间失败（路径不存在或系统调用异常）: {host_path}",
            "path": host_path,
        }
    return {
        "success": True,
        "message": "已在系统中打开工作空间",
        "path": host_path,
    }


def _container_to_host_path(container_path: str) -> str:
    """容器路径 → 宿主机路径转换。

    Agent 跑在宿主机时，工作空间路径本身就是宿主机路径（如 D:/myproject/xxx），
    直接返回原路径即可。
    """
    return container_path


def _require_sub(caller: dict[str, Any] | None) -> str:
    """归属闸前置：caller 未认证 → WorkspaceAccessDenied（404，防存在性探测）。"""
    sub = str((caller or {}).get("sub") or "")
    if not sub:
        raise WorkspaceAccessDenied(_DENIED_MESSAGE)
    return sub


def _resolve_local_workspace_path(caller: dict[str, Any] | None = None) -> str:
    """``_local`` 显式入口：部署级共享本地工作区 = 项目根。

    独立实体通道（不混入任务工作区解析）：前端 fileOpener 兜底、交互面板
    文件读取以 ``_local`` 直指项目根；无单任务归属，认证 caller 即可达。
    """
    _require_sub(caller)
    return str(_get_project_root())


async def _resolve_project_workspace_path(
    project_id: str, caller: dict[str, Any] | None = None
) -> str | None:
    """项目登记显式入口：projectId 经 project.list_paths 服务解析项目文件夹（T6a）。

    独立实体通道（不混入任务工作区解析）：项目行"打开文件夹"/项目工作区
    标签以裸登记 projectId 调 workspaces open / file-tree 端点。归属闸按
    登记行 submitted_by（project_create 落账，project.get 服务读出），行缺归属
    fail-closed；未登记 / 服务故障 → None（非项目实体，路由层落任务/会话
    state 通道），归属拒绝上抛不吞。
    """
    sub = _require_sub(caller)
    try:
        handle = plugin.get_capability("project")
        paths = (await handle.call("list_paths", {})).get("paths") or {}
        project_path = paths.get(project_id) if isinstance(paths, dict) else None
        if project_path:
            await _assert_project_ownership(project_id, sub)
            return str(project_path)
        return None
    except WorkspaceAccessDenied:
        raise
    except Exception as exc:  # noqa: BLE001 — 登记故障按非项目实体处理，不阻断后续解析
        logger.warning("项目登记通道解析失败 | project_id=%s err=%s", project_id, exc)
        return None


async def _resolve_workspace_path(
    container_task_id: str, caller: dict[str, Any] | None = None
) -> str | None:
    """任务/会话工作区解析——state 单一真相源直读，并施加归属闸（U6）。

    四通道兜底链已收敛为单通道（用户裁定 2026-10-08）：``_local`` 与项目
    登记剥离为显式独立入口（_resolve_local_workspace_path /
    _resolve_project_workspace_path，经 _resolve_container_path 按 id 类别
    路由）；task_service 任务镜像通道删除——state 行"任务 init 即写"是契约
    （workspace_lifecycle init 步经 pipeline-state.update 落 task.ws_meta），
    镜像属双写歧路。行未命中 / 行内无工作区键 → None（无工作区坐标，上层
    如实报 no_workspace，无兜底）。

    归属闸：请求方与目标任务归属不一致 → WorkspaceAccessDenied（404，防
    存在性探测）；任务域目标缺归属元数据 fail-closed 拒绝并说明原因（存量
    回填属运行时操作）。任务行（task.id / task.submitted_by / task.ws_meta
    任一在场）按行 ``task.submitted_by``（task_submit 出生协议写全）；会话域
    行（无任何任务身份标记）坐标即服务端 state 真值，认证 caller 可达（R3
    会话域 pipelineId 解析通道）。路径与归属同行读出（单次读取，避免二次
    读行归属错位）。
    """
    sub = _require_sub(caller)
    state_path, owner, is_task_row = await get_workspace_service().resolve_workspace_from_state(
        container_task_id
    )
    if not state_path:
        return None
    if is_task_row:
        if not owner:
            raise WorkspaceAccessDenied(_MISSING_OWNER_MESSAGE)
        if owner != sub:
            raise WorkspaceAccessDenied(_DENIED_MESSAGE)
    return state_path


async def _resolve_container_path(
    container_task_id: str, caller: dict[str, Any] | None = None
) -> str | None:
    """端点 container id → 工作区路径的显式路由（认证闸单点）。

    三类 id 各走独立解析入口，互不回退：``_local`` → _resolve_local_workspace_path；
    项目登记命中 → _resolve_project_workspace_path（归属拒绝上抛，不落
    state 通道）；其余（任务/会话管道 id）→ _resolve_workspace_path（state
    单一真相源直读）。全路由未命中维持既有 None（无工作区坐标）语义。
    """
    _require_sub(caller)
    if container_task_id == "_local":
        return _resolve_local_workspace_path(caller)
    project_path = await _resolve_project_workspace_path(container_task_id, caller)
    if project_path is not None:
        return project_path
    return await _resolve_workspace_path(container_task_id, caller)


async def _assert_project_ownership(project_id: str, sub: str) -> None:
    """项目登记通道归属闸：登记行 submitted_by（project_create 落账，经
    project.get 服务读出，T6a 起不再跨插件 import tasks 登记单例）与请求方
    比对；行缺归属 → fail-closed。"""
    handle = plugin.get_capability("project")
    row = await handle.call("get", {"project_id": project_id})
    project = row.get("project") if isinstance(row, dict) and row.get("found") else None
    owner = str(project.get("submitted_by") or "") if isinstance(project, dict) else ""
    if not owner:
        raise WorkspaceAccessDenied(_MISSING_OWNER_MESSAGE)
    if owner != sub:
        raise WorkspaceAccessDenied(_DENIED_MESSAGE)


def _open_on_host(path: str) -> bool:
    """宿主原生直开路径（目录与文件同 API）。

    Windows 用 ``os.startfile``（explorer/默认程序接管）；其余平台回退
    ``webbrowser.open``（file URI）。宿主判定按 ``sys.platform``。
    路径不存在或 OS 调用异常 → False（调用方如实向用户报失败）。
    """
    try:
        resolved = Path(path).resolve()
        if not resolved.exists():
            logger.warning("路径不存在，无法打开: %s", resolved)
            return False

        if sys.platform == "win32":
            os.startfile(str(resolved))  # type: ignore[attr-defined]
        else:
            webbrowser.open(resolved.as_uri())

        logger.info("已在宿主打开: %s", resolved)
        return True
    except Exception as e:  # noqa: BLE001 — OS 调用失败如实转 False
        logger.warning("宿主打开失败: %s | path=%s", e, path)
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
    description="HTTP endpoint handler for /ext/workspace_service/** (workspaces domain)",
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
    file-content/entries 用 query(path=)。端点鉴权由 http_endpoints auth=user
    声明（dispatcher 层）；handler 另从 Authorization 头解析 caller 身份供
    工作区解析路由与归属闸（_resolve_container_path，U6 垂直隔离）消费。
    """
    try:
        caller = _resolve_caller(headers)
        q = query or {}

        # POST /open-file（body: file_path/line/column）
        if path == f"{_PREFIX}/open-file" and method == "POST":
            body = _decode_body(raw_body) or {}
            return _ok(_json_response(await open_file_in_ide(body)))

        # GET /fs-tree（query: path=，空=根层）——多端目录树索引
        # （须先于 /{container_task_id} 系列路由：单段 "fs-tree" 会被当 id 吞掉）

        # GET /fs-tree（query: path=，空=根层）——多端目录树索引
        # （须先于 /{container_task_id} 系列路由：单段 "fs-tree" 会被当 id 吞掉）
        if path == f"{_PREFIX}/fs-tree" and method == "GET":
            return _ok(_json_response(await fs_tree(path=q.get("path", ""))))

        # /{container_task_id} 系列路由
        if path.startswith(_PREFIX + "/"):
            rest = path[len(_PREFIX) + 1 :]  # "{id}" 或 "{id}/file-tree" 等
            if not rest or "/" not in rest:
                if rest and method == "GET":  # 单级：/{id} GET workspace
                    return _ok(_json_response(await get_workspace(rest)))
            else:
                cid, action = rest.split("/", 1)
                if action == "artifacts" and method == "GET":
                    return _ok(_json_response(await get_workspace_artifacts(cid)))
                if action == "file-tree" and method == "GET":
                    return _ok(
                        _json_response(await get_file_tree(cid, path=q.get("path", ""), caller=caller))
                    )
                if action == "file-content" and method == "GET":
                    # 分页参数（字节窗口，用户裁定 2026-10-24 按需加载）：
                    # 非法值按缺省容错（offset=0 / limit=读到尾），不 400
                    try:
                        req_offset = int(q.get("offset", "0"))
                    except ValueError:
                        req_offset = 0
                    try:
                        req_limit = int(q["limit"]) if "limit" in q else None
                    except ValueError:
                        req_limit = None
                    return _ok(
                        _json_response(
                            await get_file_content(
                                cid,
                                path=q.get("path", ""),
                                caller=caller,
                                offset=req_offset,
                                limit=req_limit,
                            )
                        )
                    )
                if action == "file-content" and method == "PUT":
                    body = _decode_body(raw_body)
                    return _ok(
                        _json_response(
                            await save_file_content(
                                cid,
                                path=q.get("path", ""),
                                body=body,
                                caller=caller,
                            )
                        )
                    )
                if action == "create-entry" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await create_entry(cid, body, caller)))
                if action == "entries" and method == "DELETE":
                    return _ok(
                        _json_response(
                            await delete_entry(
                                cid,
                                path=q.get("path", ""),
                                caller=caller,
                            )
                        )
                    )
                if action == "rename-entry" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await rename_entry(cid, body, caller)))
                if action == "move-entry" and method == "POST":
                    body = _decode_body(raw_body) or {}
                    return _ok(_json_response(await move_entry(cid, body, caller)))
                if action == "open" and method == "POST":
                    return _ok(_json_response(await open_workspace_in_ide(cid, caller)))

        logger.warning("workspaces http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except WorkspaceAccessDenied as exc:
        return _ok(_json_response({"error": str(exc)}, 404))
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
                "description": "基础路径（可选，用于列举真实文件目录）",
            },
            "path": {
                "type": "string",
                "description": "工作区内相对路径（posix，缺省根层；单层列举不递归）",
            },
        },
        "required": ["container_task_id"],
    },
    description="List one directory level of a workspace (lazy-loading file tree)",
)
async def workspace_get_file_tree(
    container_task_id: str,
    base_path: str | None = None,
    path: str = "",
) -> dict[str, Any]:
    """列出工作空间文件树指定层级。"""
    if _service is None:
        return {"success": False, "error": "服务未初始化"}

    if base_path is not None:
        result = await _service.get_file_tree(
            container_task_id=container_task_id,
            base_path=base_path,
            sub_path=path,
        )
        return {
            "success": True,
            "entries": result.get("entries", []),
            "truncated": result.get("truncated", False),
        }
    # 无显式 base_path：走解析通道（无工作区/目录缺失如实失败，不返回假空树）
    result = await get_file_tree(container_task_id, path=path)
    if result.get("workspace_status"):
        return {
            "success": False,
            "error": result.get("error", ""),
            "workspace_status": result["workspace_status"],
        }
    return {
        "success": True,
        "entries": result.get("entries", []),
        "truncated": result.get("truncated", False),
    }


if __name__ == "__main__":
    plugin.run()
