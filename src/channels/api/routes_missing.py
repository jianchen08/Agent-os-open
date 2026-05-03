"""缺失路由补全模块。

提供前端期望但后端未实现的路由组，返回合理的占位响应。
包括：projects, users, monitoring, triggers, interaction,
agent-calls, execution/records, sessions, knowledge-base,
floating-chat, cost-control, evaluation, evaluation-metrics别名。
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
async def list_users(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


@users_router.get("/stats", summary="获取用户统计")
async def get_user_stats(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"total_users": 0, "active_users": 0}


@users_router.post("", summary="创建用户")
async def create_user(body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": "stub", "username": "", "message": "用户创建成功（存根）"}


@users_router.patch("/{user_id}/role", summary="更新用户角色")
async def update_user_role(user_id: str, body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"id": user_id, "role": "user"}


@users_router.patch("/{user_id}/active", summary="更新用户激活状态")
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
    return {"cpu_percent": 0, "memory_percent": 0, "disk_percent": 0}


@monitoring_router.get("/tasks/statistics", summary="获取任务统计")
async def get_task_statistics(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"total": 0, "pending": 0, "running": 0, "completed": 0, "failed": 0}


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
    return {"items": [], "total": 0}


@triggers_router.get("/stats", summary="获取触发器统计")
async def get_trigger_stats(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"total": 0, "active": 0, "triggered": 0}


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


@execution_router.post("/records/clear-all", summary="清理所有记录")
async def clear_all_records(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"success": True, "message": "所有记录已清理", "cleared_count": 0}


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
    return {"budget_limit": 0, "budget_used": 0, "budget_remaining": 0}


@cost_control_router.get("/usage/statistics", summary="获取使用统计")
async def get_usage_statistics(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"total_cost": 0, "total_tokens": 0, "period": "30d"}


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
        return await list_metrics()
    except Exception:
        return {"items": [], "total": 0}


@eval_metrics_alias_router.get("/{metric_id}", summary="获取评估指标详情（别名）")
async def get_eval_metric_alias(metric_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    try:
        from channels.api.routes_evaluation import get_metric
        return await get_metric(metric_id, _user)
    except Exception:
        return {"id": metric_id, "name": "", "description": ""}
