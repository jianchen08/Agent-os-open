#!/usr/bin/env python3
"""容器隔离系统 MCP 服务端——纯接口适配层。

本目录为实现模块（平铺），本文件只做接口适配：
调用同目录实现模块，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §4.2]
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

import wsl_health
from checkpoint import CheckpointManager

# 直接导入同目录实现模块（文件就在旁边，不需要额外路径前缀）
from manager import IsolationManager, extract_providers_config
from workspace import _isolation_config_path

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.isolation_types import (
    IsolationLevel,
    OperationType,
    TaskType,
)
from agentos_plugin_sdk.permission_checker import PermissionChecker
from agentos_plugin_sdk.permission_policy import PermissionPolicyManager

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("isolation_service")

# 全局服务实例
_manager: IsolationManager | None = None
_checkpoint_mgr: CheckpointManager | None = None
_permission_checker: PermissionChecker | None = None
_config_watcher_task: asyncio.Task[Any] | None = None


def _make_state_rows_provider() -> Any:
    """构造 pipeline_state 聚合行提供者（销毁面 ``task.ws_meta`` 坐标通道）。

    经内核 ``pipeline-state`` capability 读聚合行（triggers_ext 同款形态）；
    ``task.ws_meta`` / ``lineage.parent_pipeline_id`` 在 manifest export_fields
    全局并集内（workspace_lifecycle / task_submit 出口声明），本插件直读。
    能力句柄懒解析（协程内 get_capability）；读取失败由 manager 留痕跳过。
    """

    async def _provide() -> list[dict[str, Any]]:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
        return rows if isinstance(rows, list) else []

    return _provide


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """初始化隔离管理器。"""
    global _manager, _checkpoint_mgr, _permission_checker, _config_watcher_task

    config = plugin.get_config()
    logger.info("[isolation] 加载插件，配置项数: %d", len(config))

    # 初始化隔离管理器。get_config() 含内核按 config_files 注入的
    # isolation_config.yaml 快照（providers.cua.limits 显式配额等），
    # 经 override 通路生效（config 中心在插件侧不可达，见 P1-7 DEBT）。
    _manager = IsolationManager(providers_config_override=extract_providers_config(config))
    _manager.set_state_rows_provider(_make_state_rows_provider())
    await _manager.start()

    # 配置热更新：前端设置页保存 isolation_config.yaml 后按 mtime 重建 manager，
    # 新环境创建即用新配额（存量容器由 _find_existing_container 复用找回）。
    if _config_watcher_task is None or _config_watcher_task.done():
        _config_watcher_task = asyncio.create_task(_watch_config_reload())

    # 初始化检查点管理器
    _checkpoint_mgr = CheckpointManager(project_root=os.getcwd())

    # 初始化权限检查器
    _permission_checker = PermissionChecker()

    logger.info("[isolation] 隔离服务已启动")


async def _watch_config_reload() -> None:
    """轮询 isolation_config.yaml mtime，变化时重建 IsolationManager。

    内核 PUT 配置端点直写磁盘 yaml 无通知，get_config() 是启动快照——
    轻量轮询（5s）让前端保存的配额改动无需重启插件即生效。
    """
    global _manager
    cfg_path = _isolation_config_path()
    last_mtime: float | None = None
    while True:
        try:
            mtime = cfg_path.stat().st_mtime
            if last_mtime is not None and mtime != last_mtime:
                logger.info("[isolation] 检测到隔离配置变更，重建隔离管理器 | file=%s", cfg_path)
                import yaml  # noqa: PLC0415

                raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                new_manager = IsolationManager(
                    providers_config_override=extract_providers_config({"isolation": {"isolation_config": raw}})
                )
                new_manager.set_state_rows_provider(_make_state_rows_provider())
                await new_manager.start()
                old = _manager
                _manager = new_manager
                if old:
                    await old.stop()
                last_mtime = mtime
            elif last_mtime is None:
                last_mtime = mtime
        except OSError:
            pass  # 文件暂不可读（原子写窗口），下轮再试
        await asyncio.sleep(5)


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """停止隔离管理器。"""
    global _manager, _checkpoint_mgr, _permission_checker, _config_watcher_task

    # 停配置 watcher：cancel + 收敛等待——不取消则 while True 轮询任务在
    # 卸载后仍常驻事件循环（持旧 manager 引用，纯泄漏）。
    watcher = _config_watcher_task
    _config_watcher_task = None
    if watcher is not None and not watcher.done():
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass  # cancel() 的预期收敛路径

    if _manager:
        await _manager.stop()
        _manager = None
    _checkpoint_mgr = None
    _permission_checker = None

    # 终止 WSL 保活会话（拉起者负责终止，防 sleep infinity 常驻残留）
    wsl_health.terminate_keepalive()

    logger.info("[isolation] 隔离服务已停止")


@plugin.tool(
    name="isolation.create_env",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID"},
            "task_type": {
                "type": "string",
                "enum": ["project", "module", "atomic"],
                "default": "atomic",
                "description": "任务类型",
            },
            "operation_type": {
                "type": "string",
                "description": "操作类型（如 code_execution, file_operation）",
            },
            "workspace": {"type": "string", "description": "工作目录路径"},
            "parent_workspace": {"type": "string", "description": "父任务工作目录"},
            "parent_env_id": {"type": "string", "description": "父环境 ID"},
            "isolation_level": {
                "type": "string",
                "enum": ["isolated", "non_isolated"],
                "description": "隔离级别（默认 isolated）",
            },
            "execution_profile": {
                "type": "string",
                "enum": ["linux-bash", "native-win"],
                "description": (
                    "执行档（可选，MXC 后端按 profile→backend 注册表选择；"
                    "缺省 linux-bash。非 MXC 后端忽略此参数）"
                ),
            },
        },
        "required": ["task_id"],
    },
    description="创建或获取隔离环境",
)
async def isolation_create_env(
    task_id: str,
    task_type: str = "atomic",
    operation_type: str | None = None,
    workspace: str | None = None,
    parent_workspace: str | None = None,
    parent_env_id: str | None = None,
    is_root_task: bool = True,
    isolation_level: str | None = None,
    metadata: dict[str, Any] | None = None,
    parent_task_id: str | None = None,
    tool_name: str | None = None,
    execution_profile: str | None = None,
) -> dict[str, Any]:
    """创建或复用隔离环境（同 workspace 共享容器）。

    业务失败显式 success=False（与 isolation.destroy_env 同约定：内核归一化
    只认显式 success 键，裸 {"error": …} 会被包成 success=true 信封，调用方
    按信封判定失败才不被谎报）。成功响应除基础字段外补带 exec_backend 与
    provider_error——容器获取唯一属主归位后（isolation_guard 弃私有 manager，
    改调本服务），消费方凭同一响应完成落地与 bash 传输通道注入；exec_backend
    仅原生环境有（docker 环境为 null），provider_error 仅 ERROR 占位环境非空。
    """
    if _manager is None:
        return {"success": False, "error": "隔离服务未初始化"}

    level = None
    if isolation_level:
        level = IsolationLevel(isolation_level)

    # 执行档并入 metadata（provider 读取；非 MXC 后端忽略）：profile 是环境
    # 选择参数，不是新的服务方法入参，保持 provider 契约不变。
    env_metadata = dict(metadata or {})
    if execution_profile:
        env_metadata["execution_profile"] = execution_profile

    try:
        env = await _manager.get_or_create_environment(
            task_id=task_id,
            task_type=TaskType(task_type),
            operation_type=OperationType(operation_type) if operation_type else None,
            parent_env_id=parent_env_id,
            workspace=workspace,
            parent_workspace=parent_workspace,
            is_root_task=is_root_task,
            isolation_level=level,
            metadata=env_metadata or None,
            parent_task_id=parent_task_id,
            tool_name=tool_name,
        )
    except Exception as exc:
        # 配额超限（manager 抛 RuntimeError）等创建失败：显式失败信封携原因，
        # 调用方（isolation_guard）转 blocked 而非吞成成功。
        return {"success": False, "error": str(exc)}
    info = env.provider_info if isinstance(env.provider_info, dict) else {}
    return {
        "env_id": env.env_id,
        "level": env.level.value,
        "provider_type": env.provider_type,
        "status": env.status,
        "context_task_id": env.context.task_id,
        "exec_backend": info.get("exec_backend"),
        "provider_error": info.get("error", ""),
    }


@plugin.tool(
    name="isolation.execute",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID"},
            "task_type": {
                "type": "string",
                "enum": ["project", "module", "atomic"],
                "default": "atomic",
            },
            "operation": {
                "type": "object",
                "description": "操作描述（type/command/files 等）",
            },
            "operation_type": {"type": "string"},
            "workspace": {"type": "string"},
            "parent_workspace": {"type": "string"},
        },
        "required": ["task_id", "operation"],
    },
    description="在隔离环境中执行操作",
)
async def isolation_execute(
    task_id: str,
    operation: dict[str, Any],
    task_type: str = "atomic",
    operation_type: str | None = None,
    parent_env_id: str | None = None,
    workspace: str | None = None,
    parent_workspace: str | None = None,
    is_root_task: bool = True,
    isolation_level: str | None = None,
    parent_task_id: str | None = None,
    tool_name: str | None = None,
) -> dict[str, Any]:
    """在隔离环境中执行操作（自动复用/创建环境）。"""
    if _manager is None:
        return {"success": False, "error": "隔离服务未初始化"}

    level = None
    if isolation_level:
        level = IsolationLevel(isolation_level)

    result = await _manager.execute_in_isolation(
        task_id=task_id,
        task_type=TaskType(task_type),
        operation=operation,
        operation_type=OperationType(operation_type) if operation_type else None,
        parent_env_id=parent_env_id,
        workspace=workspace,
        parent_workspace=parent_workspace,
        is_root_task=is_root_task,
        isolation_level=level,
        parent_task_id=parent_task_id,
        tool_name=tool_name,
    )
    return result.to_dict()


@plugin.tool(
    name="isolation.bind_external_env",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "绑定的任务 ID（会话面可用 session 兜底键）"},
            "workspace": {"type": "string", "description": "任务工作空间（宿主真实存在路径）"},
            "container": {"type": "string", "description": "外部容器名或 id（Harbor 任务容器）"},
            "workdir": {
                "type": "string",
                "description": "容器内工作目录（isolation.execute 默认 cwd，默认 /workspace）",
            },
        },
        "required": ["task_id", "workspace", "container"],
    },
    description="绑定外部指定容器（Terminal-Bench 通道）：此后该任务/工作空间的隔离请求收养该容器执行",
)
async def isolation_bind_external_env(
    task_id: str,
    workspace: str,
    container: str,
    workdir: str = "/workspace",
) -> dict[str, Any]:
    """登记外部容器绑定（bind 即 docker inspect 验证，不可达显式失败）。

    外部容器生命周期归调用方（Harbor）所有：销毁面只解登记，永不删除容器。
    业务失败显式 success=False（与 create_env/destroy_env 同信封约定）。
    """
    if _manager is None:
        return {"success": False, "error": "隔离服务未初始化"}
    return await _manager.bind_external_env(
        task_id=task_id, workspace=workspace, container=container, workdir=workdir
    )


@plugin.tool(
    name="isolation.query_external_binding",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID"},
            "workspace": {"type": "string", "description": "工作空间路径"},
        },
    },
    description="查询外部容器绑定（fs 工具路由判据面）：bound=True 时返回收养容器名与锚定工作目录",
)
async def isolation_query_external_binding(
    task_id: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """查询外部容器绑定（幂等只读；找不到 bound=False）。"""
    if _manager is None:
        return {"bound": False, "container_name": "", "workdir": ""}
    return _manager.lookup_external_binding(task_id=task_id, workspace=workspace)


@plugin.tool(
    name="isolation.unbind_external_env",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "任务 ID"},
            "workspace": {"type": "string", "description": "工作空间路径"},
        },
    },
    description="解除外部容器绑定（只删绑定记录，不碰容器）",
)
async def isolation_unbind_external_env(
    task_id: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """解除外部容器绑定（幂等：绑定不存在也返回成功）。"""
    if _manager is None:
        return {"success": False, "error": "隔离服务未初始化"}
    removed = _manager.unbind_external_env(task_id=task_id, workspace=workspace)
    return {"success": True, "removed": removed, "task_id": task_id or "", "workspace": workspace or ""}


@plugin.tool(
    name="isolation.destroy_env",
    schema={
        "type": "object",
        "properties": {
            "env_id": {"type": "string", "description": "环境 ID"},
            "task_id": {"type": "string", "description": "任务 ID（按任务销毁）"},
            "container_name": {
                "type": "string",
                "description": "容器名（state 真值直删，不依赖内存登记；与 task_id 同给时优先生效）",
            },
            "success": {"type": "boolean", "default": True, "description": "任务是否成功完成"},
        },
    },
    description="销毁隔离环境",
)
async def isolation_destroy_env(
    env_id: str | None = None,
    task_id: str | None = None,
    container_name: str | None = None,
    success: bool = True,
) -> dict[str, Any]:
    """销毁隔离环境（按 container_name / env_id 或 task_id）。

    container_name 是 state 真值通道（pipeline_state.isolation.container_name）：
    服务重启后内存登记为空也能按名删除，杜绝"登记丢失=销毁谎报成功=泄漏"。
    失败显式 success=False（内核归一化只认显式 success 键，裸 {"error": …}
    会被包成 success=true 信封——environment_lifecycle 按信封 error 键判定
    销毁成败，裸返会谎报 environment_released=True）。
    """
    if _manager is None:
        return {"success": False, "error": "隔离服务未初始化"}

    if container_name:
        destroyed = await _manager.destroy_environment(container_name, success=success)
        return {"destroyed": destroyed, "container_name": container_name}
    if task_id:
        await _manager.destroy_by_task_id(task_id, success=success)
        return {"destroyed": True, "task_id": task_id}
    if env_id:
        destroyed = await _manager.destroy_environment(env_id, success=success)
        return {"destroyed": True, "env_id": env_id}
    return {"success": False, "error": "必须提供 env_id、task_id 或 container_name"}


@plugin.tool(
    name="isolation.list_envs",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "按任务 ID 筛选（可选）"},
            "level": {
                "type": "string",
                "enum": ["isolated", "non_isolated"],
                "description": "按隔离级别筛选（可选）",
            },
        },
    },
    description="列出隔离环境",
)
async def isolation_list_envs(
    task_id: str | None = None,
    level: str | None = None,
) -> dict[str, Any]:
    """列出隔离环境。"""
    if _manager is None:
        return {"success": False, "error": "隔离服务未初始化"}

    env_level = None
    if level:
        env_level = IsolationLevel(level)

    envs = await _manager.list_environments(task_id=task_id, level=env_level)
    return {
        "environments": [
            {
                "env_id": e.env_id,
                "level": e.level.value,
                "provider_type": e.provider_type,
                "status": e.status,
                "task_id": e.context.task_id,
            }
            for e in envs
        ],
        "total": len(envs),
    }


@plugin.tool(
    name="isolation.check_policy",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要检查的文件路径"},
            "workspace": {"type": "string", "description": "工作目录"},
            "operation": {
                "type": "string",
                "enum": ["read", "write"],
                "description": "操作类型",
            },
        },
        "required": ["path", "operation"],
    },
    description="检查文件操作权限",
)
async def isolation_check_policy(
    path: str,
    operation: str = "read",
    workspace: str | None = None,
) -> dict[str, Any]:
    """检查文件操作是否符合权限策略。"""
    if _permission_checker is None:
        return {"error": "权限检查器未初始化"}

    # 使用默认策略（允许工作空间内的读写）
    policy = PermissionPolicyManager().get_default_policy()

    if operation == "read":
        allowed, message = _permission_checker.check_read_permission(
            path, workspace, policy
        )
    else:
        allowed, message = _permission_checker.check_write_permission(
            path, workspace, policy, operation
        )

    return {"allowed": allowed, "message": message}


@plugin.tool(
    name="isolation.checkpoint",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "restore", "cleanup", "list"],
                "description": "检查点操作",
            },
            "task_id": {"type": "string", "description": "任务 ID"},
            "workspace": {"type": "string", "description": "工作目录"},
            "files_to_backup": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要备份的文件列表",
            },
        },
        "required": ["action"],
    },
    description="检查点管理（创建/恢复/清理/列出）",
)
async def isolation_checkpoint(
    action: str,
    task_id: str | None = None,
    workspace: str | None = None,
    files_to_backup: list[str] | None = None,
) -> dict[str, Any]:
    """检查点管理操作。"""
    if _checkpoint_mgr is None:
        return {"error": "检查点管理器未初始化"}

    if action == "create":
        if not task_id or not workspace:
            return {"error": "create 需要 task_id 和 workspace"}
        # 全树扫描 + 逐文件备份与工作空间体量线性（分钟级），必须出事件循环：
        # 本 handler 是 async，同步跑在合宿宿主循环上会饿死 watchdog（审计 A-2）。
        cp = await asyncio.to_thread(
            _checkpoint_mgr.create_checkpoint, task_id, workspace, files_to_backup
        )
        return {"created": True, "checkpoint_id": cp.task_id}

    if action == "restore":
        if not task_id:
            return {"error": "restore 需要 task_id"}
        ok = await asyncio.to_thread(_checkpoint_mgr.restore_checkpoint, task_id)
        return {"restored": ok}

    if action == "cleanup":
        if not task_id:
            return {"error": "cleanup 需要 task_id"}
        ok = await asyncio.to_thread(_checkpoint_mgr.cleanup_checkpoint, task_id)
        return {"cleaned": ok}

    if action == "list":
        # 有界目录列举 + 小 manifest 读取（条数=检查点数，不随工作空间体量增长），
        # 不属 >10s 阻塞面，保持同步以零额外线程开销。
        checkpoints = _checkpoint_mgr.list_checkpoints()
        return {"checkpoints": checkpoints, "total": len(checkpoints)}

    return {"error": f"未知操作: {action}"}


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
    description="HTTP endpoint handler for /ext/isolation/external/** (Terminal-Bench 外部容器通道正门)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """外部容器通道 REST 正门（/ext/isolation/external/**）。

    POST /ext/isolation_service/external/bind    {task_id, workspace, container, workdir}
    POST /ext/isolation_service/external/unbind  {task_id?, workspace?}

    响应经 http_json 标准助手包成内核期望的 HttpHandleResponse（body base64）；
    JSON 解析失败 400，绑定失败 422，未知路由 404。供本机外部进程（Harbor
    BaseAgent 适配器）经内核 http_endpoints 动态路由调用（acp_quota_pool
    同款形态）。
    """
    del plugin_id, headers, query  # 通道无额外头/query 消费

    # 共享根注入（workspace.py 同款形态）：http_json 族经 _root_api 显式出口消费（E2 闸）
    _shared_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _shared_root not in sys.path:
        sys.path.insert(0, _shared_root)
    from _root_api import decode_body as _decode_body, error as _http_error, json_response as _json_response, ok as _ok

    if _manager is None:
        return _ok(_json_response({"error": "隔离服务未初始化"}, 503))

    route = (path, method.upper())
    if route == ("/ext/isolation_service/external/bind", "POST"):
        try:
            body = _decode_body(raw_body, strict_object=True)
        except ValueError as exc:
            return _ok(_json_response({"error": str(exc)}, 400))
        result = await _manager.bind_external_env(
            task_id=str(body.get("task_id") or ""),
            workspace=str(body.get("workspace") or ""),
            container=str(body.get("container") or ""),
            workdir=str(body.get("workdir") or "/workspace"),
        )
        if not result.get("success"):
            return _ok(_json_response({"error": result.get("error", "绑定失败")}, 422))
        return _ok(_json_response(result))

    if route == ("/ext/isolation_service/external/unbind", "POST"):
        try:
            body = _decode_body(raw_body)
        except ValueError as exc:
            return _ok(_json_response({"error": str(exc)}, 400))
        removed = _manager.unbind_external_env(
            task_id=str(body.get("task_id") or "") or None,
            workspace=str(body.get("workspace") or "") or None,
        )
        return _ok(_json_response({"success": True, "removed": removed}))

    logger.warning("http.handle: no route for path=%s method=%s", path, method)
    return _http_error(f"not found: {path} {method}", 404)


@plugin.tool(
    name="isolation.stats",
    schema={
        "type": "object",
        "properties": {},
    },
    description="获取隔离系统统计信息",
)
async def isolation_stats() -> dict[str, Any]:
    """获取隔离环境统计信息。"""
    if _manager is None:
        return {"success": False, "error": "隔离服务未初始化"}

    return _manager.get_stats()


if __name__ == "__main__":
    plugin.run()
