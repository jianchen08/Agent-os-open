"""
执行记录调试 API 路由

提供前端调试页面所需的 API 端点
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_async_session, get_current_user
from src.auth.models import UserInDB
from src.db.models import Session
from src.db.models.execution import ExecutionRecord
from src.db.repositories.execution_record_repo import ExecutionRecordRepository

router = APIRouter(tags=["execution-debug"])


# ========== 固定路径路由（必须在动态路径之前定义）==========

@router.get("/records/sessions", summary="获取有执行记录的会话列表（调试用）")
async def get_execution_records_sessions(
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取当前用户有执行记录的所有会话列表（用于调试页面）
    """
    from sqlalchemy import func, select

    user_id_str = str(current_user.id)

    # 获取用户的所有会话
    session_result = await db.execute(
        select(Session).where(Session.user_id == user_id_str)
    )
    sessions = session_result.scalars().all()

    if not sessions:
        return {"sessions": [], "total": 0}

    session_ids = [s.id for s in sessions]

    # 查询每个会话的执行记录数量
    count_query = (
        select(ExecutionRecord.session_id, func.count(ExecutionRecord.id).label("record_count"))
        .where(ExecutionRecord.session_id.in_(session_ids))
        .group_by(ExecutionRecord.session_id)
    )
    count_result = await db.execute(count_query)
    record_counts = {row.session_id: row.record_count for row in count_result.all()}

    # 只返回有执行记录的会话
    sessions_with_records = [
        {
            "id": s.id,
            "title": s.title or f"会话 {s.id[:8]}",
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            "record_count": record_counts.get(s.id, 0),
        }
        for s in sessions
        if s.id in record_counts and record_counts[s.id] > 0
    ]

    return {
        "sessions": sessions_with_records,
        "total": len(sessions_with_records),
    }


# ========== 动态路径路由 ==========

@router.get("/records", summary="获取执行记录列表（调试用）")
async def get_execution_records_list(
    session_id: str | None = Query(None, description="会话ID过滤"),
    parent_record_id: str | None = Query(None, description="父记录ID过滤"),
    limit: int = Query(100, ge=1, le=500, description="返回数量限制"),
    offset: int = Query(0, ge=0, description="跳过数量"),
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取执行记录列表（用于调试页面）
    如果没有提供 session_id，则返回用户的所有执行记录（跨会话）
    """
    from sqlalchemy import func, select

    user_id_str = str(current_user.id)

    # 获取用户的所有会话ID
    session_result = await db.execute(
        select(Session).where(Session.user_id == user_id_str)
    )
    sessions = session_result.scalars().all()
    session_ids = [s.id for s in sessions]

    # 处理空字符串情况
    effective_session_id = session_id if session_id else None

    if not session_ids:
        return {
            "records": [],
            "total": 0,
            "session_id": effective_session_id,
        }

    # 构建查询 - 限制在用户的会话范围内
    query = select(ExecutionRecord).where(ExecutionRecord.session_id.in_(session_ids))

    # 如果指定了 session_id，进一步过滤
    if effective_session_id:
        if effective_session_id not in session_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )
        query = query.where(ExecutionRecord.session_id == effective_session_id)

    if parent_record_id is not None:
        if parent_record_id == "":
            query = query.where(ExecutionRecord.parent_record_id.is_(None))
        else:
            query = query.where(ExecutionRecord.parent_record_id == parent_record_id)

    # 查询总数
    count_query = select(func.count(ExecutionRecord.id)).where(ExecutionRecord.session_id.in_(session_ids))
    if effective_session_id:
        count_query = count_query.where(ExecutionRecord.session_id == effective_session_id)
    if parent_record_id is not None:
        if parent_record_id == "":
            count_query = count_query.where(ExecutionRecord.parent_record_id.is_(None))
        else:
            count_query = count_query.where(ExecutionRecord.parent_record_id == parent_record_id)

    count_result = await db.execute(count_query)
    total = count_result.scalar() or 0

    # 分页查询
    query = query.order_by(ExecutionRecord.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    records = result.scalars().all()

    return {
        "records": [
            {
                "id": r.id,
                "session_id": r.session_id,
                "parent_record_id": r.parent_record_id,
                "message_data": r.message_data,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ],
        "total": total,
        "session_id": effective_session_id,
    }


@router.get("/records/tree/{session_id}", summary="获取执行记录树")
async def get_execution_records_tree(
    session_id: str,
    max_depth: int = Query(5, ge=1, le=10, description="最大嵌套深度"),
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取执行记录的树形结构（用于调试页面）
    """
    from sqlalchemy import select

    user_id_str = str(current_user.id)

    # 验证会话归属权
    session_result = await db.execute(
        select(Session).where(Session.id == session_id, Session.user_id == user_id_str)
    )
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
        )

    # 使用仓储获取执行树
    repo = ExecutionRecordRepository(db)
    tree = await repo.get_execution_tree(session_id, max_depth)

    # 查询总数
    from sqlalchemy import func

    count_result = await db.execute(
        select(func.count(ExecutionRecord.id)).where(
            ExecutionRecord.session_id == session_id
        )
    )
    total = count_result.scalar() or 0

    return {
        "tree": tree,
        "total": total,
        "session_id": session_id,
        "max_depth": max_depth,
    }


@router.get("/records/{record_id}", summary="获取单个执行记录")
async def get_execution_record(
    record_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取单个执行记录（用于前端解析 [[exec:ID]] 标记）
    """
    from sqlalchemy import select

    user_id_str = str(current_user.id)

    # 查询执行记录
    result = await db.execute(
        select(ExecutionRecord).where(ExecutionRecord.id == record_id)
    )
    record = result.scalar_one_or_none()

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="执行记录不存在"
        )

    # 验证会话归属权
    session_result = await db.execute(
        select(Session).where(Session.id == record.session_id, Session.user_id == user_id_str)
    )
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权访问此执行记录"
        )

    # 从 message_data 提取信息
    message_data = record.message_data or {}
    executor = message_data.get("executor", {})
    timing = message_data.get("timing", {})
    order = message_data.get("order", {})

    return {
        "id": str(record.id),
        "session_id": record.session_id,
        "parent_record_id": record.parent_record_id,
        "record_type": message_data.get("record_type", "unknown"),
        "executor_type": executor.get("type", ""),
        "executor_id": executor.get("id", ""),
        "executor_name": executor.get("name", ""),
        "content": message_data.get("content", ""),
        "status": message_data.get("status", "unknown"),
        "error": message_data.get("error"),
        "input": message_data.get("input"),
        "output": message_data.get("output"),
        "tool_call_id": message_data.get("tool_call_id"),
        "started_at": timing.get("started_at"),
        "completed_at": timing.get("completed_at"),
        "duration_ms": timing.get("duration_ms"),
        "sequence": order.get("sequence", 0),
        "depth": order.get("depth", 0),
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


@router.get("/records/{record_id}/children", summary="获取子执行记录")
async def get_execution_record_children(
    record_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取子执行记录列表
    """
    from sqlalchemy import select

    user_id_str = str(current_user.id)

    # 先查询父记录以获取会话ID
    parent_result = await db.execute(
        select(ExecutionRecord).where(ExecutionRecord.id == record_id)
    )
    parent = parent_result.scalar_one_or_none()

    if not parent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="父记录不存在"
        )

    # 验证会话归属权
    session_result = await db.execute(
        select(Session).where(Session.id == parent.session_id, Session.user_id == user_id_str)
    )
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权访问此记录"
        )

    # 使用仓储获取子记录
    repo = ExecutionRecordRepository(db)
    children = await repo.get_children_records(record_id)

    return children


@router.delete("/records/{record_id}", summary="删除执行记录")
async def delete_execution_record_endpoint(
    record_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    删除执行记录（用于调试页面）
    """
    from sqlalchemy import delete, select

    user_id_str = str(current_user.id)

    # 查询记录
    result = await db.execute(
        select(ExecutionRecord).where(ExecutionRecord.id == record_id)
    )
    record = result.scalar_one_or_none()

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在"
        )

    # 验证会话归属权
    session_result = await db.execute(
        select(Session).where(Session.id == record.session_id, Session.user_id == user_id_str)
    )
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="无权删除此记录"
        )

    # 删除记录（级联删除子记录）
    await db.execute(
        delete(ExecutionRecord).where(ExecutionRecord.id == record_id)
    )
    await db.commit()

    return {"success": True, "deleted_id": record_id}


@router.delete("/records/session/{session_id}", summary="删除会话的所有执行记录")
async def delete_session_execution_records(
    session_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    删除会话的所有执行记录（用于调试页面）
    """
    from sqlalchemy import delete, select

    user_id_str = str(current_user.id)

    # 验证会话归属权
    session_result = await db.execute(
        select(Session).where(Session.id == session_id, Session.user_id == user_id_str)
    )
    session = session_result.scalar_one_or_none()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
        )

    # 删除该会话的所有执行记录
    await db.execute(
        delete(ExecutionRecord).where(ExecutionRecord.session_id == session_id)
    )
    await db.commit()

    return {"success": True, "deleted_count": "all", "session_id": session_id}
