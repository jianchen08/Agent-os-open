"""
任务评估 API 路由

提供 AC（验收标准）评估的 API 端点：
- 获取任务所有 AC 状态
- 评估单个 AC
- 获取 AC 评估结果
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import generate_trace_id
from src.api.errors import create_error_response
from src.api.routes.auth import get_current_user
from src.api.schemas.tasks import (
    AcceptanceCriterionStatus,
    ACEvaluateRequest,
    ACEvaluationResult,
    TaskACListResponse,
    TaskACResultResponse,
)
from src.db.connection import get_async_session
from src.tasks.ac_evaluator import ACEvaluator
from src.tasks.services.evaluation_service import EvaluationService

router = APIRouter()


# ============================================================================
# 依赖注入
# ============================================================================


async def get_ac_evaluator(
    session: AsyncSession = Depends(get_async_session),
) -> ACEvaluator:
    """
    获取 AC 评估器实例

    Args:
        session: 数据库会话

    Returns:
        ACEvaluator: AC 评估器实例
    """
    return ACEvaluator(session)


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
    "/{task_id}/ac",
    response_model=TaskACListResponse,
    summary="获取所有 AC 状态",
    description="获取任务的所有验收标准及其状态",
)
async def get_task_ac_list(
    task_id: str,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> TaskACListResponse:
    """获取任务所有 AC 状态"""
    # 验证权限
    has_access = await verify_task_access(task_id, str(current_user.id), session)
    if not has_access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    # 查询任务
    from sqlalchemy import select

    from src.db.models import Task

    result = await session.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()

    if task is None:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TASK_EVAL_001",
            trace_id=trace_id,
            message="任务不存在",
            path=f"/api/v1/tasks/{task_id}/ac",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )

    # 解析 AC 列表
    acceptance_criteria = task.acceptance_criteria or []

    # 转换为响应格式
    ac_list = []
    total = len(acceptance_criteria)
    passed = 0
    failed = 0
    pending = 0

    for ac in acceptance_criteria:
        ac_status = ac.get("status", "pending")

        if ac_status == "passed":
            passed += 1
        elif ac_status == "failed":
            failed += 1
        else:
            pending += 1

        ac_list.append(
            AcceptanceCriterionStatus(
                id=ac.get("id", ""),
                description=ac.get("description", ""),
                type=ac.get("type"),
                is_red_line=ac.get("is_red_line", False),
                weight=ac.get("weight", 1.0),
                status=ac_status,
                evaluator_type=ac.get("evaluator_type"),
                evaluator_id=ac.get("evaluator_id"),
                evaluated_at=ac.get("evaluated_at"),
                retry_count=ac.get("retry_count", 0),
                evaluation_result=ac.get("evaluation_result"),
            )
        )

    return TaskACListResponse(
        task_id=task_id,
        total=total,
        passed=passed,
        failed=failed,
        pending=pending,
        acceptance_criteria=ac_list,
    )


@router.post(
    "/{task_id}/ac/{ac_id}/evaluate",
    response_model=ACEvaluationResult,
    summary="评估单个 AC",
    description="对指定的验收标准进行评估",
)
async def evaluate_task_ac(
    task_id: str,
    ac_id: str,
    request: ACEvaluateRequest = None,
    current_user=Depends(get_current_user),
    ac_evaluator: ACEvaluator = Depends(get_ac_evaluator),
    session: AsyncSession = Depends(get_async_session),
) -> ACEvaluationResult:
    """评估单个 AC"""
    # 验证权限
    has_access = await verify_task_access(task_id, str(current_user.id), session)
    if not has_access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    # 如果提供了证据，先更新到 AC 中
    if request and request.evidence:
        from sqlalchemy import select, update

        from src.db.models import Task

        # 查询任务
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()

        if task:
            acceptance_criteria = task.acceptance_criteria or []
            for ac in acceptance_criteria:
                if ac.get("id") == ac_id:
                    ac["last_evidence"] = request.evidence
                    break

            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(acceptance_criteria=acceptance_criteria)
            )
            await session.commit()

    # 执行评估（ACEvaluator 只负责评估逻辑）
    eval_result = await ac_evaluator.evaluate_single(
        ac_id=ac_id,
        task_id=task_id,
    )

    if "error" in eval_result:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TASK_EVAL_002",
            trace_id=trace_id,
            message=eval_result.get("error", "评估失败"),
            path=f"/api/v1/tasks/{task_id}/ac/{ac_id}/evaluate",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error.model_dump(mode="json"),
        )

    # 检查是否已经通过
    if eval_result.get("status") == "already_passed":
        raise HTTPException(
            status_code=status.HTTP_208_ALREADY_REPORTED, detail="该验收标准已通过"
        )

    # 通过 EvaluationService 应用评估结果（更新数据库和状态）
    evaluation_service = EvaluationService(session)
    apply_result = await evaluation_service.apply_evaluation_results(
        task_id=task_id,
        evaluation_results=[
            {
                "metric_id": eval_result.get("ac_id"),
                "passed": eval_result.get("passed", False),
                "score": eval_result.get("score", 0),
                "feedback": eval_result.get("feedback", ""),
                "details": eval_result.get("details", {}),
            }
        ],
    )

    if "error" in apply_result:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TASK_EVAL_003",
            trace_id=trace_id,
            message=apply_result.get("error", "应用评估结果失败"),
            path=f"/api/v1/tasks/{task_id}/ac/{ac_id}/evaluate",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error.model_dump(mode="json"),
        )

    return ACEvaluationResult(
        task_id=eval_result["task_id"],
        ac_id=eval_result["ac_id"],
        passed=eval_result["passed"],
        score=eval_result["score"],
        feedback=eval_result["feedback"],
        details=eval_result.get("details"),
        execution_time=eval_result.get("execution_time", 0),
        evaluated_at=eval_result["evaluated_at"],
    )


@router.post(
    "/{task_id}/ac/evaluate-all",
    response_model=TaskACListResponse,
    summary="评估所有 AC",
    description="评估任务的所有待评估验收标准",
)
async def evaluate_all_task_ac(
    task_id: str,
    parallel: bool = Query(True, description="是否并行评估"),
    current_user=Depends(get_current_user),
    ac_evaluator: ACEvaluator = Depends(get_ac_evaluator),
    session: AsyncSession = Depends(get_async_session),
) -> TaskACListResponse:
    """评估所有 AC"""
    # 验证权限
    has_access = await verify_task_access(task_id, str(current_user.id), session)
    if not has_access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    # 执行评估（ACEvaluator 只负责评估逻辑）
    eval_result = await ac_evaluator.evaluate_all(
        task_id=task_id,
        parallel=parallel,
    )

    if "error" in eval_result:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TASK_EVAL_003",
            trace_id=trace_id,
            message=eval_result.get("error", "评估失败"),
            path=f"/api/v1/tasks/{task_id}/ac/evaluate-all",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error.model_dump(mode="json"),
        )

    # 通过 EvaluationService 批量应用评估结果
    evaluation_service = EvaluationService(session)
    results = eval_result.get("results", [])
    converted_results = [
        {
            "metric_id": r.get("ac_id"),
            "passed": r.get("passed", False),
            "score": r.get("score", 0),
            "feedback": r.get("feedback", ""),
            "details": r.get("details", {}),
        }
        for r in results
    ]
    apply_result = await evaluation_service.apply_evaluation_results(task_id, converted_results)

    if "error" in apply_result:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TASK_EVAL_004",
            trace_id=trace_id,
            message=apply_result.get("error", "应用评估结果失败"),
            path=f"/api/v1/tasks/{task_id}/ac/evaluate-all",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error.model_dump(mode="json"),
        )

    # 重新获取 AC 列表
    from sqlalchemy import select

    from src.db.models import Task

    task_result = await session.execute(select(Task).where(Task.id == task_id))
    task = task_result.scalar_one_or_none()

    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    # 解析 AC 列表
    acceptance_criteria = task.acceptance_criteria or []

    # 转换为响应格式
    ac_list = []
    total = len(acceptance_criteria)
    passed = 0
    failed = 0
    pending = 0

    for ac in acceptance_criteria:
        ac_status = ac.get("status", "pending")

        if ac_status == "passed":
            passed += 1
        elif ac_status == "failed":
            failed += 1
        else:
            pending += 1

        ac_list.append(
            AcceptanceCriterionStatus(
                id=ac.get("id", ""),
                description=ac.get("description", ""),
                type=ac.get("type"),
                is_red_line=ac.get("is_red_line", False),
                weight=ac.get("weight", 1.0),
                status=ac_status,
                evaluator_type=ac.get("evaluator_type"),
                evaluator_id=ac.get("evaluator_id"),
                evaluated_at=ac.get("evaluated_at"),
                retry_count=ac.get("retry_count", 0),
                evaluation_result=ac.get("evaluation_result"),
            )
        )

    return TaskACListResponse(
        task_id=task_id,
        total=total,
        passed=passed,
        failed=failed,
        pending=pending,
        acceptance_criteria=ac_list,
    )


@router.get(
    "/{task_id}/ac/{ac_id}/result",
    response_model=TaskACResultResponse,
    summary="获取 AC 评估结果",
    description="获取指定验收标准的评估结果",
)
async def get_ac_result(
    task_id: str,
    ac_id: str,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> TaskACResultResponse:
    """获取 AC 评估结果"""
    # 验证权限
    has_access = await verify_task_access(task_id, str(current_user.id), session)
    if not has_access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")

    # 查询任务
    from sqlalchemy import select

    from src.db.models import Task

    result = await session.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()

    if task is None:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TASK_EVAL_001",
            trace_id=trace_id,
            message="任务不存在",
            path=f"/api/v1/tasks/{task_id}/ac/{ac_id}/result",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )

    # 查找目标 AC
    acceptance_criteria = task.acceptance_criteria or []
    target_ac = None

    for ac in acceptance_criteria:
        if ac.get("id") == ac_id:
            target_ac = ac
            break

    if target_ac is None:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="TASK_EVAL_004",
            trace_id=trace_id,
            message=f"验收标准不存在: {ac_id}",
            path=f"/api/v1/tasks/{task_id}/ac/{ac_id}/result",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=error.model_dump(mode="json")
        )

    return TaskACResultResponse(
        task_id=task_id,
        ac_id=ac_id,
        status=target_ac.get("status", "pending"),
        evaluation_result=target_ac.get("evaluation_result"),
        evaluated_at=target_ac.get("evaluated_at"),
    )
