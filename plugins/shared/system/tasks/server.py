#!/usr/bin/env python3
"""Task Service MCP 服务端——纯接口适配层。

老代码从 0.1 src/tasks/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

[来源: docs/working/module_migration_plan.md §4.1]
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
# 共享层自举（plugins/shared/ —— project_registry 所在，与 storage.py 同模式）。
_SHARED_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

# tasks/projects 域 HTTP 面自持。
# http_api 内部懒 import server.plugin 取能力句柄，此处顶层 import 无环。
import http_api  # noqa: E402,PLC0415
from service import TaskService  # noqa: E402
from task_types import TaskStatus  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("task_service")

_service: TaskService | None = None


def _get_service() -> TaskService:
    """获取全局 TaskService 实例，未初始化时抛出 RuntimeError。"""
    if _service is None:
        raise RuntimeError("TaskService not initialized. Was on_load called?")
    return _service


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize task service on load."""
    global _service
    config = plugin.get_config()
    # data_dir 仅在插件配置显式给出时覆盖（存储优先级第 1 级）；未配置时
    # 传 None，由 TaskStorage 解析剩余两级：TASKS_STORAGE_DIR env → 多租户
    # 根 data/{tenant}/tasks（storage.py __init__ 契约）。
    _service = TaskService(data_dir=config.get("data_dir"))
    # 容器任务实体遗留数据清除（幂等；project = 文件夹+登记 模型落地后，
    # 容器任务行/挂靠引用/container_* 隔离副本不再有写入方，此处只清不改）。
    from project_registry import purge_legacy_container_data  # noqa: PLC0415

    purge_stats = purge_legacy_container_data(_service.storage)
    logger.info("TaskService initialized | legacy_purge=%s", purge_stats)


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup task service on unload."""
    global _service
    _service = None


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
