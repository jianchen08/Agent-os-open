"""任务管理 API 路由。

提供任务的 CRUD 操作、提交和评估接口。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, Query

from channels.api.deps import APIError, require_auth, validate_pagination
from channels.api.models import (
    TaskCreate,
    TaskEvaluateRequest,
    TaskEvaluateResponse,
    TaskListResponse,
    TaskResponse,
    TaskSubmitResponse,
    TaskUpdate,
    store,
)

logger = logging.getLogger(__name__)

# FastAPI 在模块级别使用 -> 注解时需要 APIRouter 实例
from fastapi import APIRouter  # noqa: E402

router = APIRouter(prefix="/api/v1/tasks", tags=["任务"])


def _get_task_service() -> Any:
    """获取全局 TaskService 实例（来自 TaskStorage/YAML 文件）。

    BUG-FIX-fix_20260506_007: 补充从 TaskStorage 获取管道引擎创建的任务
    问题根因: routes_tasks.py 只查询 api_store（内存 dict），
              管道引擎通过 TaskService → TaskStorage → YAML 文件管理任务，
              两者完全独立，导致 API 返回空列表
    修复方案: 合并 api_store 和 TaskStorage 的数据源
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


def _task_model_to_dict(task_model: Any) -> dict[str, Any]:
    """将 TaskModel dataclass 转为字典。"""
    from dataclasses import asdict
    d = asdict(task_model)
    d["status"] = task_model.status.value if hasattr(task_model.status, "value") else str(task_model.status)
    if hasattr(task_model, "priority") and hasattr(task_model.priority, "value"):
        d["priority"] = task_model.priority.value
    return d


def _task_to_response(t: dict[str, Any]) -> TaskResponse:
    """将存储层任务字典转为 TaskResponse。"""
    return TaskResponse(
        id=t["id"],
        title=t["title"],
        description=t.get("description"),
        status=t.get("status", "pending"),
        priority=t.get("priority", 5),
        agent_id=t.get("agent_id"),
        thread_id=t.get("thread_id"),
        created_by=t.get("created_by"),
        tags=t.get("tags", []),
        input_data=t.get("input_data", {}),
        result=t.get("result"),
        created_at=t.get("created_at", ""),
        updated_at=t.get("updated_at", ""),
    )


@router.get(
    "",
    response_model=TaskListResponse,
    summary="获取任务列表",
)
# BUG-FIX-fix_20260512_async_list_all: 改为 async def 以支持 await task_service.list_all()
async def list_tasks(
    status: str | None = Query(default=None, description="按状态筛选"),
    priority: int | None = Query(
        default=None, ge=1, le=9, description="按优先级筛选",
    ),
    session_id: str | None = Query(default=None, description="按会话 ID 筛选"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _user: dict = Depends(require_auth),
) -> TaskListResponse:
    """获取当前用户的任务列表。

    支持按状态、优先级和会话 ID 筛选，分页返回。
    合并 api_store 和 TaskStorage（YAML 文件）两个数据源。
    session_id 筛选基于 task.metadata["session_id"] 字段匹配。

    Returns:
        TaskListResponse 包含 items 和 total
    """
    validate_pagination(limit, offset)
    tasks = store.get_user_tasks(_user["sub"])

    task_service = _get_task_service()
    if task_service is not None:
        try:
            # BUG-FIX-fix_20260512_async_list_all: 添加 await
            # BUG-FIX-fix_20260512_session_filter: 传递 session_id 到 list_all 减少不必要数据获取
            ts_tasks = await task_service.list_all(limit=1000, session_id=session_id)
            api_ids = {t["id"] for t in tasks}
            for tm in ts_tasks:
                if tm.id not in api_ids:
                    tasks.append(_task_model_to_dict(tm))
        except Exception as exc:
            logger.warning("从 TaskStorage 加载任务失败: %s", exc)

    # BUG-FIX-fix_20260512_session_filter: 按 session_id 过滤 api_store 来源的任务
    # 问题根因: 前端 FileTreeWidget 已传递 session_id 参数，
    #           但后端 API 未按此参数筛选，导致所有会话的任务混在一起显示。
    # 修复方案: TaskStorage 来源的任务已在 list_all 中过滤，
    #           这里仅过滤 api_store 来源的任务。
    # 影响范围: list_tasks API 返回的任务列表。
    # 修复日期: 2026-05-12
    if session_id:
        tasks = [t for t in tasks if t.get("metadata", {}).get("session_id") == session_id]

    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    if priority is not None:
        tasks = [t for t in tasks if t.get("priority") == priority]

    total = len(tasks)
    end = offset + limit
    page = tasks[offset:end]
    items = [_task_to_response(t) for t in page]
    return TaskListResponse(items=items, total=total)


@router.get(
    "/debug/all",
    summary="获取任务调试数据（全字段）",
)
# BUG-FIX-fix_20260512_async_list_all: 改为 async def 以支持 await task_service.list_all()
async def get_tasks_debug(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc"),
    status: str | None = Query(default=None),
    session_id: str | None = Query(default=None, description="按会话 ID 筛选"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """获取任务调试数据（全字段）。

    支持按状态和会话 ID 筛选，返回全字段数据用于调试。

    Returns:
        包含 items 和 total 的字典
    """
    task_service = _get_task_service()
    if task_service is None:
        return {"items": [], "total": 0}
    try:
        # BUG-FIX-fix_20260512_async_list_all: 添加 await
        all_tasks = await task_service.list_all(limit=limit, reverse=(sort_order == "desc"))
        if status:
            all_tasks = [t for t in all_tasks if t.status.value == status]
        # BUG-FIX-fix_20260512_session_filter: 按 session_id 过滤
        if session_id:
            all_tasks = [t for t in all_tasks if t.metadata.get("session_id") == session_id]
        items = [_task_model_to_dict(t) for t in all_tasks]
        return {"items": items, "total": len(items)}
    except Exception:
        return {"items": [], "total": 0}


@router.post(
    "",
    response_model=TaskResponse,
    status_code=201,
    summary="创建任务",
)
def create_task(
    body: TaskCreate,
    _user: dict = Depends(require_auth),
) -> TaskResponse:
    """创建新任务。

    Args:
        body: 任务创建请求

    Returns:
        TaskResponse 新创建的任务
    """
    task = store.create_task(
        user_id=_user["sub"],
        title=body.title,
        description=body.description,
        agent_id=body.agent_id,
        priority=body.priority,
        tags=body.tags,
        input_data=body.input_data,
    )
    logger.info("用户 %s 创建任务: %s", _user.get("username"), task["id"])
    return _task_to_response(task)


@router.get(
    "/{task_id}",
    response_model=TaskResponse,
    summary="获取任务详情",
)
def get_task(
    task_id: str,
    _user: dict = Depends(require_auth),
) -> TaskResponse:
    """获取指定任务的详情。

    Args:
        task_id: 任务 ID

    Returns:
        TaskResponse 任务详情

    Raises:
        APIError: 任务不存在 (404)
    """
    task = store.get_task(task_id)
    if task is None:
        task_service = _get_task_service()
        if task_service is not None:
            tm = task_service.get_task(task_id)
            if tm is not None:
                task = _task_model_to_dict(tm)
    if task is None:
        raise APIError(
            status_code=404,
            error_code="TASK_001",
            message="任务不存在或已被删除",
        )
    return _task_to_response(task)


@router.patch(
    "/{task_id}",
    response_model=TaskResponse,
    summary="更新任务",
)
def update_task(
    task_id: str,
    body: TaskUpdate,
    _user: dict = Depends(require_auth),
) -> TaskResponse:
    """更新指定任务的字段。

    Args:
        task_id: 任务 ID
        body: 任务更新请求（仅传入需要更新的字段）

    Returns:
        TaskResponse 更新后的任务

    Raises:
        APIError: 任务不存在 (404)
    """
    task = store.update_task(
        task_id,
        title=body.title,
        description=body.description,
        status=body.status,
        priority=body.priority,
        tags=body.tags,
    )
    if task is None:
        raise APIError(
            status_code=404,
            error_code="TASK_001",
            message="任务不存在或已被删除",
        )
    return _task_to_response(task)


@router.delete(
    "/{task_id}",
    summary="删除任务",
)
def delete_task(
    task_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, str]:
    """删除指定任务。

    Args:
        task_id: 任务 ID

    Returns:
        删除成功消息

    Raises:
        APIError: 任务不存在 (404)
    """
    deleted = store.delete_task(task_id)
    if not deleted:
        raise APIError(
            status_code=404,
            error_code="TASK_001",
            message="任务不存在或已被删除",
        )
    return {"message": "任务已删除"}


@router.post(
    "/{task_id}/submit",
    response_model=TaskSubmitResponse,
    summary="提交任务执行",
)
def submit_task(
    task_id: str,
    _user: dict = Depends(require_auth),
) -> TaskSubmitResponse:
    """提交任务进入执行队列。

    将任务状态从 pending 变为 queued，等待调度器分配执行。

    Args:
        task_id: 任务 ID

    Returns:
        TaskSubmitResponse 包含 task_id 和状态

    Raises:
        APIError: 任务不存在 (404) 或状态不允许 (400)
    """
    task = store.get_task(task_id)
    if task is None:
        raise APIError(
            status_code=404,
            error_code="TASK_001",
            message="任务不存在或已被删除",
        )

    current_status = task.get("status", "pending")
    allowed_statuses = {"pending", "failed"}
    if current_status not in allowed_statuses:
        raise APIError(
            status_code=400,
            error_code="TASK_002",
            message=f"当前状态 '{current_status}' 不允许提交，"
            f"仅允许: {', '.join(allowed_statuses)}",
        )

    store.update_task(task_id, status="queued")
    logger.info("用户 %s 提交任务 %s 执行", _user.get("username"), task_id)

    return TaskSubmitResponse(
        task_id=task_id,
        status="queued",
        message="任务已提交到执行队列",
    )


@router.post(
    "/{task_id}/evaluate",
    response_model=TaskEvaluateResponse,
    summary="评估任务",
)
def evaluate_task(
    task_id: str,
    body: TaskEvaluateRequest | None = None,
    _user: dict = Depends(require_auth),
) -> TaskEvaluateResponse:
    """对指定任务执行评估。

    根据指定的评估指标对任务结果进行自动化评估。
    如果未指定 metric_ids，则执行任务关联 Agent 的推荐指标。

    Args:
        task_id: 任务 ID
        body: 评估请求（可选，默认执行所有推荐指标）

    Returns:
        TaskEvaluateResponse 评估结果

    Raises:
        APIError: 任务不存在 (404)
    """
    task = store.get_task(task_id)
    if task is None:
        raise APIError(
            status_code=404,
            error_code="TASK_001",
            message="任务不存在或已被删除",
        )

    metric_ids = []
    if body:
        metric_ids = body.metric_ids

    # 尝试使用评估引擎
    try:
        from evaluation.loader import MetricLoader
        loader = MetricLoader()
        loader.load_all()

        # 如果未指定指标，尝试从关联 Agent 获取推荐指标
        if not metric_ids:
            agent_id = task.get("agent_id")
            if agent_id:
                reg = _get_agent_registry()
                if reg:
                    agent_cfg = reg.get(agent_id)
                    if agent_cfg:
                        metric_ids = [
                            m.metric_id
                            for m in agent_cfg.recommended_metrics
                        ]

        # 如果仍无指标，加载所有
        if not metric_ids:
            metric_ids = loader.list_metrics()

        results: list[dict[str, Any]] = []
        for mid in metric_ids:
            metric_def = loader.get(mid)
            if metric_def is None:
                continue
            results.append({
                "metric_id": mid,
                "name": metric_def.name,
                "status": "skipped",
                "message": "评估引擎未连接（API 模式下暂不支持自动执行）",
                "passed": None,
            })

        return TaskEvaluateResponse(
            task_id=task_id,
            overall_passed=False,
            summary=f"共 {len(results)} 个指标待评估（需连接评估引擎）",
            results=results,
        )

    except Exception as exc:
        logger.warning("评估引擎加载失败: %s", exc)
        return TaskEvaluateResponse(
            task_id=task_id,
            overall_passed=False,
            summary="评估引擎不可用",
            results=[],
        )


def _cancel_running_pipeline(task_id: str) -> bool:
    """取消任务关联的运行中管道（best-effort）。

    通过 TaskWorker.cancel_pipeline 强制取消 asyncio.Task，
    触发 PipelineEngine 的 CancelledError，真正停止执行。

    Args:
        task_id: 任务 ID

    Returns:
        是否成功取消了运行中的管道
    """
    try:
        from infrastructure.service_provider import get_service_provider
        task_worker = get_service_provider().get("task_worker")
        if task_worker is None:
            return False
        return task_worker.cancel_pipeline(task_id)
    except Exception:
        return False


@router.post(
    "/{task_id}/pause",
    summary="暂停任务",
)
async def pause_task(
    task_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """暂停指定任务，同时取消正在运行的 PipelineEngine。

    执行两步操作：
    1. 将任务状态从 running/pending 变为 paused（持久化到 YAML）
    2. 取消该任务关联的 PipelineEngine 协程（真正停止 LLM 调用）

    重启后 paused 状态的任务不会被 TaskWorker 自动恢复执行。

    Args:
        task_id: 任务 ID

    Returns:
        暂停成功消息

    Raises:
        APIError: TaskService 不可用 (503)、任务不存在 (404) 或状态不允许 (400)
    """
    task_service = _get_task_service()
    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="TASK_003",
            message="TaskService 不可用，无法暂停任务",
        )

    try:
        # BUG-FIX-fix_20260512_async_compat: pause_task 现在是 async
        await task_service.pause_task(task_id)
    except KeyError:
        raise APIError(
            status_code=404,
            error_code="TASK_001",
            message=f"任务不存在: {task_id}",
        )
    except Exception as exc:
        from tasks.state_machine import InvalidTransitionError
        if isinstance(exc, InvalidTransitionError):
            raise APIError(
                status_code=400,
                error_code="TASK_002",
                message=str(exc),
            )
        raise APIError(
            status_code=500,
            error_code="TASK_099",
            message=f"暂停任务失败: {exc}",
        )

    pipeline_cancelled = _cancel_running_pipeline(task_id)

    logger.info(
        "用户 %s 暂停任务 %s (pipeline_cancelled=%s)",
        _user.get("username"), task_id, pipeline_cancelled,
    )
    return {
        "success": True,
        "task_id": task_id,
        "paused_count": 1,
        "pipeline_cancelled": pipeline_cancelled,
        "message": "任务已暂停" + ("，运行中管道已取消" if pipeline_cancelled else ""),
    }


@router.post(
    "/{task_id}/resume",
    summary="恢复任务",
)
async def resume_task(
    task_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """恢复指定暂停的任务，同时重新触发 PipelineEngine 执行。

    执行两步操作：
    1. 将任务状态从 paused 变为 pending
    2. 发布 task.submitted 事件，触发 TaskWorker 重新执行任务

    Args:
        task_id: 任务 ID

    Returns:
        恢复成功消息

    Raises:
        APIError: TaskService 不可用 (503)、任务不存在 (404) 或状态不允许 (400)
    """
    task_service = _get_task_service()
    if task_service is None:
        raise APIError(
            status_code=503,
            error_code="TASK_003",
            message="TaskService 不可用，无法恢复任务",
        )

    try:
        # BUG-FIX-fix_20260512_async_compat: resume_task 现在是 async
        task = await task_service.resume_task(task_id)
    except KeyError:
        raise APIError(
            status_code=404,
            error_code="TASK_001",
            message=f"任务不存在: {task_id}",
        )
    except Exception as exc:
        from tasks.state_machine import InvalidTransitionError
        if isinstance(exc, InvalidTransitionError):
            raise APIError(
                status_code=400,
                error_code="TASK_002",
                message=str(exc),
            )
        raise APIError(
            status_code=500,
            error_code="TASK_099",
            message=f"恢复任务失败: {exc}",
        )

    task_submitted = _submit_task_event(task_id, task_service)

    logger.info(
        "用户 %s 恢复任务 %s (task_submitted=%s)",
        _user.get("username"), task_id, task_submitted,
    )
    return {
        "success": True,
        "task_id": task_id,
        "resumed_count": 1,
        "task_submitted": task_submitted,
        "message": "任务已恢复" + ("，已重新提交执行" if task_submitted else ""),
    }


def _submit_task_event(task_id: str, task_service: Any) -> bool:
    """发布 task.submitted 事件，触发 TaskWorker 重新执行任务。

    从 TaskService 获取任务的完整信息，构建事件数据，
    通过 EventBus 发布 task.submitted 事件。
    TaskWorker 订阅该事件后会创建后台协程执行 PipelineEngine。

    Args:
        task_id: 任务 ID
        task_service: TaskService 实例

    Returns:
        是否成功发布了事件
    """
    try:
        task = task_service.get_task(task_id)
        if task is None:
            return False

        metadata = task.metadata or {}
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()

        event_bus = provider.get("event_bus")
        if event_bus is None:
            logger.warning("_submit_task_event: EventBus 不可用")
            return False

        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(event_bus.emit("task.submitted", {
                "task_id": task.id,
                "target_type": task.target_type or "agent",
                "target_id": metadata.get("target_id", ""),
                "user_input": task.title,
                "description": task.description,
                "acceptance_criteria": metadata.get("acceptance_criteria", {}),
                "workspace": metadata.get("workspace", ""),
            }))
        else:
            loop.run_until_complete(event_bus.emit("task.submitted", {
                "task_id": task.id,
                "target_type": task.target_type or "agent",
                "target_id": metadata.get("target_id", ""),
                "user_input": task.title,
                "description": task.description,
                "acceptance_criteria": metadata.get("acceptance_criteria", {}),
                "workspace": metadata.get("workspace", ""),
            }))

        return True
    except Exception as exc:
        logger.warning("_submit_task_event: 发布事件失败: task_id=%s, error=%s", task_id, exc)
        return False


def _get_agent_registry() -> Any:
    """惰性获取 Agent 注册表。"""
    try:
        from agents.registry import AgentRegistry
        if AgentRegistry.has_instance():
            return AgentRegistry.get_instance()
    except ImportError:
        pass
    return None
