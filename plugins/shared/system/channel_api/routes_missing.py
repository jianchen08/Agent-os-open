"""缺失路由补全模块。

提供前端期望但后端未实现的路由组，返回合理的占位响应。
包括：projects, users, triggers, interaction,
agent-calls, execution/records, sessions, knowledge-base,
floating-chat, files/capabilities。
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from deps import require_auth
from fastapi import APIRouter, Depends, HTTPException, Query

logger = logging.getLogger(__name__)


class _HumanInteractionCapabilityProxy:
    """经内核 tool-executor 调 human_interaction_tool sidecar 的真实服务实例。

    0.2 sidecar 进程隔离：channel_api 进程内 import human.service 拿到的是本进程
    的全新实例（_requests 恒空、Event 表为空）——真实交互数据在
    human_interaction_tool 进程。经标准能力 tool-executor.invoke 调用该插件的
    interaction.* 工具（作用于真实单例），绕开动态 namespace（human-interaction）
    注册晚于本插件 initialize 的时序问题。
    capability 未覆盖的单条查询（get_request）从 get_pending 过滤兜底。
    """

    _TOOL_METHODS = {
        "get_pending": "interaction.get_pending",
        "respond": "interaction.respond",
    }

    def __init__(self, executor_call: Callable[[str, dict[str, Any]], Any]) -> None:
        self._executor_call = executor_call

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        tool = self._TOOL_METHODS.get(method)
        if not tool:
            raise RuntimeError(f"human-interaction.{method} 无对应工具")
        res = await self._executor_call("invoke", {"tool_name": tool, "args": params})
        # invoke 返回形状自适应：工具结果可能直接平铺，也可能包在 data/result 里
        for candidate in (
            res,
            res.get("data") if isinstance(res, dict) else None,
            res.get("result") if isinstance(res, dict) else None,
        ):
            if isinstance(candidate, dict):
                if candidate.get("error"):
                    raise RuntimeError(f"{tool} 失败: {candidate['error']}")
                return candidate
        raise RuntimeError(f"{tool} 返回无法解析: {type(res).__name__}")

    async def get_pending_requests(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        res = await self._call("get_pending", {"session_id": session_id, "limit": limit})
        return res.get("requests", [])

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        items = await self.get_pending_requests(limit=500)
        for it in items:
            if isinstance(it, dict) and (
                it.get("id") == request_id or it.get("request_id") == request_id
            ):
                return it
        return None

    async def respond(self, request_id: str, resp_data: dict[str, Any]) -> bool:
        # 兼容嵌套（body.response.*）与扁平（body 平铺）两种前端形状
        inner = resp_data.get("response", {}) if isinstance(resp_data, dict) else {}
        if not isinstance(inner, dict) or not inner:
            inner = resp_data if isinstance(resp_data, dict) else {}
        return await self.submit_response(
            request_id=request_id,
            response_type=inner.get("response_type", "answered"),
            selected_option=inner.get("selected_option"),
            answers=inner.get("answers"),
            feedback=inner.get("feedback"),
        )

    async def submit_response(
        self,
        request_id: str,
        response_type: str,
        selected_option: str | None = None,
        answers: list[str] | None = None,
        feedback: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        try:
            res = await self._call("respond", {
                "request_id": request_id,
                "response_type": response_type,
                "selected_option": selected_option,
                "answers": answers,
                "feedback": feedback,
            })
        except RuntimeError as exc:
            logger.warning("[channel_api] 交互响应转发失败 | request_id=%s | err=%s", request_id, exc)
            return False
        return bool(res.get("ok"))


def _channel_api_plugin() -> Any:
    """取 channel_api 入口的 plugin 实例（持有内核注入的能力句柄）。

    入口是 `python server.py`——模块名是 __main__；`from server import plugin`
    会把 server.py 再导入一遍，创建**未初始化的第二个实例**（无 capabilities），
    必须从 __main__ 取。
    """
    import __main__ as _main  # noqa: PLC0415

    plugin_obj = getattr(_main, "plugin", None)
    if plugin_obj is None:
        mod = sys.modules.get("server")
        plugin_obj = getattr(mod, "plugin", None) if mod else None
    if plugin_obj is None:
        raise ImportError("channel_api plugin 实例不可达")
    return plugin_obj


def _get_human_interaction_service():
    """优先经内核 tool-executor 转发到 human sidecar 真实实例；通道不可用回退本地。

    修复「审批卡片不弹 + 批准无响应」的实例隔离断裂：本进程 import 的
    human.service 是空实例，查 pending 恒空、respond 唤不醒真实 wait_for_choice。
    """
    try:
        executor = _channel_api_plugin().get_capability("tool-executor")

        async def _executor_call(method: str, params: dict[str, Any]) -> Any:
            # 审批等待（wait_for_choice 业务超时 86400）经 security_check →
            # human sidecar 全链长等待：SDK 默认 30s 会先于用户操作掐断
            # （2026-08-16 卡死根因同类）；显式传大值，实际超时由 human
            # 服务 enforce。
            return await executor.call(method, params, timeout=86500.0)

        return _HumanInteractionCapabilityProxy(_executor_call)
    except (KeyError, AttributeError, ImportError):
        logger.warning(
            "[channel_api] tool-executor 能力不可用，human-interaction 回退本地空实例"
            "（pending 将恒空，审批恢复/响应不可用）"
        )
        from human.service import get_human_interaction_service  # noqa: PLC0415

        return get_human_interaction_service()


def _safe_enum_value(obj):
    """延迟加载 safe_enum_value（0.2 位于 tasks/enum_utils.py）。"""
    from tasks.enum_utils import safe_enum_value  # noqa: PLC0415

    return safe_enum_value(obj)

# 模块加载时间（近似应用启动时间，用于计算运行时长）
_module_start_time: float = time.time()


# ---------------------------------------------------------------------------
# Projects 路由 - /api/v1/projects
# ---------------------------------------------------------------------------
projects_router = APIRouter(prefix="/api/v1/projects", tags=["项目"], dependencies=[Depends(require_auth)])


@projects_router.get("", summary="获取项目列表")
async def list_projects(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取项目列表。

    Returns:
        {items: [], total: 0, limit: 20, offset: 0}
    """
    return {"items": [], "total": 0, "limit": limit, "offset": offset}


@projects_router.post("", summary="创建项目")
async def create_project(body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    """创建项目。

    Returns:
        {project: {id, userId, goal, status, autoExecute, currentTaskIndex, tasks: [],
                   timestamps: {createdAt, updatedAt}}}
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    now = datetime.now(timezone.utc).isoformat()
    return {
        "project": {
            "id": "stub-project-1",
            "userId": _user.get("sub", ""),
            "goal": (body or {}).get("goal", ""),
            "status": "created",
            "autoExecute": False,
            "currentTaskIndex": 0,
            "tasks": [],
            "timestamps": {"createdAt": now, "updatedAt": now},
        }
    }


async def get_task_tree(  # noqa: PLR0912,PLR0915
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
            from routes_threads import store as api_store  # noqa: PLC0415

            session = api_store.get_session(session_id)
            if session and session.pipeline_ids:
                related_pipeline_ids = set(session.pipeline_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "get_task_tree: 解析会话 pipeline_ids 失败 | session_id=%s err=%s",
                session_id,
                exc,
                exc_info=True,
            )

        # 从任务自身的 pipeline_run_id / parent_pipeline_id 递归扩展管道树
        # 主管道的 pipeline_run_id 已在 related_pipeline_ids 中，
        # 子任务的 parent_pipeline_id 指向父管道，pipeline_run_id 是子管道自身。
        # 通过迭代扩展：已知管道 → 找到 parent_pipeline_id 匹配的任务 → 加入其 pipeline_run_id
        if related_pipeline_ids:
            changed = True
            while changed:
                changed = False
                for t in all_tasks:
                    if t.parent_pipeline_id and t.parent_pipeline_id in related_pipeline_ids:  # noqa: SIM102
                        if t.pipeline_run_id and t.pipeline_run_id not in related_pipeline_ids:
                            related_pipeline_ids.add(t.pipeline_run_id)
                            changed = True

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


from tasks.service_access import get_task_service as _get_task_service  # noqa: E402


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
    status_val = _safe_enum_value(task.status)

    # 安全提取 ws_meta 工作空间元信息
    _metadata = getattr(task, "metadata", None) or {}
    _ws_meta = _metadata.get("ws_meta", {}) or {}

    _agent_level = getattr(task, "agent_level", None)
    _agent_level_str = (
        _agent_level.value if _agent_level and hasattr(_agent_level, "value") else str(_agent_level or "")
    )

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
    """获取项目详情。

    Returns:
        {project: {id, goal, status, ...tasks}}
    """
    return {"project": {"id": project_id, "goal": "", "status": "active", "tasks": []}}


@projects_router.post("/{project_id}/auto-execute", summary="切换自动执行")
async def toggle_auto_execute(project_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    """切换自动执行。

    Returns:
        {project: {...}}
    """
    return {"project": {"id": project_id, "autoExecute": False, "status": "active"}}


@projects_router.post("/{project_id}/pause", summary="暂停项目")
async def pause_project(project_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    """暂停项目。

    Returns:
        {project: {...}}
    """
    return {"project": {"id": project_id, "status": "suspended"}}


@projects_router.post("/{project_id}/resume", summary="恢复项目")
async def resume_project(project_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    """恢复项目。

    Returns:
        {project: {...}}
    """
    return {"project": {"id": project_id, "status": "active"}}


@projects_router.delete("/{project_id}", summary="删除项目")
async def delete_project(project_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"message": "项目已删除", "id": project_id}


# ---------------------------------------------------------------------------
# Users 路由 - /api/v1/users
# ---------------------------------------------------------------------------
users_router = APIRouter(prefix="/api/v1/users", tags=["用户管理"], dependencies=[Depends(require_auth)])


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
async def update_user_role(
    user_id: str, body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)
) -> dict[str, Any]:
    return {"id": user_id, "role": "user"}


@users_router.api_route("/{user_id}/active", methods=["PUT", "PATCH"], summary="更新用户激活状态")
async def update_user_active(
    user_id: str, body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)
) -> dict[str, Any]:
    return {"id": user_id, "is_active": True}


@users_router.delete("/{user_id}", summary="删除用户")
async def delete_user(user_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"message": "用户已删除", "id": user_id}


@users_router.get("/settings", summary="获取用户设置")
async def get_user_settings(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"settings": {}}


@users_router.put("/settings", summary="更新用户设置")
async def update_user_settings(
    body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)
) -> dict[str, Any]:
    return {"settings": {}, "message": "设置已更新"}


# ---------------------------------------------------------------------------
# Triggers 路由 - /api/v1/triggers
# ---------------------------------------------------------------------------
triggers_router = APIRouter(prefix="/api/v1/triggers", tags=["触发器"], dependencies=[Depends(require_auth)])


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
async def update_trigger(
    trigger_id: str, body: dict[str, Any] | None = None, _user: dict = Depends(require_auth)
) -> dict[str, Any]:
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
interaction_router = APIRouter(prefix="/api/v1/interaction", tags=["人类交互"], dependencies=[Depends(require_auth)])


@interaction_router.post("/response", summary="提交交互响应")
async def submit_interaction_response(
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """提交交互响应，调用 HumanInteractionService.respond() 触发 Event.set()。"""
    if not body or "request_id" not in body:
        raise HTTPException(status_code=400, detail="缺少 request_id")
    service = _get_human_interaction_service()
    result = await service.respond(body["request_id"], body)
    return {"success": result}


@interaction_router.get("/pending", summary="获取待处理请求")
async def get_pending_interactions(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取所有待处理的交互请求列表。"""
    service = _get_human_interaction_service()
    requests = await service.get_pending_requests()
    return {"items": requests, "total": len(requests)}


@interaction_router.get("/{request_id}", summary="获取交互请求详情")
async def get_interaction(
    request_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """根据 request_id 获取交互请求详情，不存在则返回 404。"""
    service = _get_human_interaction_service()
    record = await service.get_request(request_id)
    if not record:
        raise HTTPException(status_code=404, detail="交互请求不存在")
    return record


@interaction_router.post("/{request_id}/approve", summary="批准请求")
async def approve_interaction(
    request_id: str,
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """批准交互请求。"""
    service = _get_human_interaction_service()
    result = await service.submit_response(
        request_id=request_id,
        response_type="approved",
        selected_option="approve",
        feedback=body.get("feedback") if body else None,
    )
    return {"success": result, "request_id": request_id, "status": "approved"}


@interaction_router.post("/{request_id}/deny", summary="拒绝请求")
async def deny_interaction(
    request_id: str,
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """拒绝交互请求。"""
    service = _get_human_interaction_service()
    result = await service.submit_response(
        request_id=request_id,
        response_type="denied",
        selected_option="reject",
        feedback=body.get("feedback") if body else None,
    )
    return {"success": result, "request_id": request_id, "status": "denied"}


@interaction_router.post("/{request_id}/cancel", summary="取消请求")
async def cancel_interaction(
    request_id: str,
    body: dict[str, Any] | None = None,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """取消交互请求。"""
    service = _get_human_interaction_service()
    result = await service.cancel_request(
        request_id=request_id,
        reason=body.get("reason") if body else None,
    )
    return {"success": result, "request_id": request_id, "status": "cancelled"}


@interaction_router.post("/{request_id}/viewed", summary="标记已查看")
async def mark_viewed(
    request_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """标记交互请求为已查看状态。"""
    service = _get_human_interaction_service()
    result = await service.mark_as_viewed(request_id)
    return {"success": result, "request_id": request_id, "viewed": True}


# ---------------------------------------------------------------------------
# Agent Calls 路由 - /api/v1/agent-calls
# ---------------------------------------------------------------------------
agent_calls_router = APIRouter(
    prefix="/api/v1/agent-calls", tags=["Agent调用记录"], dependencies=[Depends(require_auth)]
)


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
# 数据来源：ExecutionRecordStorage（按 pipeline_run_id 分组的 YAML 持久化）。
# ---------------------------------------------------------------------------
execution_router = APIRouter(prefix="/api/v1/execution", tags=["执行记录"], dependencies=[Depends(require_auth)])

# 执行记录的 YAML 存储后端（ExecutionRecordStorage）已在 0.2 架构中废弃：
# 内核 pipeline_loop 将消息/轨迹下沉到 SQLite（messages / traces / pipeline_checkpoints）。
# 以下 handler 改为返回空结构，保持前端契约不破坏（HTTP 200 + 空载荷），
# 待后续接入内核 messages/traces 能力后再恢复数据。


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
    # 存储层无 parent_record_id 概念，无子记录可返回
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
sessions_router = APIRouter(prefix="/api/v1/sessions", tags=["会话"], dependencies=[Depends(require_auth)])


@sessions_router.get("/{session_id}/total-token-usage", summary="获取会话总Token用量")
async def get_session_total_token_usage(session_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"session_id": session_id, "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "request_count": 0}


@sessions_router.get("/{session_id}/context-token-usage", summary="获取上下文Token用量")
async def get_session_context_token_usage(
    session_id: str, parent_execution_record_id: str | None = Query(default=None), _user: dict = Depends(require_auth)
) -> dict[str, Any]:
    return {"current_context_tokens": 0, "is_estimated": True, "model": "default"}


# ---------------------------------------------------------------------------
# Knowledge Base 路由 - /api/v1/knowledge-base
# ---------------------------------------------------------------------------
knowledge_base_router = APIRouter(
    prefix="/api/v1/knowledge-base", tags=["知识库"], dependencies=[Depends(require_auth)]
)


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
floating_chat_router = APIRouter(prefix="/api/v1/floating-chat", tags=["悬浮窗"], dependencies=[Depends(require_auth)])


@floating_chat_router.get("/status", summary="获取悬浮窗状态")
async def get_floating_chat_status(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"active": False}


@floating_chat_router.post("/launch", summary="启动悬浮窗")
async def launch_floating_chat(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"active": True, "message": "悬浮窗已启动"}


# ---------------------------------------------------------------------------
# Files Capabilities 路由 - /api/v1/files
# ---------------------------------------------------------------------------
files_router = APIRouter(prefix="/api/v1/files", tags=["文件"], dependencies=[Depends(require_auth)])


@files_router.get("/capabilities", summary="获取模型文件能力")
async def get_model_file_capabilities(
    model_name: str = Query(default="default", description="模型名称"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """返回指定模型支持的文件上传能力。

    前端 ChatInput 组件在初始化时调用此接口，决定是否显示文件上传按钮
    以及限制可上传的文件类型和大小。

    能力数据来源于 llm.yaml 的 multimodal 配置（经
    ModelCapabilityRegistry.get_capability 读取），按模型返回真实能力。
    只声明真正的多模态能力（image/audio/video）；文本/文档/代码类附件
    由前端宽规则放行、后端提取文本后直接拼进用户消息，无需声明能力。

    Args:
        model_name: 模型名称（如 glm-5.2）或别名

    Returns:
        模型文件能力声明，包含支持的多模态类型、最大大小等信息
    """
    from multimodal.capabilities import ModelCapabilityRegistry  # noqa: PLC0415

    cap = ModelCapabilityRegistry.get_capability(model_name)
    return {
        "model_name": model_name,
        "supports_image": cap.supports_image,
        "supports_audio": cap.supports_audio,
        "supports_video": cap.supports_video,
        "supported_image_types": cap.supported_image_types,
        "supported_audio_types": cap.supported_audio_types,
        "supported_video_types": cap.supported_video_types,
        "max_image_size": cap.max_image_size,
        "max_audio_size": cap.max_audio_size,
        "max_video_size": cap.max_video_size,
        "is_multimodal": cap.supports_image or cap.supports_audio or cap.supports_video,
    }


@files_router.get("/supported-types", summary="获取支持的文件类型")
async def get_supported_file_types(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {
        "image_types": {"default": ["image/png", "image/jpeg", "image/gif", "image/webp"]},
        "document_types": {"default": ["application/pdf", "text/plain", "text/markdown", "text/csv"]},
        "max_image_size": 20 * 1024 * 1024,
        "max_document_size": 50 * 1024 * 1024,
    }


# ---------------------------------------------------------------------------
# Task Phase & AC 路由 - /api/v1/tasks/{id}/phase, /api/v1/tasks/{id}/ac
# ---------------------------------------------------------------------------

task_phase_router = APIRouter(prefix="/api/v1/tasks", tags=["任务阶段"], dependencies=[Depends(require_auth)])


@task_phase_router.get("/{task_id}/phase", summary="获取任务当前阶段")
async def get_task_phase(task_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    """获取任务当前执行阶段。

    根据任务实际状态映射到前端阶段概念：
    - pending/scheduled/paused → prepare (准备阶段)
    - running → execute (执行阶段)
    - evaluating → evaluate (评估阶段)
    - completed/failed/cancelled/timeout → 终态，使用最后已知阶段

    Returns:
        {taskId, currentPhase, phaseStatus}
    """
    _STATUS_TO_PHASE: dict[str, tuple[str, str]] = {  # noqa: N806
        "pending": ("prepare", "pending"),
        "scheduled": ("prepare", "pending"),
        "suspended": ("prepare", "pending"),
        "running": ("execute", "running"),
        "blocked": ("execute", "running"),
        "evaluating": ("evaluate", "running"),
        "completed": ("evaluate", "completed"),
        "failed": ("execute", "failed"),
        "cancelled": ("prepare", "failed"),
        "timeout": ("execute", "failed"),
    }

    task_service = _get_task_service()
    if task_service:
        try:
            task = task_service.get_task(task_id)
            if task:
                status_str = _safe_enum_value(task.status)
                phase, phase_status = _STATUS_TO_PHASE.get(status_str, ("prepare", "pending"))
                return {
                    "taskId": task_id,
                    "currentPhase": phase,
                    "phaseStatus": phase_status,
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "解析任务阶段失败，回退默认 prepare/pending | task_id=%s err=%s",
                task_id,
                exc,
                exc_info=True,
            )

    return {
        "taskId": task_id,
        "currentPhase": "prepare",
        "phaseStatus": "pending",
    }


@task_phase_router.post("/{task_id}/phase/prepare/complete", summary="完成准备阶段")
async def complete_prepare_phase(task_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    """标记准备阶段完成。

    Returns:
        {task_id, current_phase}
    """
    return {"task_id": task_id, "current_phase": "execute"}


@task_phase_router.post("/{task_id}/phase/execute/complete", summary="完成执行阶段")
async def complete_execute_phase(task_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    """标记执行阶段完成。

    Returns:
        {task_id, current_phase}
    """
    return {"task_id": task_id, "current_phase": "review"}


@task_phase_router.get("/{task_id}/phase/{phase}/output", summary="获取阶段输出")
async def get_phase_output(task_id: str, phase: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    """获取指定阶段的输出结果。

    Returns:
        {output, error}
    """
    return {"output": None, "error": None}


@task_phase_router.get("/{task_id}/ac", summary="获取任务验收标准")
async def get_task_ac(task_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    """获取任务的验收标准列表。

    Returns:
        {taskId, acceptanceCriteria: []}
    """
    return {"taskId": task_id, "acceptanceCriteria": []}


@task_phase_router.post("/{task_id}/ac/{ac_id}/evaluate", summary="评估单个验收标准")
async def evaluate_ac(
    task_id: str,
    ac_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """评估单个验收标准。

    Returns:
        {acceptance_criterion: {...}}
    """
    return {
        "acceptance_criterion": {
            "id": ac_id,
            "task_id": task_id,
            "status": "not_evaluated",
            "passed": None,
        },
    }


@task_phase_router.post("/{task_id}/ac/evaluate-all", summary="评估所有验收标准")
async def evaluate_all_ac(task_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    """评估任务的所有验收标准。

    Returns:
        {taskId, acceptanceCriteria: []}
    """
    return {"taskId": task_id, "acceptanceCriteria": []}


@task_phase_router.get("/{task_id}/ac/{ac_id}/result", summary="获取验收标准评估结果")
async def get_ac_result(
    task_id: str,
    ac_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取验收标准的评估结果。

    Returns:
        {acceptance_criterion: {...}}
    """
    return {
        "acceptance_criterion": {
            "id": ac_id,
            "task_id": task_id,
            "status": "not_evaluated",
            "passed": None,
        },
    }
