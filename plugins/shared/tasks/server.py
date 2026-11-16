#!/usr/bin/env python3
"""Task Service MCP 服务端——纯接口适配层。

tasks 域逻辑承载于 service.py（组合 _task_crud/_task_state/_task_cleanup
混入）与 http_api（/ext/task_service/** HTTP 面）；本文件只做接口适配，
把两者经 MCP SDK 暴露为工具与 http.handle 端点。
"""
from __future__ import annotations

import logging
import sys
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

# 共享层自举（plugins/shared/ —— project_registry / state_fields 等共享裸模块所在）。
_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

# 组根（system/）自持注入：`tasks.service` 限定导入（下方 TaskService，与
# service_access 的 M3 防误解析契约同源）的解析前提——组根不随 bootstrap
# 注入（ADR 2026-09-08-plugin-bootstrap-sink 决策 1），需用方显式注入
# （workspace/server.py 同款）。
if _paths.group_root not in sys.path:
    sys.path.insert(0, _paths.group_root)

# tasks/projects 域 HTTP 面自持。
# http_api 内部懒 import server.plugin 取能力句柄，此处顶层 import 无环。
import events as task_events  # noqa: E402,PLC0415
import http_api  # noqa: E402,PLC0415
from task_types import TaskStatus  # noqa: E402
from tasks.service import TaskService  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402
from agentos_plugin_sdk.capability import FrontendEmitter  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("task_service")

# 能力句柄正门注入：http_api._capability 在合宿模式下取不到 __main__.plugin
# （合宿 __main__ 是 host.py，无 plugin 全局），经此注入成员实例；
# 独占模式下两路径指向同一对象，行为不变。
http_api.set_plugin_instance(plugin)

_service: TaskService | None = None


def _get_service() -> TaskService:
    """获取全局 TaskService 实例，未初始化时抛出 RuntimeError。"""
    if _service is None:
        raise RuntimeError("TaskService not initialized. Was on_load called?")
    return _service


@plugin.on_domain_event
async def _on_domain_event(params: dict) -> None:
    """任务域事件入口：run.started 复位 + run 终态派生 task_completed/task_failed。

    判定与发射语义见 events.py（ADR 2026-08-28 事件下沉——任务域裁决词汇
    归本插件，内核只发 run.*）。
    """
    event_name = str(params.get("event") or "")
    if event_name == "run.started":
        # 新 run 启动复位：任务激活时历史终态残留（failed 等）复位 running。
        # 缺此复位，stop_check 聚合读面会把刚启动的新 run 立即判死（failed
        # 任务被自己的历史终态锁死，无法重新运行）。复位语义见
        # events.handle_run_started_event（用户侧终态豁免）。
        try:
            state_cap = plugin.get_capability("pipeline-state")
        except (KeyError, AttributeError):
            logger.warning("[task_service] 能力句柄缺席，跳过新 run 启动复位")
            return
        await task_events.handle_run_started_event(params, state_cap)
        return
    if event_name == "run.cancelled":
        # 停止终态随写（谁终态谁随写）：内核 run.cancelled（用户停止/任务
        # 取消/删除署名，事件名属内核 run 域词汇原样）→ 自身投影 + 父管道
        # owned 镜像同批随写 stopped（ADR 2026-10-30 词汇收敛：cancelled
        # 任务域退役，用户停止 = stopped）。与 API 停止路径
        # （http_api._write_cancel_terminal）同语义——停止面只写自身投影时，
        # 父会话视角的 owned 镜像仍停在 running。
        pipeline_id = str(params.get("pipeline_id") or "")
        if not pipeline_id:
            return
        try:
            plugin.get_capability("pipeline-state")
        except (KeyError, AttributeError):
            logger.warning("[task_service] 能力句柄缺席，跳过取消终态随写")
            return
        await http_api.follow_write_cancel_terminal(pipeline_id)
        return
    if event_name not in ("run.completed", "run.failed"):
        return
    try:
        state_cap = plugin.get_capability("pipeline-state")
        bus_cap = plugin.get_capability("event-bus")
    except (KeyError, AttributeError):
        logger.warning("[task_service] 能力句柄缺席，跳过任务域事件派生")
        return
    # WS 腿（P-19/U-1）：task_completed 经 frontend.emit 推前端；能力缺席
    # （旧内核）降级 None——只走域总线不推前端。
    frontend_emitter = FrontendEmitter.from_plugin(plugin)
    await task_events.handle_run_terminal_event(
        event_name, params, state_cap, bus_cap, frontend_emitter
    )


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize task service on load."""
    global _service
    config = plugin.get_config()
    # data_dir 仅在插件配置显式给出时覆盖（存储优先级第 1 级）；未配置时
    # 传 None，由 TaskStorage 解析剩余两级：TASKS_STORAGE_DIR env → 多租户
    # 根 data/{tenant}/tasks（storage.py __init__ 契约）。
    _service = TaskService(data_dir=config.get("data_dir"))
    # 正门实例注入 service_access：同进程 co-host 消费方
    # （child_task_guard/workspace/task_reminder 等）经 get_task_service()
    # 共享本实例，消除"正门带 config / 克隆零参"双实例化分叉。
    import service_access  # noqa: PLC0415

    service_access.set_task_service(_service)
    # 清理链跨进程能力：pipeline-executor（删任务时停/删管道数据）+
    # frontend（task_deleted 前端通知）。缺 capability 时降级留痕。
    import _task_cleanup  # noqa: PLC0415

    from agentos_plugin_sdk.capability import FrontendEmitter  # noqa: PLC0415

    async def _exec(params: dict[str, Any]) -> dict[str, Any]:
        handle = plugin.get_capability("pipeline-executor")
        return await handle.call(params["method"], params["params"])

    # from_plugin 对缺 frontend capability 内建降级（返回 None，清理链不推送），
    # 不可能外抛——无需 try/except 兜底
    _task_cleanup.set_cleanup_capabilities(_exec, FrontendEmitter.from_plugin(plugin))
    # 容器任务实体遗留数据清除（幂等；T6a 拆两半：任务行半 = 纯 TaskStorage
    # 操作内联本插件；container_* 目录半 = project.purge_legacy_dirs 服务消费，
    # 工作空间基目录解析随登记簿收口项目域主。均不再有写入方，此处只清不改）。
    _containers = {
        t.id
        for t in _service.storage.list_all()
        if (t.metadata or {}).get("task_scope") == "container"
    }
    _removed_containers = sum(1 for cid in _containers if _service.storage.delete(cid))
    _detached = 0
    for _t in _service.storage.list_all():
        if _t.parent_task_id in _containers:
            _service.storage.update(_t.id, parent_task_id=None)
            _detached += 1
    try:
        _purge_cap = plugin.get_capability("project")
        _purge_dirs = await _purge_cap.call("purge_legacy_dirs", {})
        _removed_dirs = int(_purge_dirs.get("removed_dirs") or 0)
    except Exception as exc:  # noqa: BLE001 — 目录半幂等遗留清除，服务缺席下次启动重试
        _removed_dirs = 0
        logger.warning("[task_service] container_* 目录清除服务不可用（下次启动重试）| err=%s", exc)
    purge_stats = {
        "removed_containers": _removed_containers,
        "detached_children": _detached,
        "removed_dirs": _removed_dirs,
    }
    # 启动期簿记×投影分歧对账（runs 退役收尾，ADR 2026-10-08-runs-retire-completion）：
    # 纯告警不裁决（谁终态谁随写）——发现分歧只发 WARNING，不写终态不派发事件。
    # 读面故障降级留痕（reconcile_startup 内部处理），不阻断装载。
    # 未回执补发面（ADR 2026-10-28 D2）：终态域事件在插件不在场窗口被丢弃 →
    # 装载对账重发已落定的终态事件 + 回执 + 纠正父侧 owned 脏镜像。
    import reconcile  # noqa: PLC0415

    divergences: list[dict[str, Any]] = []
    try:
        state_cap = plugin.get_capability("pipeline-state")
        runs_cap = plugin.get_capability("service-registry")
        # event-bus 能力（补发面用）：缺席不阻断对账——补发面自身降级为
        # 「只回执 + 父镜像」并留痕（既有 failed/用户停止侧补写面不受牵连）。
        try:
            bus_cap = plugin.get_capability("event-bus")
        except (KeyError, AttributeError):
            bus_cap = None
            logger.warning(
                "[task_service] event-bus 能力缺席，未回执终态补发降级（只回执 + 父镜像）"
            )
        divergences = await reconcile.reconcile_startup(state_cap, runs_cap, bus_cap)
    except KeyError as exc:
        divergences = []
        logger.warning("[task_service] 对账能力缺席，启动对账跳过 | err=%s", exc)
    replayed = sum(1 for d in divergences if d.get("kind") == "unreceipted_terminal_replay")
    logger.info(
        "TaskService initialized | legacy_purge=%s | divergences_warned=%d | "
        "terminal_replayed=%d",
        purge_stats, len(divergences) - replayed, replayed,
    )


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup task service on unload."""
    global _service
    _service = None
    import service_access  # noqa: PLC0415

    service_access.set_task_service(None)


# ──────────────────────────────────────────────
# MCP Tools
# ──────────────────────────────────────────────


@plugin.tool(
    name="task.create",
    schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Task title"},
            "description": {"type": "string", "default": "", "description": "Task description"},
            "priority": {
                "type": "integer",
                "default": 5,
                "minimum": 1,
                "maximum": 10,
                "description": "Task priority (1=highest, 10=lowest)",
            },
            "parent_task_id": {
                "type": "string",
                "description": "Parent task ID for subtask hierarchy",
            },
        },
        "required": ["title"],
    },
    description="Create a new task",
)
async def task_create(
    title: str,
    description: str = "",
    priority: int = 5,
    parent_task_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create a new task with the given title and optional parameters."""
    svc = _get_service()
    task = await svc.create_task(
        title=title,
        description=description,
        priority=priority,
        parent_task_id=parent_task_id,
        **kwargs,
    )
    return {"id": task.id, "status": task.status.value, "title": task.title}


@plugin.tool(
    name="task.get",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID"},
        },
        "required": ["task_id"],
    },
    description="Get task details by ID",
)
async def task_get(task_id: str) -> dict[str, Any] | None:
    """Retrieve a task by its ID. Returns None if not found."""
    svc = _get_service()
    task = svc.get_task(task_id)
    if task is None:
        return None
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "priority": int(task.priority),
        "parent_task_id": task.parent_task_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "result": task.result,
        "error": task.error,
    }


@plugin.tool(
    name="task.transition",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID"},
            "action": {
                "type": "string",
                "enum": ["start", "pause", "resume", "fail", "complete_evaluation"],
                "description": "Transition action to perform",
            },
            "reason": {"type": "string", "description": "Reason for fail action"},
            "passed": {
                "type": "boolean",
                "description": "Evaluation result (for complete_evaluation action)",
            },
            "result": {"type": "object", "description": "Evaluation result data"},
        },
        "required": ["task_id", "action"],
    },
    description="Perform a state transition on a task",
)
async def task_transition(
    task_id: str,
    action: str,
    reason: str | None = None,
    passed: bool = False,
    result: Any = None,
) -> dict[str, Any]:
    """Execute a state transition on a task.

    Supported actions: start, pause, resume, fail, complete_evaluation.
    """
    svc = _get_service()
    if action == "start":
        await svc.start_task(task_id)
    elif action == "pause":
        await svc.pause_task(task_id)
    elif action == "resume":
        await svc.resume_task(task_id)
    elif action == "fail":
        await svc.fail_task(task_id, reason=reason or "")
    elif action == "complete_evaluation":
        await svc.complete_evaluation(task_id, passed=passed, result=result)
    else:
        return {"ok": False, "error": f"Unknown action: {action}"}
    # 状态转换方法多为 None 返回（副作用式）——转换后回读任务取终态
    task = svc.get_task(task_id)
    if task is None:
        return {"ok": False, "error": "Task not found"}
    return {"ok": True, "status": task.status.value, "task_id": task.id}


@plugin.tool(
    name="task.list",
    schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pending", "running", "evaluating", "stopped", "completed", "failed", "timeout"],
                "description": "Filter by status. Omit to list all tasks.",
            },
            "parent_task_id": {
                "type": "string",
                "description": "Filter by parent task ID (list subtasks)",
            },
        },
    },
    description="List tasks by status or parent",
)
async def task_list(
    status: str | None = None,
    parent_task_id: str | None = None,
) -> dict[str, Any]:
    """List tasks, optionally filtered by status or parent task."""
    svc = _get_service()
    if parent_task_id:
        tasks = svc.list_subtasks(parent_task_id)
    elif status:
        tasks = svc.list_by_status(TaskStatus(status))
    else:
        tasks = await svc.list_all()

    return {
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": int(t.priority),
            }
            for t in tasks
        ],
        "total": len(tasks),
    }


@plugin.tool(
    name="task.cancel",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Root task ID to cancel"},
            "reason": {"type": "string", "description": "Cancellation reason"},
            "cascade": {
                "type": "boolean",
                "default": True,
                "description": "Cascade cancel all subtasks",
            },
        },
        "required": ["task_id"],
    },
    description="Cancel a task and optionally cascade to subtasks",
)
async def task_cancel(
    task_id: str,
    reason: str | None = None,
    cascade: bool = True,
) -> dict[str, Any]:
    """Cancel a task. If cascade=True, all subtasks are also stopped."""
    svc = _get_service()
    if cascade:
        count = await svc.cancel_task_cascade(task_id, reason=reason or "")
        return {"cancelled": count, "task_id": task_id}
    await svc.pause_task(task_id)
    return {"cancelled": 0, "task_id": task_id}


@plugin.tool(
    name="task.delete",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID to delete"},
        },
        "required": ["task_id"],
    },
    description="Delete a task (soft delete for containers, hard delete otherwise)",
)
async def task_delete(task_id: str) -> dict[str, Any]:
    """Delete a task. Container tasks are soft-deleted; others are hard-deleted."""
    svc = _get_service()
    result = await svc.delete_task(task_id)
    return {"deleted": result, "task_id": task_id}


@plugin.tool(
    name="task.get_transitions",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Task ID"},
        },
        "required": ["task_id"],
    },
    description="Get valid state transitions for a task",
)
async def task_get_transitions(task_id: str) -> dict[str, Any]:
    """Get the list of valid target states for the given task."""
    svc = _get_service()
    transitions = svc.get_valid_transitions(task_id)
    return {"transitions": transitions, "task_id": task_id}


@plugin.tool(
    name="projects.list",
    schema={"type": "object", "properties": {}},
    description=(
        "List registered projects (登记行直读：id/title/path/status/"
        "workflow_state/auto_execute；与 /ext/task_service/projects 同源)"
    ),
)
async def projects_list() -> dict[str, Any]:
    """项目登记行全量列举——project.list 服务消费（跨插件读面，T6a 收口项目域主）。"""
    handle = plugin.get_capability("project")
    result = await handle.call("list", {})
    rows = [r for r in (result.get("items") or []) if isinstance(r, dict)]
    return {"projects": rows, "total": len(rows)}


# ──────────────────────────────────────────────
# HTTP 面（/ext/task_service/**）
# ──────────────────────────────────────────────


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
    description="HTTP endpoint handler for /ext/task_service/** (tasks 21 端点 + projects 7 端点)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到 tasks/projects 域 handler（http_api 统一持有）。

    签名覆盖 HttpHandleRequest 全部字段（SDK 的 td.handler(**arguments) 展开）。
    """
    return await http_api.handle_http(path, method, raw_body, query or {}, headers)


if __name__ == "__main__":
    plugin.run()
