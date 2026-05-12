"""
任务管理 API 路由

支持新的数据模型：
- 使用 evaluation_metric_ids 替代 acceptance_criteria
- 关联 ExecutionRecord 记录执行过程
- 支持可复用的评估指标
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_current_user
from src.db.connection import get_async_session
from src.db.models import Task, User
from src.services.task_service import TaskService

router = APIRouter(tags=["tasks"])


# ============================================================================
# Pydantic 请求/响应模型
# ============================================================================


class TaskCreateRequest(BaseModel):
    """创建任务请求"""

    title: str = Field(..., description="任务标题", min_length=1, max_length=255)
    description: str | None = Field(None, description="任务描述")
    goal: dict[str, Any] | None = Field(None, description="任务目标")
    agent_id: str | None = Field(None, description="执行者 ID")
    priority: str = Field("medium", description="任务优先级: low | medium | high")
    evaluation_metric_ids: list[str] | None = Field(
        default_factory=list, description="评估指标 ID 列表"
    )
    acceptance_criteria: dict[str, Any] | None = Field(
        None, description="验收标准（评估器的输入和断言）"
    )
    parent_task_id: str | None = Field(None, description="父任务 ID")
    tags: list[str] | None = Field(
        default_factory=list, description="标签列表（用于分类和检索）"
    )


class TaskUpdateRequest(BaseModel):
    """更新任务请求"""

    title: str | None = Field(
        None, description="任务标题", min_length=1, max_length=255
    )
    description: str | None = Field(None, description="任务描述")
    status: str | None = Field(None, description="任务状态")
    goal: dict[str, Any] | None = Field(None, description="任务目标")
    priority: str | None = Field(None, description="任务优先级")
    current_phase: str | None = Field(None, description="当前阶段")
    phase_status: dict[str, Any] | None = Field(None, description="阶段状态")
    error_message: str | None = Field(None, description="错误信息")
    evaluation_metric_ids: list[str] | None = Field(
        None, description="评估指标 ID 列表"
    )
    tags: list[str] | None = Field(None, description="标签列表")


class EvaluationMetricInfo(BaseModel):
    """评估指标信息"""

    id: str = Field(..., description="指标 ID")
    name: str = Field(..., description="指标名称")
    description: str = Field(..., description="指标描述")
    category: str = Field(..., description="指标分类")
    evaluator_type: str = Field(..., description="评估器类型")
    evaluator_id: str = Field(..., description="评估器 ID")


class TaskResponse(BaseModel):
    """任务响应"""

    id: str = Field(..., description="任务 ID")
    title: str = Field(..., description="任务标题")
    description: str | None = Field(None, description="任务描述")
    agent_id: str | None = Field(None, description="执行者 ID")
    priority: Any = Field("medium", description="任务优先级")
    status: str = Field(..., description="任务状态")
    created_at: str = Field(..., description="创建时间")
    updated_at: str | None = Field(None, description="更新时间")
    parent_task_id: str | None = Field(None, description="父任务 ID")
    session_id: str | None = Field(None, description="关联会话 ID")
    user_id: str | None = Field(None, description="所属用户 ID")
    goal: dict[str, Any] | None = Field(None, description="任务目标")
    evaluation_metric_ids: list[str] | None = Field(
        default_factory=list, description="评估指标 ID 列表"
    )
    evaluation_metrics: list[EvaluationMetricInfo] | None = Field(
        default_factory=list, description="评估指标详情"
    )
    execution_record_id: str | None = Field(None, description="执行记录 ID")
    current_phase: str | None = Field(None, description="当前阶段")
    phase_status: dict[str, Any] | None = Field(None, description="阶段状态")
    total_criteria: int = Field(0, description="总指标数")
    passed_criteria: int = Field(0, description="通过指标数")
    failed_criteria: int = Field(0, description="失败指标数")
    progress_percent: float = Field(0.0, description="进度百分比")
    task_type: str | None = Field(None, description="任务类型")
    agent_level: int | None = Field(None, description="Agent 层级")
    tags: list[str] | None = Field(default_factory=list, description="标签列表")
    subtasks: list["TaskResponse"] | None = Field(
        default_factory=list, description="子任务列表"
    )


class TaskDetailResponse(TaskResponse):
    """任务详情响应"""

    task_metrics: list[dict[str, Any]] | None = Field(
        default_factory=list, description="任务指标评估状态"
    )


class EvaluationStatusResponse(BaseModel):
    """评估状态响应"""

    task_id: str = Field(..., description="任务 ID")
    total_metrics: int = Field(..., description="总指标数")
    pending_metrics: int = Field(..., description="待评估指标数")
    passed_metrics: int = Field(..., description="通过指标数")
    failed_metrics: int = Field(..., description="失败指标数")
    skipped_metrics: int = Field(..., description="跳过指标数")
    progress_percent: float = Field(..., description="进度百分比")
    metrics: list[dict[str, Any]] = Field(..., description="指标状态列表")


# 解决循环引用
TaskResponse.model_rebuild()


# ============================================================================
# API 端点
# ============================================================================


@router.post("/", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    task_data: TaskCreateRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """创建新任务

    支持新的数据模型：
    - evaluation_metric_ids: 评估指标 ID 列表（引用可复用指标）
    - 自动关联执行记录记录执行过程
    """
    task_service = TaskService(db)
    # 转换为字典格式，TaskService 期望字典输入
    task_dict = task_data.model_dump(exclude_unset=True)
    result = await task_service.create_task(task_dict, str(current_user.id))
    return result


@router.get("/", response_model=list[TaskResponse])
async def list_tasks(
    skip: int = Query(0, ge=0, description="跳过数量"),
    limit: int = Query(100, ge=1, le=100, description="限制数量"),
    root_only: bool = Query(False, description="是否只返回根任务"),
    include_subtasks: bool = Query(True, description="是否包含子任务"),
    status: str | None = Query(None, description="按状态过滤"),
    session_id: str | None = Query(
        None, description="会话ID过滤（只返回该会话的任务）"
    ),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """获取任务列表

    返回任务的评估指标信息，支持按状态过滤。
    如果提供 session_id，则只返回该会话中的任务。
    """
    task_service = TaskService(db)
    filters = {"status": status} if status else None
    if session_id:
        filters = filters or {}
        filters["session_id"] = session_id
    return await task_service.list_tasks(
        current_user.id,
        skip,
        limit,
        include_subtasks=include_subtasks,
        root_only=root_only,
        filters=filters,
    )


@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: str,
    session_id: str | None = Query(None, description="会话ID（用于验证权限）"),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """获取任务详情

    联表查询评估指标，返回完整的任务信息。
    只能查看自己创建的任务。
    如果提供 session_id，则只能查看该会话中的任务。
    """
    task_service = TaskService(db)
    task = await task_service.get_task(
        task_id, str(current_user.id), session_id=session_id
    )
    if not task:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在或无权访问"
        )
    return task


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: str,
    task_data: TaskUpdateRequest,
    session_id: str | None = Query(None, description="会话ID（用于验证权限）"),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """更新任务

    支持 evaluation_metric_ids 更新。
    只有任务创建者才能更新任务。
    如果提供 session_id，则只能更新该会话中的任务。
    """
    from fastapi import HTTPException

    task_service = TaskService(db)
    # 转换为字典格式
    update_dict = task_data.model_dump(exclude_unset=True, exclude_none=True)
    if not update_dict:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="没有提供更新字段"
        )
    task = await task_service.update_task(
        task_id, update_dict, str(current_user.id), session_id=session_id
    )
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在或无权访问"
        )
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: str,
    session_id: str | None = Query(None, description="会话ID（用于验证权限）"),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """删除任务

    只有任务创建者才能删除任务。
    如果提供 session_id，则只能删除该会话中的任务。
    """
    from fastapi import HTTPException

    task_service = TaskService(db)
    success = await task_service.delete_task(
        task_id, str(current_user.id), session_id=session_id
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在或无权访问"
        )


@router.get("/{task_id}/evaluation-status", response_model=EvaluationStatusResponse)
async def get_evaluation_status(
    task_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """查询任务评估状态

    返回任务的评估进度和指标状态详情。

    注意：当前实现中没有独立的 task_metrics 关联表，
    任务通过 evaluation_metric_ids JSON 数组字段引用评估指标。
    """
    from src.core.exceptions import NotFoundException

    task_service = TaskService(db)
    try:
        result = await task_service.get_evaluation_status(task_id, str(current_user.id))
        return EvaluationStatusResponse(**result)
    except NotFoundException as e:
        from fastapi import HTTPException

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=e.message)


@router.post("/check-timeout")
async def check_timeout_tasks(
    db: AsyncSession = Depends(get_async_session),
):
    """
    检查超时任务

    由定时触发器调用，检查并处理超时的任务
    """
    task_service = TaskService(db)
    return await task_service.check_timeout_tasks()


class TaskStartResponse(BaseModel):
    """任务启动响应"""

    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="响应消息")
    task_id: str | None = Field(None, description="任务 ID")
    project_id: str | None = Field(None, description="项目 ID")
    status: str | None = Field(None, description="任务状态")


@router.post(
    "/{task_id}/start", response_model=TaskStartResponse, status_code=status.HTTP_200_OK
)
async def start_task_execution(
    task_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """
    手动启动任务执行

    通过统一调度入口 scheduler.schedule() 触发任务执行。

    Args:
        task_id: 任务 ID
        db: 数据库会话
        current_user: 当前用户

    Returns:
        任务启动响应
    """
    from fastapi import HTTPException
    from sqlalchemy import select

    from src.db.models import Task
    from src.orchestration import schedule as schedule_task

    try:
        result = await db.execute(
            select(Task).where(Task.id == task_id, Task.user_id == str(current_user.id))
        )
        task = result.scalar_one_or_none()

        if not task:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在或无权访问"
            )

        if task.status not in ["pending", "failed"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"任务状态为 {task.status}，只有 pending 或 failed 状态的任务可以启动",
            )

        project_id = task.parent_task_id if task.parent_task_id else task_id

        schedule_result = await schedule_task(task_id)

        if schedule_result.get("success"):
            return TaskStartResponse(
                success=True,
                message="任务已启动",
                task_id=task_id,
                project_id=project_id,
                status="running",
            )
        else:
            error = schedule_result.get("error", "未知错误")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"启动任务失败: {error}",
            )

    except HTTPException:
        raise
    except Exception as e:
        import logging

        logging.exception(
            f"[start_task_execution] 启动任务失败 | task_id={task_id} | error={e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"启动任务失败: {str(e)}",
        )


# ============================================================================
# 调试 API 端点
# ============================================================================


class TaskDebugResponse(BaseModel):
    """任务调试响应 - 包含所有原始字段"""

    id: str = Field(..., description="任务 ID")
    parent_task_id: str | None = Field(None, description="父任务 ID")
    execution_record_id: str | None = Field(None, description="执行记录 ID")
    user_id: str | None = Field(None, description="用户 ID")
    session_id: str | None = Field(None, description="会话 ID")
    title: str = Field(..., description="任务标题")
    goal: dict[str, Any] | None = Field(None, description="任务目标")
    target_type: str | None = Field(None, description="目标类型")
    target_id: str | None = Field(None, description="目标 ID")
    target_name: str | None = Field(None, description="目标名称")
    priority: int = Field(5, description="优先级")
    dependencies: list[str] | None = Field(None, description="依赖任务 ID 列表")
    due_date: str | None = Field(None, description="截止日期")
    retry_count: int = Field(0, description="重试次数")
    max_retries: int = Field(3, description="最大重试次数")
    evaluation_metric_ids: list[str] | None = Field(
        None, description="评估指标 ID 列表"
    )
    status: str = Field("pending", description="任务状态")
    started_at: str | None = Field(None, description="开始时间")
    completed_at: str | None = Field(None, description="完成时间")
    created_at: str = Field(..., description="创建时间")
    updated_at: str = Field(..., description="更新时间")
    metadata: dict[str, Any] | None = Field(None, description="元数据")
    tags: list[str] | None = Field(None, description="标签列表")


class TaskDebugListResponse(BaseModel):
    """任务调试列表响应"""

    items: list[TaskDebugResponse] = Field(default_factory=list, description="任务列表")
    total: int = Field(0, description="总数量")


@router.get("/debug/all", response_model=TaskDebugListResponse)
async def get_tasks_debug(
    skip: int = Query(0, ge=0, description="跳过数量"),
    limit: int = Query(100, ge=1, le=500, description="限制数量"),
    sort_by: str = Query("created_at", description="排序字段"),
    sort_order: str = Query("desc", description="排序方向: asc | desc"),
    status_filter: str | None = Query(None, alias="status", description="状态过滤"),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """
    调试用：获取任务全字段数据

    功能：
    - 返回所有原始字段，不做任何转换
    - 支持按任意字段排序
    - 支持按状态过滤
    - 不经过业务层处理，直接查询数据库
    """
    query = select(Task)

    if status_filter:
        query = query.where(Task.status == status_filter)

    order_func = desc if sort_order.lower() == "desc" else asc
    sort_column = getattr(Task, sort_by, Task.created_at)
    query = query.order_by(order_func(sort_column))

    query = query.offset(skip).limit(limit)

    result = await db.execute(query)
    tasks = result.scalars().all()

    count_query = select(Task)
    if status_filter:
        count_query = count_query.where(Task.status == status_filter)
    count_result = await db.execute(count_query)
    total = len(count_result.scalars().all())

    def task_to_debug_response(task: Task) -> TaskDebugResponse:
        return TaskDebugResponse(
            id=task.id,
            parent_task_id=task.parent_task_id,
            execution_record_id=task.execution_record_id,
            user_id=task.user_id,
            session_id=task.session_id,
            title=task.title,
            goal=task.goal,
            target_type=task.target_type,
            target_id=task.target_id,
            target_name=task.target_name,
            priority=task.priority,
            dependencies=task.dependencies,
            due_date=task.due_date.isoformat() if task.due_date else None,
            retry_count=task.retry_count,
            max_retries=task.max_retries,
            evaluation_metric_ids=task.evaluation_metric_ids,
            status=task.status,
            started_at=task.started_at.isoformat() if task.started_at else None,
            completed_at=task.completed_at.isoformat() if task.completed_at else None,
            created_at=task.created_at.isoformat() if task.created_at else "",
            updated_at=task.updated_at.isoformat() if task.updated_at else "",
            metadata=task.task_metadata,
            tags=task.tags,
        )

    return TaskDebugListResponse(
        items=[task_to_debug_response(t) for t in tasks],
        total=total,
    )
