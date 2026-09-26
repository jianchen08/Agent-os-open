#!/usr/bin/env python3
"""Task Service MCP 服务端——纯接口适配层。

tasks 域逻辑承载于 service.py（组合 _task_crud/_task_state/_task_cleanup
混入）与 http_api（/ext/task_service/** HTTP 面）；本文件只做接口适配，
把两者经 MCP SDK 暴露为工具与 http.handle 端点。
"""
from __future__ import annotations

import logging
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

# 共享层自举（plugins/shared/ —— project_registry / state_fields 等共享裸模块所在）。
_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

# tasks/projects 域 HTTP 面自持。
# http_api 内部懒 import server.plugin 取能力句柄，此处顶层 import 无环。
import events as task_events  # noqa: E402,PLC0415
import http_api  # noqa: E402,PLC0415
from service import TaskService  # noqa: E402
from task_types import TaskStatus  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

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
    """任务域事件派生入口：订阅 run 终态，派生 task_completed/task_failed。

    判定与发射语义见 events.py（ADR 2026-08-28 事件下沉——任务域裁决词汇
    归本插件，内核只发 run.*）。
    """
    event_name = str(params.get("event") or "")
    if event_name not in ("run.completed", "run.failed"):
        return
    try:
        state_cap = plugin.get_capability("pipeline-state")
        bus_cap = plugin.get_capability("event-bus")
    except (KeyError, AttributeError):
        logger.warning("[task_service] 能力句柄缺席，跳过任务域事件派生")
        return
    await task_events.handle_run_terminal_event(event_name, params, state_cap, bus_cap)


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
    # 容器任务实体遗留数据清除（幂等；project = 文件夹+登记 模型落地后，
    # 容器任务行/挂靠引用/container_* 隔离副本不再有写入方，此处只清不改）。
    from project_registry import purge_legacy_container_data  # noqa: PLC0415

    purge_stats = purge_legacy_container_data(_service.storage)
    # U13 启动调和（域界定 docs/working/U13终态两写域界定_20260906.md §二）：
    # 内核 reap 把残留 running 一律扫 failed，与投影 completed 冲突的分歧行由
    # 任务域按投影仲裁（completed 权威补派通知 / 无终态证据对齐 failed）。
    # 读面故障降级留痕（reconcile_startup 内部处理），不阻断装载。
    import reconcile  # noqa: PLC0415

    try:
        state_cap = plugin.get_capability("pipeline-state")
        runs_cap = plugin.get_capability("service-registry")
        bus_cap = plugin.get_capability("event-bus")
        reconciled = await reconcile.reconcile_startup(state_cap, runs_cap, bus_cap)
    except KeyError as exc:
        reconciled = []
        logger.warning("[task_service] 调和能力缺席，启动调和跳过 | err=%s", exc)
    logger.info(
        "TaskService initialized | legacy_purge=%s | orphan_reconciled=%d",
        purge_stats, len(reconciled),
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
    """Cancel a task. If cascade=True, all subtasks are also cancelled."""
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
