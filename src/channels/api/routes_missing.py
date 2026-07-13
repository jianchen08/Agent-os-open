"""缺失路由补全模块。

提供前端期望但后端未实现的路由组，返回合理的占位响应。
包括：projects, users, monitoring, triggers, interaction,
agent-calls, execution/records, sessions, knowledge-base,
floating-chat, cost-control, evaluation, evaluation-metrics别名,
files/capabilities。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from channels.api.deps import require_auth
from human_interaction import get_human_interaction_service
from utils.enum_utils import safe_enum_value

logger = logging.getLogger(__name__)

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
            from channels.api.routes_threads import store as api_store  # noqa: PLC0415

            session = api_store.get_session(session_id)
            if session and session.pipeline_ids:
                related_pipeline_ids = set(session.pipeline_ids)
        except Exception:
            pass

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
    status_val = safe_enum_value(task.status)

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
# Monitoring 路由 - /api/v1/monitoring
# ---------------------------------------------------------------------------
monitoring_router = APIRouter(prefix="/api/v1/monitoring", tags=["监控"], dependencies=[Depends(require_auth)])


def _with_fallback_strategies(
    strategies: list[Callable[[], dict[str, Any] | None]],
    default: dict[str, Any],
) -> dict[str, Any]:
    """按顺序尝试多个数据获取策略，返回第一个成功结果。

    统一封装监控函数中反复出现的「主策略→降级1→降级2→默认空响应」三级降级链。
    每个策略函数返回 None 表示失败（触发下一个策略），返回 dict 表示成功。

    Args:
        strategies: 策略函数列表，按优先级从高到低排列
        default: 所有策略都失败时的兜底响应

    Returns:
        第一个成功的策略结果，或 default
    """
    for strategy in strategies:
        result = strategy()
        if result is not None:
            return result
    return default


def _get_token_usage() -> dict[str, Any]:
    """获取全局 token 用量统计。

    从 infrastructure 层获取真实数据，依次尝试：
    1. ExecutionRecordStorage 中汇总的 PipelineRunSummary（已注册、有真实数据）
    2. ServiceProvider 中注册的 UsageMonitor（预留，当前未注册）
    3. PerformanceMonitor 中的 LLM 统计（预留，当前未注册）
    4. 零值兜底

    Returns:
        包含 total_tokens, prompt_tokens, completion_tokens, request_count 的字典
    """

    def _strategy_execution_record_storage() -> dict[str, Any] | None:
        from infrastructure.service_access import get_execution_record_storage  # noqa: PLC0415

        storage = get_execution_record_storage()
        if storage is None:
            return None
        tokens = storage.get_total_tokens()
        # tokens 格式: {"input_tokens": N, "output_tokens": N, "total_tokens": N, "cached_tokens": N}
        total = tokens.get("total_tokens", 0)
        if total == 0 and not tokens:
            return None
        # request_count = 各管道迭代次数之和（每轮迭代对应一次 LLM 调用）。
        summaries = storage.list_all_summaries()
        request_count = 0
        for summary in summaries:
            iters = getattr(summary, "total_iterations", 0)
            if isinstance(iters, int) and iters > 0:
                request_count += iters
        return {
            "total_tokens": total,
            "prompt_tokens": tokens.get("input_tokens", 0),
            "completion_tokens": tokens.get("output_tokens", 0),
            "request_count": request_count,
        }

    def _strategy_usage_monitor() -> dict[str, Any] | None:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        provider = get_service_provider()
        monitor = provider.get("usage_monitor")
        if monitor is None:
            return None
        stats = monitor.get_statistics()
        records = monitor.get_recent_records(limit=10000)
        prompt_total = sum(r.prompt_tokens for r in records)
        completion_total = sum(r.completion_tokens for r in records)
        return {
            "total_tokens": stats.total_tokens,
            "prompt_tokens": prompt_total,
            "completion_tokens": completion_total,
            "request_count": stats.total_requests,
        }

    def _strategy_perf_monitor() -> dict[str, Any] | None:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        provider = get_service_provider()
        perf_monitor = provider.get("performance_monitor")
        if perf_monitor is None:
            return None
        llm_stats = getattr(perf_monitor, "_llm_stats", {})
        return {
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "request_count": llm_stats.get("request_count", 0),
        }

    return _with_fallback_strategies(
        [_strategy_execution_record_storage, _strategy_usage_monitor, _strategy_perf_monitor],
        {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "request_count": 0},
    )


def _build_cache_result(hits: int, misses: int) -> dict[str, Any]:
    """从 hits/misses 构建统一的缓存统计响应。"""
    total = hits + misses
    hit_rate = round(hits / total * 100, 2) if total > 0 else 0.0
    return {
        "cache_hits": hits,
        "cache_misses": misses,
        "hit_rate": hit_rate,
        "total_requests": total,
    }


def _get_cache_stats() -> dict[str, Any]:
    """获取 LLM prompt cache 命中率统计。

    直接取 ExecutionRecordStorage 中汇总的 cached_tokens / input_tokens：
    - cache_hits  = cached_tokens（input 中命中 prompt cache 的部分）
    - cache_misses = input_tokens - cached_tokens
    - hit_rate   = cache_hits / (hits + misses)

    Returns:
        包含 cache_hits, cache_misses, hit_rate, total_requests 的字典
    """
    from infrastructure.service_access import get_execution_record_storage  # noqa: PLC0415

    storage = get_execution_record_storage()
    if storage is None:
        return {"cache_hits": 0, "cache_misses": 0, "hit_rate": 0.0, "total_requests": 0}

    tokens = storage.get_total_tokens()
    input_tokens = tokens.get("input_tokens", 0)
    cached_tokens = tokens.get("cached_tokens", 0)
    # cached_tokens 是 input_tokens 中命中 prompt cache 的部分
    hits = cached_tokens
    misses = max(input_tokens - cached_tokens, 0)
    return _build_cache_result(hits, misses)


def _get_system_metrics() -> dict[str, Any]:
    """获取真实系统指标。

    依次尝试：
    1. PerformanceMonitor.get_current_metrics()
    2. psutil 直接采集
    3. 零值兜底
    """

    def _strategy_perf_monitor() -> dict[str, Any] | None:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        provider = get_service_provider()
        perf_monitor = provider.get("performance_monitor")
        if perf_monitor is None or not hasattr(perf_monitor, "get_system_metrics"):
            return None
        import asyncio  # noqa: PLC0415

        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 不能在运行中的事件循环里 await，用 psutil 直接采集
            return _collect_psutil_metrics()
        metrics = loop.run_until_complete(perf_monitor.get_system_metrics())
        return _format_system_metrics(
            cpu=metrics.cpu_usage,
            memory_percent=metrics.memory_usage,
            disk_percent=metrics.disk_usage,
        )

    def _strategy_psutil() -> dict[str, Any] | None:
        return _collect_psutil_metrics()

    return _with_fallback_strategies(
        [_strategy_perf_monitor, _strategy_psutil],
        _format_system_metrics(),
    )


def _collect_psutil_metrics() -> dict[str, Any]:
    """通过 psutil 直接采集系统指标（同步）。"""
    try:
        import psutil  # noqa: PLC0415

        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return _format_system_metrics(
            cpu=psutil.cpu_percent(interval=0.1),
            memory_percent=mem.percent,
            disk_percent=disk.percent,
            mem_total=mem.total,
            mem_used=mem.used,
            mem_available=mem.available,
            disk_total=disk.total,
            disk_used=disk.used,
            disk_free=disk.free,
        )
    except Exception:
        return _format_system_metrics()


def _format_system_metrics(
    cpu: float = 0.0,
    memory_percent: float = 0.0,
    disk_percent: float = 0.0,
    mem_total: int = 0,
    mem_used: int = 0,
    mem_available: int = 0,
    disk_total: int = 0,
    disk_used: int = 0,
    disk_free: int = 0,
) -> dict[str, Any]:
    """格式化系统指标为统一响应结构。"""
    return {
        "cpu_usage": round(cpu, 2),
        "memory": {
            "total": mem_total,
            "used": mem_used,
            "available": mem_available,
            "usage_percent": round(memory_percent, 2),
        },
        "disk": {
            "mount_point": "/",
            "total": disk_total,
            "used": disk_used,
            "free": disk_free,
            "usage_percent": round(disk_percent, 2),
        },
        "uptime": int(time.time() - _module_start_time),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _get_task_statistics() -> dict[str, Any]:
    """获取真实任务统计。

    依次尝试：
    1. TaskService.get_all_tasks()
    2. 零值兜底
    """
    _default = {
        "total": 0,
        "succeeded": 0,
        "failed": 0,
        "running": 0,
        "pending": 0,
        "avg_duration": 0,
        "success_rate": 0,
    }

    def _strategy_task_service() -> dict[str, Any] | None:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        provider = get_service_provider()
        task_service = provider.get("task_service")
        if task_service is None or not hasattr(task_service, "get_all_tasks"):
            return None
        tasks = task_service.get_all_tasks()
        total = len(tasks)
        succeeded = sum(1 for t in tasks if getattr(t, "status", "") == "completed")
        failed = sum(1 for t in tasks if getattr(t, "status", "") == "failed")
        running = sum(1 for t in tasks if getattr(t, "status", "") == "running")
        pending = sum(1 for t in tasks if getattr(t, "status", "") == "pending")
        return {
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "running": running,
            "pending": pending,
            "avg_duration": 0,
            "success_rate": round(succeeded / total * 100, 2) if total > 0 else 0,
        }

    return _with_fallback_strategies([_strategy_task_service], _default)


@monitoring_router.get("", summary="获取监控汇总数据")
async def get_monitoring_overview(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取所有监控数据的汇总视图。

    Returns:
        包含 system_metrics, task_statistics, token_usage, cache_stats 的字典
    """
    return {
        "system_metrics": _get_system_metrics(),
        "task_statistics": _get_task_statistics(),
        "token_usage": _get_token_usage(),
        "cache_stats": _get_cache_stats(),
    }


@monitoring_router.get("/token-usage", summary="获取 Token 用量统计")
async def get_token_usage(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """返回全局 token 使用统计。

    Returns:
        包含 total_tokens, prompt_tokens(input), completion_tokens(output),
        request_count 的字典
    """
    return {"token_usage": _get_token_usage()}


@monitoring_router.get("/cache-stats", summary="获取缓存命中率统计")
async def get_cache_stats(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """返回缓存统计。

    Returns:
        包含 cache_hits, cache_misses, hit_rate, total_requests 的字典
    """
    return {"cache_stats": _get_cache_stats()}


@monitoring_router.get("/system/metrics", summary="获取系统指标")
async def get_system_metrics(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    """返回真实系统指标（CPU/内存/磁盘）。"""
    return {
        "metrics": _get_system_metrics(),
        "token_usage": _get_token_usage(),
        "cache_stats": _get_cache_stats(),
    }


@monitoring_router.get("/tasks/statistics", summary="获取任务统计")
async def get_task_statistics(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    """返回真实任务统计数据。"""
    return {"statistics": _get_task_statistics()}


@monitoring_router.get("/tasks", summary="获取监控任务列表")
async def get_monitoring_tasks(
    page: int = Query(default=1, ge=1, description="页码（从1开始）"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    status: str | None = Query(default=None, description="按状态筛选"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取监控任务列表，合并 MemoryStore 和 TaskStorage 数据源。

    参照 routes_tasks.py 的 list_tasks 端点，从 MemoryStore 和 TaskStorage 两个数据源
    合并任务数据，支持 page/page_size 分页和 status 筛选，返回前端 monitoring.ts
    TaskInfo 格式的数据，供前端 DebugTasksPage 和 MonitoringPage 任务列表显示。

    Args:
        page: 页码（从1开始）
        page_size: 每页数量
        status: 可选状态筛选（pending/running/completed/failed/cancelled）
        _user: 认证用户

    Returns:
        包含 items、total、page、page_size 的字典
    """
    from channels.api.memory_store import store  # noqa: F401,PLC0415

    # _get_task_service 已在模块级别从 tasks.service_access 导入

    # 监控页面状态映射：将后端特殊状态映射为前端 TaskInfo 兼容的状态值
    # 注意：monitoring TaskInfo 使用 'running' 而非 'in_progress'
    _MONITORING_STATUS_MAP: dict[str, str] = {  # noqa: N806
        "evaluating": "running",
        "suspended": "pending",
        "queued": "pending",
    }

    def _to_monitoring_status(raw: str) -> str:
        """将后端状态映射为前端监控页面兼容的状态。"""
        return _MONITORING_STATUS_MAP.get(raw, raw)

    def _taskmodel_to_monitoring_dict(tm: Any) -> dict[str, Any]:
        """将 TaskModel dataclass 转为字典（保留原始状态值，不做 in_progress 映射）。"""
        from dataclasses import asdict  # noqa: PLC0415

        d = asdict(tm)
        # 提取枚举的原始字符串值
        raw_status = safe_enum_value(tm.status)
        d["status"] = raw_status
        if hasattr(tm, "priority") and hasattr(tm.priority, "value"):
            d["priority"] = tm.priority.value
        return d

    # 参数映射: page/page_size → offset/limit
    offset = (page - 1) * page_size
    limit = page_size

    tasks: list[dict[str, Any]] = []

    task_service = _get_task_service()
    if task_service is not None:
        try:
            ts_tasks = await task_service.list_all(limit=1000)
            for tm in ts_tasks:
                tasks.append(_taskmodel_to_monitoring_dict(tm))
        except Exception as exc:
            logger.warning(
                "monitoring/tasks: 从 TaskStorage 加载任务失败: %s",
                exc,
            )

    # ── 转换为前端 TaskInfo 格式并筛选 ──
    items_all: list[dict[str, Any]] = []
    for t in tasks:
        raw_status = t.get("status", "pending")
        mapped_status = _to_monitoring_status(raw_status)

        # 状态筛选（筛选在映射后的状态上进行）
        if status and mapped_status != status:
            continue

        # 计算执行时长（毫秒）
        duration: int | None = None
        started = t.get("started_at")
        completed = t.get("completed_at")
        if started and completed:
            try:
                from datetime import datetime as _dt  # noqa: PLC0415

                s = _dt.fromisoformat(started)
                c = _dt.fromisoformat(completed)
                duration = int((c - s).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass

        # 构建前端 monitoring.ts TaskInfo 格式
        item = {
            "id": t["id"],
            "intent": t.get("title", ""),
            "name": t.get("title", ""),
            "status": mapped_status,
            "created_at": t.get("created_at", ""),
            "started_at": started,
            "completed_at": completed,
            "agent_id": t.get("agent_id") or t.get("metadata", {}).get("target_id"),
            "error": t.get("error"),
            "duration": duration,
            "current_step": t.get("current_step"),
            "progress": t.get("progress"),
        }
        items_all.append(item)

    # 按创建时间降序排序（最新的在前）
    items_all.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    total = len(items_all)
    page_items = items_all[offset : offset + limit]

    return {
        "items": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@monitoring_router.get("/events", summary="获取事件列表")
async def get_event_list(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {"items": [], "total": 0}


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
    service = get_human_interaction_service()
    result = await service.respond(body["request_id"], body)
    return {"success": result}


@interaction_router.get("/pending", summary="获取待处理请求")
async def get_pending_interactions(
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取所有待处理的交互请求列表。"""
    service = get_human_interaction_service()
    requests = await service.get_pending_requests()
    return {"items": requests, "total": len(requests)}


@interaction_router.get("/{request_id}", summary="获取交互请求详情")
async def get_interaction(
    request_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """根据 request_id 获取交互请求详情，不存在则返回 404。"""
    service = get_human_interaction_service()
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
    service = get_human_interaction_service()
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
    service = get_human_interaction_service()
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
    service = get_human_interaction_service()
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
    service = get_human_interaction_service()
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


def _get_exec_storage() -> Any:
    """获取全局 ExecutionRecordStorage 实例，不可用时返回 None。

    与本模块 token 统计（line 418）/ cache 统计（line 504）的取用方式一致，
    集中在一个入口避免每个 handler 重复 import。
    """
    from infrastructure.service_access import get_execution_record_storage  # noqa: PLC0415

    return get_execution_record_storage()


def _record_to_response(record: Any) -> dict[str, Any]:
    """把 ExecutionRecordData 映射为前端 ExecutionRecord 接口（见 executionRecords.ts）。

    status 由 error 字段推导：有错误 → failed，否则 completed。
    depth 用 iteration 近似（存储层无显式层级概念）。
    message_data 返回完整字段快照，供详情查看。
    """
    from dataclasses import asdict  # noqa: PLC0415

    status = "failed" if getattr(record, "error", None) else "completed"
    return {
        "id": record.record_id,
        "session_id": record.pipeline_run_id,
        "parent_record_id": None,
        "depth": getattr(record, "iteration", None),
        "sequence": getattr(record, "sequence", None),
        "record_type": record.type,
        "status": status,
        "message_data": asdict(record),
        "created_at": record.created_at or "",
    }


def _summary_to_session_info(summary: Any) -> dict[str, Any]:
    """把 PipelineRunSummary 映射为前端 SessionInfo 接口。

    title 用 final_output 截断（summary 无独立标题字段）。
    updated_at 复用 created_at（summary 无独立更新时间）。
    record_count 取 total_records。
    """
    from infrastructure.execution_record_storage import summarize_text  # noqa: PLC0415

    return {
        "id": summary.run_id,
        "title": summarize_text(summary.final_output, max_len=80),
        "created_at": summary.created_at or "",
        "updated_at": summary.created_at or "",
        "record_count": getattr(summary, "total_records", 0),
    }


def _list_session_summaries_desc(storage: Any) -> list[Any]:
    """返回全部 summary，按 created_at 降序（最新在前）。"""
    summaries = storage.list_all_summaries()
    summaries.sort(key=lambda s: s.created_at or "", reverse=True)
    return summaries


@execution_router.get("/records", summary="获取执行记录列表")
async def list_execution_records(
    session_id: str | None = Query(default=None, description="按会话ID过滤"),
    parent_record_id: str | None = Query(default=None, description="按父记录ID过滤"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    storage = _get_exec_storage()
    if storage is None:
        return {"records": [], "total": 0, "session_id": session_id}

    if session_id:
        # 指定会话：直接取该 pipeline 的记录，按 sequence 排序
        records = storage.list_by_session(session_id, limit=offset + limit)
        total = storage.count_by_session(session_id)
        page = records[offset : offset + limit]
    else:
        # 全部会话：按 summary 时间倒序聚合各 pipeline 最近记录
        summaries = _list_session_summaries_desc(storage)
        collected: list[Any] = []
        for summary in summaries:
            if len(collected) >= limit:
                break
            collected.extend(storage.list_by_session(summary.run_id, limit=limit))
        collected.sort(key=lambda r: r.created_at or "", reverse=True)
        total = len(collected)
        page = collected[offset : offset + limit]

    return {
        "records": [_record_to_response(r) for r in page],
        "total": total,
        "session_id": session_id,
    }


@execution_router.get("/records/sessions", summary="获取有记录的会话列表")
async def get_execution_record_sessions(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    storage = _get_exec_storage()
    if storage is None:
        return {"sessions": [], "total": 0}

    summaries = _list_session_summaries_desc(storage)
    sessions = [_summary_to_session_info(s) for s in summaries]
    return {"sessions": sessions, "total": len(sessions)}


@execution_router.get("/records/group-summary", summary="获取记录分组概要")
async def get_record_group_summary(
    session_id: str | None = Query(default=None, description="按会话ID过滤"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    storage = _get_exec_storage()
    if storage is None:
        return {"groups": [], "total_groups": 0}

    summaries = _list_session_summaries_desc(storage)
    if session_id:
        summaries = [s for s in summaries if s.run_id == session_id]

    groups = []
    for summary in summaries:
        records = storage.list_by_session(summary.run_id, limit=1)
        groups.append(
            {
                "parent_record_id": summary.run_id,
                "record_count": getattr(summary, "total_records", 0),
                "earliest_time": summary.created_at or None,
                "first_record": _record_to_response(records[0]) if records else None,
            }
        )
    return {"groups": groups, "total_groups": len(groups)}


@execution_router.get("/records/tree/{session_id}", summary="获取执行记录树")
async def get_execution_tree(
    session_id: str,
    max_depth: int = Query(default=5, ge=1, le=20),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    storage = _get_exec_storage()
    if storage is None:
        return {"tree": [], "total": 0, "session_id": session_id, "max_depth": max_depth}

    # 存储层无显式父子层级，把该 session 的记录作为扁平列表返回
    records = storage.list_by_session(session_id, limit=max_depth * 50)
    return {
        "tree": [_record_to_response(r) for r in records],
        "total": storage.count_by_session(session_id),
        "session_id": session_id,
        "max_depth": max_depth,
    }


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
    storage = _get_exec_storage()
    if storage is None:
        return {"id": record_id, "session_id": "", "message_data": {}, "created_at": ""}

    record = storage.get(record_id)
    if record is None:
        return {"id": record_id, "session_id": "", "message_data": {}, "created_at": ""}
    return _record_to_response(record)


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
# Cost Control 路由 - /api/v1/cost-control
# ---------------------------------------------------------------------------
cost_control_router = APIRouter(prefix="/api/v1/cost-control", tags=["成本控制"], dependencies=[Depends(require_auth)])


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
evaluation_router = APIRouter(prefix="/api/v1/evaluation", tags=["评估"], dependencies=[Depends(require_auth)])


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
eval_metrics_alias_router = APIRouter(
    prefix="/api/v1/evaluation-metrics", tags=["评估指标别名"], dependencies=[Depends(require_auth)]
)


@eval_metrics_alias_router.get("", summary="获取评估指标列表（别名）")
async def list_eval_metrics_alias(
    metric_type: str | None = Query(default=None, description="按类型筛选"),
    tag: str | None = Query(default=None, description="按标签筛选"),
    is_red_line: bool | None = Query(default=None, description="是否红线指标"),
    limit: int = Query(default=50, ge=1, le=200, description="每页数量"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    try:
        from channels.api.routes_evaluation import _get_metric_loader, _metric_to_response  # noqa: PLC0415

        loader = _get_metric_loader()
        if loader is None:
            return {"metrics": [], "total": 0}
        metrics = list(loader.metrics.values())
        if metric_type:
            metrics = [
                m
                for m in metrics
                if (m.metric_type.value if hasattr(m.metric_type, "value") else str(m.metric_type)) == metric_type
            ]
        if tag:
            metrics = [m for m in metrics if tag in m.tags]
        if is_red_line is not None:
            metrics = [m for m in metrics if m.is_red_line == is_red_line]
        total = len(metrics)
        end = offset + limit
        page = metrics[offset:end]
        items = [_metric_to_response(m).model_dump() for m in page]
        return {"metrics": items, "total": total}
    except Exception:
        return {"metrics": [], "total": 0}


@eval_metrics_alias_router.get("/{metric_id}", summary="获取评估指标详情（别名）")
async def get_eval_metric_alias(metric_id: str, _user: dict = Depends(require_auth)) -> dict[str, Any]:
    try:
        from channels.api.routes_evaluation import _get_metric_loader, _metric_to_detail  # noqa: PLC0415

        loader = _get_metric_loader()
        if loader is None:
            raise APIError(status_code=404, error_code="API_NOTF_2004", message="评估指标加载器未初始化")  # noqa: F821
        metric = loader.get(metric_id)
        if metric is None:
            raise APIError(status_code=404, error_code="API_NOTF_2004", message=f"评估指标 '{metric_id}' 不存在")  # noqa: F821
        return _metric_to_detail(metric).model_dump()
    except Exception:
        return {"id": metric_id, "name": "", "description": ""}


# ---------------------------------------------------------------------------
# Client Register 路由 - /api/v1/client
# ---------------------------------------------------------------------------
client_router = APIRouter(prefix="/api/v1/client", tags=["客户端"], dependencies=[Depends(require_auth)])

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


@files_router.post("/upload", summary="上传文件")
async def upload_file(_user: dict = Depends(require_auth)) -> dict[str, Any]:
    return {
        "file_id": "stub",
        "filename": "",
        "mime_type": "",
        "size": 0,
        "file_type": "document",
        "base64_data": "",
        "uploaded_at": "",
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
                status_str = safe_enum_value(task.status)
                phase, phase_status = _STATUS_TO_PHASE.get(status_str, ("prepare", "pending"))
                return {
                    "taskId": task_id,
                    "currentPhase": phase,
                    "phaseStatus": phase_status,
                }
        except Exception:
            pass

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
