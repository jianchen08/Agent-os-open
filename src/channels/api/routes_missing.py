"""缺失路由补全模块。

提供前端期望但后端未实现的路由组，返回合理的占位响应。
包括：projects, users, monitoring, triggers, interaction,
agent-calls, execution/records, sessions, knowledge-base,
floating-chat, cost-control, evaluation, evaluation-metrics别名,
files/capabilities。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from channels.api.deps import require_auth

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Projects 路由 - /api/v1/projects
# ---------------------------------------------------------------------------
projects_router = APIRouter(prefix="/api/v1/projects", tags=["项目"])


@projects_router.get("", summary="获取项目列表")
async def list_projects(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


@projects_router.post("", summary="创建项目")
async def create_project(body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": "stub", "title": "", "status": "created", "message": "项目创建成功（存根）"}


async def get_task_tree(
    session_id: str | None = Query(default=None, description="按会话 ID 过滤"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取项目与任务组成的树形结构数据。

    从 TaskService 获取所有任务，构建 根任务 → 子任务 的树形层级，
    支持 session_id 过滤（基于 task.metadata["session_id"]），
    返回树形结构供前端 FileTreeWidget 渲染。

    Returns:
        包含 tree（树形结构）、items（扁平列表）、total 的字典
    """
    task_service = _get_task_service()
    if task_service is None:
        logger.warning("get_task_tree: TaskService 不可用，返回空树")
        return _empty_tree(session_id)

    try:
        # BUG-FIX-fix_20260512_async_list_all: 添加 await
        all_tasks = await task_service.list_all(limit=500, reverse=False)
    except Exception as exc:
        logger.warning("get_task_tree: list_all 失败: %s", exc)
        return _empty_tree(session_id)

    # 按 session_id 过滤
    # 策略 1：直接匹配 task.metadata["session_id"]
    # 策略 2：通过 parent_pipeline_id 关联会话的 pipeline_ids
    # 策略 3：pipeline_run_id 在会话的 pipeline_ids 中
    if session_id:
        related_pipeline_ids: set[str] = set()
        try:
            from channels.api.routes_threads import (
                store as api_store,
                _recover_threads_from_pipelines,
            )
            _recover_threads_from_pipelines(_user.get("sub", ""))
            session = api_store.get_session(session_id)
            if session and session.pipeline_ids:
                related_pipeline_ids = set(session.pipeline_ids)
            # BUG-FIX-fix_20260513_child_pipeline: 加入子管道 ID
            from infrastructure.execution_record_storage import ExecutionRecordStorage
            try:
                exec_storage = ExecutionRecordStorage()
                root_map = getattr(exec_storage, "_pipeline_root_map", {})
                child_ids = {c for c, r in root_map.items() if r in related_pipeline_ids}
                related_pipeline_ids.update(child_ids)
            except Exception:
                pass
        except Exception:
            pass

        # 第一轮：收集匹配的任务 ID
        matched_ids: set[str] = set()
        for t in all_tasks:
            if t.metadata.get("session_id") == session_id:
                matched_ids.add(t.id)
                continue
            if t.parent_pipeline_id and t.parent_pipeline_id in related_pipeline_ids:
                matched_ids.add(t.id)
                continue
            if t.pipeline_run_id and t.pipeline_run_id in related_pipeline_ids:
                matched_ids.add(t.id)
                continue

        # 第二轮：向上补全祖先任务 + 向下补全子孙任务，确保树结构完整
        task_by_id: dict[str, Any] = {t.id: t for t in all_tasks}
        children_of: dict[str, list[str]] = {}
        for t in all_tasks:
            if t.parent_task_id:
                children_of.setdefault(t.parent_task_id, []).append(t.id)

        extra_ids: set[str] = set()
        queue: list[str] = list(matched_ids)
        while queue:
            tid = queue.pop()
            current = task_by_id.get(tid)
            if not current:
                continue
            # 向上补全祖先
            if current.parent_task_id:
                parent = task_by_id.get(current.parent_task_id)
                if parent and parent.id not in matched_ids and parent.id not in extra_ids:
                    extra_ids.add(parent.id)
                    queue.append(parent.id)
            # 向下补全子孙
            for child_id in children_of.get(tid, []):
                if child_id not in matched_ids and child_id not in extra_ids:
                    extra_ids.add(child_id)
                    queue.append(child_id)

        matched_ids |= extra_ids
        all_tasks = [t for t in all_tasks if t.id in matched_ids]

    # 构建扁平列表
    flat_items = [_task_to_tree_item(t, session_id) for t in all_tasks]

    # 构建树形结构：根任务 → 子任务
    # BUG-FIX-fix_20260513_orphan_tasks: parent_task_id 指向不存在的任务时视为根任务
    task_id_set = {t.id for t in all_tasks}
    children_map: dict[str, list[dict[str, Any]]] = {}
    root_items: list[dict[str, Any]] = []

    for item in flat_items:
        parent_id = item.get("parent_task_id")
        if parent_id and parent_id in task_id_set:
            children_map.setdefault(parent_id, []).append(item)
        else:
            root_items.append(item)

    # 递归填充子节点
    _fill_children(root_items, children_map)

    total = len(flat_items)
    return {
        "id": "tree",
        "title": "任务",
        "status": "active",
        "children": root_items,
        "items": flat_items,
        "total": total,
        "session_id": session_id,
    }


def _get_task_service() -> Any:
    """通过 ServiceProvider 获取全局 TaskService 实例。

    BUG-FIX-fix_20260506_006: 使用 get_or_create 替代 get，支持懒加载创建
    问题根因: 使用 provider.get() 只能获取已注册的实例，TaskService
              从未在启动时显式注册，导致总是返回 None，API 返回空树
    修复方案: 使用 get_or_create 懒加载创建 TaskService 实例，
              与 task_submit.py 中的获取方式保持一致

    Returns:
        TaskService 实例，服务不可用或创建失败时返回 None
    """
    try:
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
        return provider.get_or_create(
            "task_service",
            lambda: __import__("tasks.service", fromlist=["TaskService"]).TaskService(),
        )
    except Exception:
        return None


def _empty_tree(session_id: str | None) -> dict[str, Any]:
    """构建空的任务树响应。

    Args:
        session_id: 会话 ID，可为 None

    Returns:
        空树结构的字典
    """
    return {
        "id": "tree",
        "title": "任务",
        "status": "active",
        "children": [],
        "items": [],
        "total": 0,
        "session_id": session_id,
    }


def _task_to_tree_item(task: Any, session_id: str | None = None) -> dict[str, Any]:
    """将 TaskModel 转换为前端树节点格式。

    Args:
        task: TaskModel 实例
        session_id: 当前会话 ID，写入节点以便前端判断跨会话

    Returns:
        树节点字典，包含 id、title、status、type、pipeline_run_id、
        ws_mode、ws_path 等字段
    """
    status_val = task.status.value if hasattr(task.status, "value") else str(task.status)

    # 安全提取 ws_meta 工作空间元信息
    _metadata = getattr(task, "metadata", None) or {}
    _ws_meta = _metadata.get("ws_meta", {}) or {}

    # BUG-FIX-fix_20260509_agent_level: 修复 agent_level 序列化格式
    # 问题根因: str(AgentLevel.L2_SUBTASK) 输出 "AgentLevel.L2_SUBTASK"，
    #          前端无法正确解析为数字层级
    # 修复方案: 使用 .value 属性获取 "L1"/"L2"/"L3" 字符串
    _agent_level = getattr(task, "agent_level", None)
    _agent_level_str = _agent_level.value if _agent_level and hasattr(_agent_level, "value") else str(_agent_level or "")

    return {
        "id": task.id,
        "title": task.title or f"任务 {task.id[:8]}",
        "description": getattr(task, "description", "") or "",
        "status": status_val,
        "type": "task",
        "parent_task_id": task.parent_task_id,
        "pipeline_run_id": getattr(task, "pipeline_run_id", None),
        "execution_record_id": getattr(task, "execution_record_id", None),
        "agent_name": getattr(task, "agent_name", ""),
        "agent_level": _agent_level_str,
        "priority": str(getattr(task, "priority", "normal")),
        "created_at": getattr(task, "created_at", ""),
        "completed_at": getattr(task, "completed_at", None),
        "error": getattr(task, "error", None),
        "ws_mode": _ws_meta.get("mode"),
        "ws_path": _ws_meta.get("path"),
        "task_scope": _metadata.get("task_scope", "non_container"),
        "session_id": _metadata.get("session_id") or session_id,
    }


def _fill_children(
    items: list[dict[str, Any]],
    children_map: dict[str, list[dict[str, Any]]],
) -> None:
    """递归填充树节点的 children 字段。

    就地修改 items 中每个节点，将其子节点从 children_map 中
    取出并挂载到 "children" 键下。

    Args:
        items: 当前层级的树节点列表
        children_map: 父任务 ID → 子节点列表的映射
    """
    for item in items:
        task_id = item["id"]
        kids = children_map.get(task_id, [])
        if kids:
            _fill_children(kids, children_map)
        item["children"] = kids


@projects_router.get("/{project_id}", summary="获取项目详情")
async def get_project(project_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": project_id, "title": "", "status": "active"}


@projects_router.post("/{project_id}/auto-execute", summary="切换自动执行")
async def toggle_auto_execute(project_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": project_id, "auto_execute": False}


@projects_router.post("/{project_id}/pause", summary="暂停项目")
async def pause_project(project_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": project_id, "status": "paused"}


@projects_router.post("/{project_id}/resume", summary="恢复项目")
async def resume_project(project_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": project_id, "status": "active"}


@projects_router.delete("/{project_id}", summary="删除项目")
async def delete_project(project_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"message": "项目已删除", "id": project_id}


# ---------------------------------------------------------------------------
# Users 路由 - /api/v1/users
# ---------------------------------------------------------------------------
users_router = APIRouter(prefix="/api/v1/users", tags=["用户管理"])


@users_router.get("", summary="获取用户列表")
async def list_users(_user: dict = Depends(require_auth)) -> list[dict[str, Any]]:
    return []


@users_router.get("/stats", summary="获取用户统计")
async def get_user_stats(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"total_users": 0, "active_users": 0, "admin_count": 0}


@users_router.post("", summary="创建用户")
async def create_user(
    username: str | None = Query(default=None),
    password: str | None = Query(default=None),
    role: str | None = Query(default=None),
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"id": "stub", "username": "", "message": "用户创建成功（存根）"}


@users_router.api_route("/{user_id}/role", methods=["PUT", "PATCH"], summary="更新用户角色")
async def update_user_role(user_id: str, body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": user_id, "role": "user"}


@users_router.api_route("/{user_id}/active", methods=["PUT", "PATCH"], summary="更新用户激活状态")
async def update_user_active(user_id: str, body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": user_id, "is_active": True}


@users_router.delete("/{user_id}", summary="删除用户")
async def delete_user(user_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"message": "用户已删除", "id": user_id}


@users_router.get("/settings", summary="获取用户设置")
async def get_user_settings(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"settings": {}}


@users_router.put("/settings", summary="更新用户设置")
async def update_user_settings(body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"settings": {}, "message": "设置已更新"}


# ---------------------------------------------------------------------------
# Monitoring 路由 - /api/v1/monitoring
# ---------------------------------------------------------------------------
monitoring_router = APIRouter(prefix="/api/v1/monitoring", tags=["监控"])


@monitoring_router.get("/system/metrics", summary="获取系统指标")
async def get_system_metrics(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {
        "metrics": {
            "cpu_usage": 0,
            "memory": {
                "total": 0,
                "used": 0,
                "available": 0,
                "usage_percent": 0,
            },
            "disk": {
                "mount_point": "/",
                "total": 0,
                "used": 0,
                "free": 0,
                "usage_percent": 0,
            },
            "uptime": 0,
            "timestamp": "",
        }
    }


@monitoring_router.get("/tasks/statistics", summary="获取任务统计")
async def get_task_statistics(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {
        "statistics": {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "running": 0,
            "pending": 0,
            "avg_duration": 0,
            "success_rate": 0,
        }
    }


@monitoring_router.get("/tasks", summary="获取监控任务列表")
async def get_monitoring_tasks(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


@monitoring_router.get("/events", summary="获取事件列表")
async def get_event_list(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


# ---------------------------------------------------------------------------
# Triggers 路由 - /api/v1/triggers
# ---------------------------------------------------------------------------
triggers_router = APIRouter(prefix="/api/v1/triggers", tags=["触发器"])


@triggers_router.get("", summary="获取触发器列表")
async def list_triggers(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"total": 0, "triggers": []}


@triggers_router.get("/stats", summary="获取触发器统计")
async def get_trigger_stats(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {
        "total_triggers": 0,
        "enabled_triggers": 0,
        "disabled_triggers": 0,
        "type_counts": {},
        "trigger_ids": [],
    }


@triggers_router.get("/{trigger_id}", summary="获取触发器详情")
async def get_trigger(trigger_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": trigger_id, "name": "", "type": "", "enabled": False}


@triggers_router.post("", summary="创建触发器")
async def create_trigger(body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": "stub", "name": "", "message": "触发器创建成功（存根）"}


@triggers_router.put("/{trigger_id}", summary="更新触发器")
async def update_trigger(trigger_id: str, body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": trigger_id, "message": "触发器已更新"}


@triggers_router.delete("/{trigger_id}", summary="删除触发器")
async def delete_trigger(trigger_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"message": "触发器已删除", "id": trigger_id}


@triggers_router.post("/{trigger_id}/enable", summary="启用触发器")
async def enable_trigger(trigger_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": trigger_id, "enabled": True}


@triggers_router.post("/{trigger_id}/disable", summary="禁用触发器")
async def disable_trigger(trigger_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": trigger_id, "enabled": False}


@triggers_router.post("/{trigger_id}/trigger", summary="手动触发")
async def manual_trigger(trigger_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": trigger_id, "triggered": True}


# ---------------------------------------------------------------------------
# Interaction 路由 - /api/v1/interaction
# ---------------------------------------------------------------------------
interaction_router = APIRouter(prefix="/api/v1/interaction", tags=["人类交互"])


@interaction_router.post("/response", summary="提交交互响应")
async def submit_interaction_response(body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"success": True, "message": "响应已提交"}


@interaction_router.get("/pending", summary="获取待处理请求")
async def get_pending_interactions(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


@interaction_router.get("/{request_id}", summary="获取交互请求详情")
async def get_interaction(request_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": request_id, "status": "pending", "type": ""}


@interaction_router.post("/{request_id}/approve", summary="批准请求")
async def approve_interaction(request_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": request_id, "status": "approved"}


@interaction_router.post("/{request_id}/deny", summary="拒绝请求")
async def deny_interaction(request_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": request_id, "status": "denied"}


@interaction_router.post("/{request_id}/cancel", summary="取消请求")
async def cancel_interaction(request_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": request_id, "status": "cancelled"}


@interaction_router.post("/{request_id}/viewed", summary="标记已查看")
async def mark_viewed(request_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": request_id, "viewed": True}


# ---------------------------------------------------------------------------
# Agent Calls 路由 - /api/v1/agent-calls
# ---------------------------------------------------------------------------
agent_calls_router = APIRouter(prefix="/api/v1/agent-calls", tags=["Agent调用记录"])


@agent_calls_router.get("", summary="获取调用记录列表")
async def list_agent_calls(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


@agent_calls_router.get("/statistics", summary="获取调用统计")
async def get_agent_call_statistics(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"total_calls": 0, "success_rate": 0.0, "avg_duration_ms": 0}


@agent_calls_router.get("/{execution_id}", summary="获取调用详情")
async def get_agent_call(execution_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": execution_id, "status": "not_found"}


# ---------------------------------------------------------------------------
# Execution Records 路由 - /api/v1/execution
# ---------------------------------------------------------------------------
execution_router = APIRouter(prefix="/api/v1/execution", tags=["执行记录"])


@execution_router.get("/records", summary="获取执行记录列表")
async def list_execution_records(
    session_id: str | None = Query(default=None, description="按会话ID过滤"),
    parent_record_id: str | None = Query(default=None, description="按父记录ID过滤"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"records": [], "total": 0, "session_id": session_id}


@execution_router.get("/records/sessions", summary="获取有记录的会话列表")
async def get_execution_record_sessions(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"sessions": [], "total": 0}


@execution_router.get("/records/group-summary", summary="获取记录分组概要")
async def get_record_group_summary(
    session_id: str | None = Query(default=None, description="按会话ID过滤"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"groups": [], "total_groups": 0}


@execution_router.get("/records/tree/{session_id}", summary="获取执行记录树")
async def get_execution_tree(
    session_id: str,
    max_depth: int = Query(default=5, ge=1, le=20),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"tree": [], "total": 0, "session_id": session_id, "max_depth": max_depth}


@execution_router.get("/records/{record_id}/children", summary="获取子执行记录")
async def get_children_records(
    record_id: str,
    _user: dict = Depends(require_auth),
) -> list[dict[str, Any]]:
    return []


@execution_router.get("/records/{record_id}", summary="获取单条执行记录")
async def get_execution_record(
    record_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"id": record_id, "session_id": "", "message_data": {}, "created_at": ""}


@execution_router.delete("/records/{record_id}", summary="删除执行记录")
async def delete_execution_record(
    record_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"success": True, "message": "记录已删除", "id": record_id}


@execution_router.delete("/records/session/{session_id}", summary="按会话删除执行记录")
async def delete_execution_records_by_session(
    session_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"success": True, "deleted_count": 0, "session_id": session_id}


@execution_router.post("/records/clear-all", summary="清理所有记录")
async def clear_all_records(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"success": True, "message": "所有记录已清理", "cleared_count": 0}


@execution_router.get("", summary="获取执行列表")
async def list_executions(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


@execution_router.get("/{execution_id}", summary="获取执行状态")
async def get_execution_status(
    execution_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"id": execution_id, "intent": "", "status": "not_found", "created_at": ""}


@execution_router.post("/{execution_id}/control", summary="执行控制（通用）")
async def control_execution(
    execution_id: str,
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"id": execution_id, "status": "controlled", "action": body.get("action", "") if body else ""}


@execution_router.post("/{execution_id}/cancel", summary="取消执行")
async def cancel_execution(
    execution_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"id": execution_id, "status": "cancelled"}


@execution_router.post("/{execution_id}/retry", summary="重试执行")
async def retry_execution(
    execution_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"id": execution_id, "status": "running"}


@execution_router.post("/{execution_id}/approve", summary="审批执行")
async def approve_execution(
    execution_id: str,
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"id": execution_id, "status": "approved"}


@execution_router.get("/{execution_id}/steps", summary="获取执行步骤")
async def get_execution_steps(
    execution_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"steps": [], "execution_id": execution_id}


@execution_router.post("/{execution_id}/inject", summary="注入Agent消息")
async def inject_agent_message(
    execution_id: str,
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    return {"id": execution_id, "status": "injected"}


# ---------------------------------------------------------------------------
# Sessions 路由 - /api/v1/sessions
# ---------------------------------------------------------------------------
sessions_router = APIRouter(prefix="/api/v1/sessions", tags=["会话"])


@sessions_router.get("/{session_id}/total-token-usage", summary="获取会话总Token用量")
async def get_session_total_token_usage(session_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"session_id": session_id, "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "request_count": 0}


@sessions_router.get("/{session_id}/context-token-usage", summary="获取上下文Token用量")
async def get_session_context_token_usage(session_id: str, parent_execution_record_id: str | None = Query(default=None), _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"current_context_tokens": 0, "is_estimated": True, "model": "default"}


# ---------------------------------------------------------------------------
# Knowledge Base 路由 - /api/v1/knowledge-base
# ---------------------------------------------------------------------------
knowledge_base_router = APIRouter(prefix="/api/v1/knowledge-base", tags=["知识库"])


@knowledge_base_router.get("", summary="获取知识库列表")
async def list_knowledge_base(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


@knowledge_base_router.get("/stats", summary="获取知识库统计")
async def get_knowledge_base_stats(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"total_documents": 0, "total_chunks": 0, "total_categories": 0}


@knowledge_base_router.post("/upload", summary="上传文件")
async def upload_knowledge_base(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"success": True, "message": "文件上传成功（存根）"}


@knowledge_base_router.get("/check", summary="检查知识库")
async def check_knowledge_base(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"available": False, "message": "知识库服务未配置"}


@knowledge_base_router.get("/categories", summary="获取分类列表")
async def list_categories(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


@knowledge_base_router.post("/categories", summary="创建分类")
async def create_category(body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"message": "分类创建成功（存根）"}


@knowledge_base_router.delete("/categories/{name}", summary="删除分类")
async def delete_category(name: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"message": f"分类 '{name}' 已删除"}


@knowledge_base_router.get("/tags", summary="获取标签列表")
async def list_tags(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


@knowledge_base_router.get("/{item_id}", summary="获取知识库详情")
async def get_knowledge_base_item(item_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": item_id, "title": "", "content": ""}


@knowledge_base_router.delete("/{item_id}", summary="删除知识库条目")
async def delete_knowledge_base_item(item_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"message": "条目已删除", "id": item_id}


# ---------------------------------------------------------------------------
# Floating Chat 路由 - /api/v1/floating-chat
# ---------------------------------------------------------------------------
floating_chat_router = APIRouter(prefix="/api/v1/floating-chat", tags=["悬浮窗"])


@floating_chat_router.get("/status", summary="获取悬浮窗状态")
async def get_floating_chat_status(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"active": False}


@floating_chat_router.post("/launch", summary="启动悬浮窗")
async def launch_floating_chat(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"active": True, "message": "悬浮窗已启动"}


# ---------------------------------------------------------------------------
# Cost Control 路由 - /api/v1/cost-control
# ---------------------------------------------------------------------------
cost_control_router = APIRouter(prefix="/api/v1/cost-control", tags=["成本控制"])


@cost_control_router.get("/budget/status", summary="获取预算状态")
async def get_budget_status(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {
        "scope": "global",
        "scope_id": "",
        "limit": 0,
        "used": 0,
        "remaining": 0,
        "usage_percent": 0,
        "alert_level": "normal",
        "estimated_cost": 0,
    }


@cost_control_router.get("/usage/statistics", summary="获取使用统计")
async def get_usage_statistics(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {
        "global_stats": {
            "daily_tokens": 0,
            "monthly_tokens": 0,
            "daily_limit": 0,
            "monthly_limit": 0,
            "daily_usage_percent": 0,
            "monthly_usage_percent": 0,
            "estimated_daily_cost": 0,
            "estimated_monthly_cost": 0,
        },
        "tasks": [],
        "sessions": [],
        "recent_records": [],
        "updated_at": "",
    }


@cost_control_router.get("/config", summary="获取成本配置")
async def get_cost_control_config(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"budget_limit": 0, "alert_threshold": 0.8}


@cost_control_router.get("/report", summary="获取成本报表")
async def get_cost_report(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total_cost": 0}


@cost_control_router.post("/budget/reset", summary="重置预算")
async def reset_budget(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"success": True, "message": "预算已重置"}


# ---------------------------------------------------------------------------
# Evaluation 路由 - /api/v1/evaluation
# ---------------------------------------------------------------------------
evaluation_router = APIRouter(prefix="/api/v1/evaluation", tags=["评估"])


@evaluation_router.post("/evaluate", summary="执行评估")
async def evaluate(body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"success": False, "message": "评估执行需要连接评估引擎", "results": []}


@evaluation_router.get("/profiles", summary="获取评估配置列表")
async def list_profiles(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


@evaluation_router.get("/profiles/default", summary="获取默认评估配置")
async def get_default_profile(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": "default", "name": "默认配置", "metrics": []}


@evaluation_router.get("/profiles/{profile_id}", summary="获取单个评估配置")
async def get_profile(profile_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": profile_id, "name": "", "metrics": []}


@evaluation_router.post("/profiles/{profile_id}/set-default", summary="设置默认配置")
async def set_default_profile(profile_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": profile_id, "is_default": True}


@evaluation_router.get("/reports", summary="获取评估报告列表")
async def list_reports(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


@evaluation_router.get("/reports/{report_id}", summary="获取评估报告")
async def get_report(report_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": report_id, "status": "not_found", "results": []}


@evaluation_router.get("/statistics", summary="获取评估统计")
async def get_statistics(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"total_evaluations": 0, "pass_rate": 0.0}


@evaluation_router.get("/trends", summary="获取评估趋势")
async def get_trends(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "period": "7d"}


# ---------------------------------------------------------------------------
# Evaluation Metrics 别名路由 - /api/v1/evaluation-metrics
# ---------------------------------------------------------------------------
eval_metrics_alias_router = APIRouter(prefix="/api/v1/evaluation-metrics", tags=["评估指标别名"])


@eval_metrics_alias_router.get("", summary="获取评估指标列表（别名）")
async def list_eval_metrics_alias(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    try:
        from channels.api.routes_evaluation import list_metrics
        result = await list_metrics()
        if "items" in result and "metrics" not in result:
            result["metrics"] = result.pop("items")
        return result
    except Exception:
        return {"metrics": [], "total": 0}


@eval_metrics_alias_router.get("/{metric_id}", summary="获取评估指标详情（别名）")
async def get_eval_metric_alias(metric_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    try:
        from channels.api.routes_evaluation import get_metric
        return await get_metric(metric_id, _user)
    except Exception:
        return {"id": metric_id, "name": "", "description": ""}


# ---------------------------------------------------------------------------
# Client Register 路由 - /api/client
# ---------------------------------------------------------------------------
client_router = APIRouter(prefix="/api/client", tags=["客户端"])

_client_registry: dict[str, dict[str, Any]] = {}


@client_router.post("/register", summary="注册客户端能力声明")
async def register_client(body: dict[str, Any]) -> dict[str, Any]:
    """接收客户端能力声明并存储。

    前端启动时会发送客户端的渲染能力（支持的组件、渲染空间等），
    后端可根据此信息过滤返回的 UI Schema。

    Args:
        body: 客户端能力声明，包含 renderingSpaces, supportedWidgets, clientType, version

    Returns:
        注册确认响应
    """
    client_type = body.get("clientType", "unknown")
    version = body.get("version", "1.0.0")

    _client_registry[client_type] = {
        "renderingSpaces": body.get("renderingSpaces", []),
        "supportedWidgets": body.get("supportedWidgets", []),
        "clientType": client_type,
        "version": version,
    }

    logger.info("客户端能力注册: type=%s, version=%s", client_type, version)

    return {
        "registered": True,
        "clientType": client_type,
        "version": version,
    }


# ---------------------------------------------------------------------------
# Files Capabilities 路由 - /api/v1/files
# ---------------------------------------------------------------------------
files_router = APIRouter(prefix="/api/v1/files", tags=["文件"])


@files_router.get("/capabilities", summary="获取模型文件能力")
async def get_model_file_capabilities(
    model_name: str = Query(default="default", description="模型名称"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """返回指定模型支持的文件上传能力。

    前端 ChatInput 组件在初始化时调用此接口，决定是否显示文件上传按钮
    以及限制可上传的文件类型和大小。当前返回通用默认配置。

    Args:
        model_name: 模型名称（如 glm-5.1），预留用于按模型返回不同能力

    Returns:
        模型文件能力声明，包含支持的文件类型、最大大小等信息
    """
    return {
        "model_name": model_name,
        "supports_image": True,
        "supports_document": True,
        "supported_image_types": ["image/png", "image/jpeg", "image/gif", "image/webp"],
        "supported_document_types": [
            "application/pdf",
            "text/plain",
            "text/markdown",
            "text/csv",
        ],
        "max_image_size": 20 * 1024 * 1024,
        "max_document_size": 50 * 1024 * 1024,
        "supports_audio": False,
        "supports_video": False,
        "supports_code": True,
        "supported_code_types": [
            "text/x-python",
            "text/javascript",
            "text/typescript",
            "text/html",
            "text/css",
            "application/json",
        ],
        "max_audio_size": 0,
        "max_video_size": 0,
        "max_code_size": 5 * 1024 * 1024,
    }


@files_router.post("/upload", summary="上传文件")
async def upload_file(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"file_id": "stub", "filename": "", "mime_type": "", "size": 0, "file_type": "document", "base64_data": "", "uploaded_at": ""}


@files_router.get("/supported-types", summary="获取支持的文件类型")
async def get_supported_file_types(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {
        "image_types": {"default": ["image/png", "image/jpeg", "image/gif", "image/webp"]},
        "document_types": {"default": ["application/pdf", "text/plain", "text/markdown", "text/csv"]},
        "max_image_size": 20 * 1024 * 1024,
        "max_document_size": 50 * 1024 * 1024,
    }
