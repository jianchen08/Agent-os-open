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
def list_tasks(
    status: str | None = Query(default=None, description="按状态筛选"),
    priority: int | None = Query(
        default=None, ge=1, le=9, description="按优先级筛选",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _user: dict = Depends(require_auth),
) -> TaskListResponse:
    """获取当前用户的任务列表。

    支持按状态和优先级筛选，分页返回。

    Returns:
        TaskListResponse 包含 items 和 total
    """
    validate_pagination(limit, offset)
    tasks = store.get_user_tasks(_user["sub"])

    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    if priority is not None:
        tasks = [t for t in tasks if t.get("priority") == priority]

    total = len(tasks)
    end = offset + limit
    page = tasks[offset:end]
    items = [_task_to_response(t) for t in page]
    return TaskListResponse(items=items, total=total)


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


def _get_agent_registry() -> Any:
    """惰性获取 Agent 注册表。"""
    try:
        from agents.registry import AgentRegistry
        if AgentRegistry.has_instance():
            return AgentRegistry.get_instance()
    except ImportError:
        pass
    return None
