"""
任务阶段 API 路由

提供任务阶段管理的 API 端点：
- 获取当前阶段状态
- 完成准备阶段
- 完成执行阶段
- 获取阶段产物
"""

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import generate_trace_id
from src.api.errors import create_error_response
from src.api.routes.auth import get_current_user
from src.api.schemas.tasks import (
    ExecutePhaseCompleteRequest,
    PhaseCompleteResponse,
    PhaseOutputResponse,
    PreparePhaseCompleteRequest,
    TaskPhaseStatusResponse,
)
from src.db.connection import get_async_session
from src.tasks.phase_controller import TaskPhaseController

router = APIRouter()


# ============================================================================
# 依赖注入
# ============================================================================


async def get_phase_controller(
    session: AsyncSession = Depends(get_async_session),
) -> TaskPhaseController:
    """
    获取任务阶段控制器实例

    Args:
        session: 数据库会话

    Returns:
        TaskPhaseController: 任务阶段控制器实例
    """
    return TaskPhaseController(session)


async def verify_task_access(
    task_id: str,
    user_id: str,
    session: AsyncSession,
) -> bool:
    """
    验证用户是否有权访问任务

    Args:
        task_id: 任务 ID
        user_id: 用户 ID
        session: 数据库会话

    Returns:
        是否有权访问
    """
    from sqlalchemy import select

    from src.db.models import Task

    result = await session.execute(select(Task.user_id).where(Task.id == task_id))
    task_user_id = result.scalar_one_or_none()

    if task_user_id is None:
        return False

    return task_user_id == user_id


# ============================================================================
# 路由
# ============================================================================


@router.get(
    "/{task_id}/phase",
    response_model=TaskPhaseStatusResponse,
    summary="获取当前阶段状态",
    description="获取任务当前的阶段状态和所有阶段信息",
)
async def get_task_phase_status(
    task_id: str,
    current_user=Depends(get_current_user),
    phase_controller: TaskPhaseController = Depends(get_phase_controller),
    session: AsyncSession = Depends(get_async_session),
) -> TaskPhaseStatusResponse:
    """获取任务阶段状态"""
    # 验证权限
    has_access = await verify_task_access(task_id, str(current_user.id), session)
    if not has_access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    result = await phase_controller.get_phase_status(task_id)

    if "error" in result:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TASK_PHASE_001",
            trace_id=trace_id,
            message=result.get("error", "获取失败"),
            path=f"/api/v1/tasks/{task_id}/phase",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )

    # 转换阶段状态
    from src.api.schemas.tasks import PhaseStatusInfo

    phases = {}
    for phase_name, phase_data in result.get("phases", {}).items():
        phases[phase_name] = PhaseStatusInfo(**phase_data)

    return TaskPhaseStatusResponse(
        task_id=result["task_id"],
        current_phase=result.get("current_phase"),
        task_status=result["task_status"],
        phases=phases,
    )


@router.post(
    "/{task_id}/phase/prepare/complete",
    response_model=PhaseCompleteResponse,
    summary="完成准备阶段",
    description="完成准备阶段，提交准备产物，进入执行阶段",
)
async def complete_prepare_phase(
    task_id: str,
    request: PreparePhaseCompleteRequest,
    current_user=Depends(get_current_user),
    phase_controller: TaskPhaseController = Depends(get_phase_controller),
    session: AsyncSession = Depends(get_async_session),
) -> PhaseCompleteResponse:
    """完成准备阶段"""
    # 验证权限
    has_access = await verify_task_access(task_id, str(current_user.id), session)
    if not has_access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    result = await phase_controller.complete_prepare_phase(
        task_id=task_id,
        output=request.output,
    )

    if "error" in result:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TASK_PHASE_002",
            trace_id=trace_id,
            message=result.get("error", "操作失败"),
            path=f"/api/v1/tasks/{task_id}/phase/prepare/complete",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error.model_dump(mode="json"),
        )

    return PhaseCompleteResponse(
        task_id=result["task_id"],
        current_phase=result["current_phase"],
        task_status="in_progress",
        completed_at=result["completed_at"],
    )


@router.post(
    "/{task_id}/phase/execute/complete",
    response_model=PhaseCompleteResponse,
    summary="完成执行阶段",
    description="完成执行阶段，系统自动进入评估阶段",
)
async def complete_execute_phase(
    task_id: str,
    request: ExecutePhaseCompleteRequest,
    current_user=Depends(get_current_user),
    phase_controller: TaskPhaseController = Depends(get_phase_controller),
    session: AsyncSession = Depends(get_async_session),
) -> PhaseCompleteResponse:
    """完成执行阶段"""
    # 验证权限
    has_access = await verify_task_access(task_id, str(current_user.id), session)
    if not has_access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    result = await phase_controller.complete_execute_phase(task_id)

    if "error" in result:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TASK_PHASE_003",
            trace_id=trace_id,
            message=result.get("error", "操作失败"),
            path=f"/api/v1/tasks/{task_id}/phase/execute/complete",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error.model_dump(mode="json"),
        )

    return PhaseCompleteResponse(
        task_id=result["task_id"],
        current_phase=result["current_phase"],
        task_status="evaluating",
        completed_at=result["completed_at"],
    )


@router.get(
    "/{task_id}/phase/{phase}/output",
    response_model=PhaseOutputResponse,
    summary="获取阶段产物",
    description="获取指定阶段的产物信息",
)
async def get_phase_output(
    task_id: str,
    phase: str = Path(..., description="阶段名称: prepare/execute/evaluate"),
    current_user=Depends(get_current_user),
    phase_controller: TaskPhaseController = Depends(get_phase_controller),
    session: AsyncSession = Depends(get_async_session),
) -> PhaseOutputResponse:
    """获取阶段产物"""
    # 验证权限
    has_access = await verify_task_access(task_id, str(current_user.id), session)
    if not has_access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    # 验证阶段名称
    valid_phases = ["prepare", "execute", "evaluate"]
    if phase not in valid_phases:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的阶段名称，必须是: {', '.join(valid_phases)}",
        )

    result = await phase_controller.get_phase_output(task_id, phase)

    if "error" in result:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TASK_PHASE_004",
            trace_id=trace_id,
            message=result.get("error", "获取失败"),
            path=f"/api/v1/tasks/{task_id}/phase/{phase}/output",
        )
        status_code = status.HTTP_400_BAD_REQUEST
        if result.get("error_code") == "PHASE_NOT_COMPLETED":
            status_code = status.HTTP_425_TOO_EARLY

        raise HTTPException(
            status_code=status_code, detail=error.model_dump(mode="json")
        )

    return PhaseOutputResponse(
        task_id=result["task_id"],
        phase=result["phase"],
        status=result["status"],
        output=result.get("output"),
        start_time=result.get("start_time"),
        end_time=result.get("end_time"),
    )


@router.post(
    "/{task_id}/phase/evaluate/complete",
    response_model=PhaseCompleteResponse,
    summary="完成评估阶段",
    description="完成评估阶段，提交评估结果",
)
async def complete_evaluate_phase(
    task_id: str,
    request: dict,
    current_user=Depends(get_current_user),
    phase_controller: TaskPhaseController = Depends(get_phase_controller),
    session: AsyncSession = Depends(get_async_session),
) -> PhaseCompleteResponse:
    """完成评估阶段"""
    # 验证权限
    has_access = await verify_task_access(task_id, str(current_user.id), session)
    if not has_access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    # 提取评估结果
    eval_result = request.get("eval_result", {})

    result = await phase_controller.complete_evaluate_phase(
        task_id=task_id,
        eval_result=eval_result,
    )

    if "error" in result:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TASK_PHASE_005",
            trace_id=trace_id,
            message=result.get("error", "操作失败"),
            path=f"/api/v1/tasks/{task_id}/phase/evaluate/complete",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error.model_dump(mode="json"),
        )

    return PhaseCompleteResponse(
        task_id=result["task_id"],
        current_phase=result.get("current_phase"),
        task_status=result["task_status"],
        completed_at=result["completed_at"],
    )
