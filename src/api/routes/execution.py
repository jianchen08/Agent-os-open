"""
Execution Control Routes

提供执行控制相关的 API 端点：
- 控制执行（暂停/恢复/重试/回滚）
- 取消执行
- 注入消息
- 审批执行
- 获取执行状态
- 获取执行步骤
"""

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_current_user
from src.api.websocket.events import create_execution_cancelled_event
from src.api.websocket.handler import connection_manager
from src.auth.models import UserInDB
from src.db.connection import get_async_session
from src.db.models import ExecutionRecord, Session

logger = logging.getLogger(__name__)

router = APIRouter()

# 使用全局 WebSocket 连接管理器实例
ws_manager = connection_manager


# ============================================================================
# 请求/响应模型（内联定义，避免循环依赖）
# ============================================================================


class ExecutionControlRequest(BaseModel):
    """执行控制请求"""

    action: str = Field(..., description="控制动作: pause/resume/retry/rollback")
    params: dict[str, Any] | None = Field(None, description="附加参数")


class ExecutionRollbackRequest(BaseModel):
    """执行回滚请求"""

    step_id: str | None = Field(None, description="回滚到的步骤 ID（可选）")


class ExecutionInjectRequest(BaseModel):
    """执行注入消息请求"""

    content: str = Field(..., description="消息内容")
    role: str | None = Field("user", description="消息角色: user/system")
    metadata: dict[str, Any] | None = Field(None, description="元数据")


class ApprovalRequest(BaseModel):
    """审批请求"""

    action: str = Field(..., description="审批动作: approve/reject/modify")
    comment: str | None = Field(None, description="审批意见")
    modifications: dict[str, Any] | None = Field(None, description="修改内容")


class ExecutionStatusResponse(BaseModel):
    """执行状态响应"""

    id: str = Field(..., description="执行 ID")
    session_id: str = Field(..., description="会话 ID")
    status: str = Field(..., description="执行状态")
    executor_type: str | None = Field(None, description="执行者类型")
    executor_id: str | None = Field(None, description="执行者 ID")
    executor_name: str | None = Field(None, description="执行者名称")
    input_data: dict[str, Any] | None = Field(None, description="输入数据")
    output_data: dict[str, Any] | None = Field(None, description="输出数据")
    error: str | None = Field(None, description="错误信息")
    created_at: str = Field(..., description="创建时间")
    updated_at: str | None = Field(None, description="更新时间")


class ExecutionStepResponse(BaseModel):
    """执行步骤响应"""

    id: str = Field(..., description="步骤 ID")
    execution_id: str = Field(..., description="执行 ID")
    name: str = Field(..., description="步骤名称")
    type: str = Field(..., description="步骤类型")
    status: str = Field(..., description="步骤状态")
    input: dict[str, Any] | None = Field(None, description="输入数据")
    output: dict[str, Any] | None = Field(None, description="输出数据")
    error: str | None = Field(None, description="错误信息")
    started_at: str | None = Field(None, description="开始时间")
    completed_at: str | None = Field(None, description="完成时间")


class ExecutionControlResponse(BaseModel):
    """执行控制响应"""

    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="响应消息")
    execution_id: str = Field(..., description="执行 ID")
    status: str = Field(..., description="执行状态")
    timestamp: str | None = Field(None, description="操作时间戳")


class PauseExecutionResponse(BaseModel):
    """暂停执行响应"""

    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="响应消息")
    execution_id: str = Field(..., description="执行 ID")
    status: str = Field(default="paused", description="执行状态")
    paused_at: str = Field(..., description="暂停时间")


class ResumeExecutionResponse(BaseModel):
    """恢复执行响应"""

    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="响应消息")
    execution_id: str = Field(..., description="执行 ID")
    status: str = Field(default="running", description="执行状态")
    resumed_at: str = Field(..., description="恢复时间")


class StopExecutionResponse(BaseModel):
    """停止执行响应"""

    success: bool = Field(..., description="是否成功")
    message: str = Field(..., description="响应消息")
    execution_id: str = Field(..., description="执行 ID")
    status: str = Field(default="stopped", description="执行状态")
    stopped_at: str = Field(..., description="停止时间")


# ============================================================================
# 辅助函数
# ============================================================================


async def _get_execution_record(
    execution_id: str, session_id: str, db: AsyncSession
) -> ExecutionRecord | None:
    """
    获取执行记录

    Args:
        execution_id: 执行 ID
        session_id: 会话 ID
        db: 数据库会话

    Returns:
        执行记录或 None
    """
    from sqlalchemy import select

    result = await db.execute(
        select(ExecutionRecord).where(
            ExecutionRecord.id == execution_id,
            ExecutionRecord.session_id == session_id,
            ExecutionRecord.message_data["version"]["is_current"]
            .as_boolean()
            .is_(True),
        )
    )
    return result.scalar_one_or_none()


async def _verify_session_ownership(
    thread_id: str, user_id: str, db: AsyncSession
) -> Session | None:
    """
    验证会话归属权

    Args:
        thread_id: 线程 ID
        user_id: 用户 ID
        db: 数据库会话

    Returns:
        会话对象或 None
    """
    from sqlalchemy import select

    result = await db.execute(
        select(Session).where(
            Session.id == thread_id,
            Session.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


# ============================================================================
# API 端点
# ============================================================================


@router.post("/{id}/control", summary="控制执行")
async def control_execution(
    id: str,
    request: ExecutionControlRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    控制执行（暂停/恢复/重试/回滚）

    Args:
        id: 执行 ID（使用 thread_id 作为执行上下文）
        request: 控制请求
        db: 数据库会话
        current_user: 当前用户

    Returns:
        执行状态响应
    """
    try:
        user_id_str = str(current_user.id)
        logger.info(
            f"[control_execution] 收到控制请求 | execution_id={id} | "
            f"action={request.action} | user_id={user_id_str}"
        )

        # 验证会话归属权
        session = await _verify_session_ownership(id, user_id_str, db)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )

        action = request.action.lower()

        # 处理不同的控制动作
        if action == "pause":
            return await _pause_execution(id, session, db)
        elif action == "resume":
            return await _resume_execution(id, session, db)
        elif action == "retry":
            step_id = request.params.get("step_id") if request.params else None
            return await _retry_execution(id, step_id, session, db)
        elif action == "rollback":
            step_id = request.params.get("step_id") if request.params else None
            return await _rollback_execution(id, step_id, session, db)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"不支持的控制动作: {action}",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"[control_execution] 控制执行失败 | execution_id={id} | error={e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"控制执行失败: {str(e)}",
        )


async def _pause_execution(
    thread_id: str, session: Session, db: AsyncSession
) -> dict[str, Any]:
    """暂停执行"""
    logger.info(f"[_pause_execution] 暂停执行 | thread_id={thread_id}")

    # Session 不再存储状态，状态由 ExecutionRecord 管理
    session.updated_at = datetime.now(UTC)
    await db.commit()

    # 发送 WebSocket 通知
    try:
        await ws_manager.send_to_thread(
            thread_id,
            {
                "type": "execution_paused",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {"sessionId": thread_id, "reason": "用户请求暂停"},
            },
        )
    except Exception as ws_error:
        logger.warning(f"[_pause_execution] WebSocket 发送失败 | error={ws_error}")

    return {
        "success": True,
        "message": "执行已暂停",
        "execution_id": thread_id,
        "status": "paused",
    }


async def _resume_execution(
    thread_id: str, session: Session, db: AsyncSession
) -> dict[str, Any]:
    """恢复执行"""
    logger.info(f"[_resume_execution] 恢复执行 | thread_id={thread_id}")

    # Session 不再存储状态，状态由 ExecutionRecord 管理
    session.updated_at = datetime.now(UTC)
    await db.commit()

    # 发送 WebSocket 通知
    try:
        await ws_manager.send_to_thread(
            thread_id,
            {
                "type": "execution_resumed",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {"sessionId": thread_id},
            },
        )
    except Exception as ws_error:
        logger.warning(f"[_resume_execution] WebSocket 发送失败 | error={ws_error}")

    return {
        "success": True,
        "message": "执行已恢复",
        "execution_id": thread_id,
        "status": "running",
    }


async def _retry_execution(
    thread_id: str, step_id: str | None, session: Session, db: AsyncSession
) -> dict[str, Any]:
    """重试执行"""
    logger.info(
        f"[_retry_execution] 重试执行 | thread_id={thread_id} | step_id={step_id}"
    )

    # 查找要重试的执行记录
    from sqlalchemy import select

    query = select(ExecutionRecord).where(
        ExecutionRecord.session_id == thread_id,
        ExecutionRecord.message_data["version"]["is_current"].as_boolean().is_(True),
    )

    if step_id:
        query = query.where(ExecutionRecord.id == step_id)

    query = query.order_by(
        func.json_extract(ExecutionRecord.message_data, "$.order.sequence").desc()
    ).limit(1)
    result = await db.execute(query)
    record = result.scalar_one_or_none()

    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="未找到可重试的执行记录"
        )

    # 更新记录状态为 pending
    if not record.message_data:
        record.message_data = {}
    record.message_data["status"] = "pending"
    await db.commit()

    # 发送 WebSocket 通知
    try:
        executor_name = record.message_data.get("executor", {}).get("name", "Unknown")
        await ws_manager.send_to_thread(
            thread_id,
            {
                "type": "execution_retry",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": {
                    "sessionId": thread_id,
                    "record_id": str(record.id),
                    "executor_name": executor_name,
                },
            },
        )
    except Exception as ws_error:
        logger.warning(f"[_retry_execution] WebSocket 发送失败 | error={ws_error}")

    return {
        "success": True,
        "message": "执行已重试",
        "execution_id": thread_id,
        "record_id": str(record.id),
        "status": "pending",
    }


async def _rollback_execution(
    thread_id: str, step_id: str | None, session: Session, db: AsyncSession
) -> dict[str, Any]:
    """回滚执行"""
    logger.info(
        f"[_rollback_execution] 回滚执行 | thread_id={thread_id} | step_id={step_id}"
    )

    from sqlalchemy import delete, select

    if step_id:
        # 查找目标步骤
        result = await db.execute(
            select(ExecutionRecord).where(
                ExecutionRecord.id == step_id,
                ExecutionRecord.session_id == thread_id,
            )
        )
        target_record = result.scalar_one_or_none()

        if not target_record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="目标步骤不存在"
            )

        # 删除该步骤之后的所有记录
        target_sequence = target_record.message_data.get("order", {}).get("sequence", 0)
        await db.execute(
            delete(ExecutionRecord).where(
                ExecutionRecord.session_id == thread_id,
                func.json_extract(ExecutionRecord.message_data, "$.order.sequence")
                > target_sequence,
            )
        )

        await db.commit()

        return {
            "success": True,
            "message": f"已回滚到步骤 {step_id}",
            "execution_id": thread_id,
            "rolled_back_to": step_id,
        }
    else:
        # 未指定步骤，回滚到上一个状态
        result = await db.execute(
            select(ExecutionRecord)
            .where(
                ExecutionRecord.session_id == thread_id,
                ExecutionRecord.message_data["version"]["is_current"]
                .as_boolean()
                .is_(True),
            )
            .order_by(
                ExecutionRecord.message_data["order"]["sequence"].as_integer().desc()
            )
            .limit(2)
        )
        records = result.scalars().all()

        if len(records) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="没有可回滚的步骤",
            )

        # 删除最新的记录
        latest_record = records[0]
        await db.execute(
            delete(ExecutionRecord).where(ExecutionRecord.id == latest_record.id)
        )

        await db.commit()

        return {
            "success": True,
            "message": "已回滚到上一步",
            "execution_id": thread_id,
            "rolled_back_to": str(records[1].id),
        }


@router.post("/{id}/cancel", summary="取消执行")
async def cancel_execution(
    id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    取消执行

    Args:
        id: 执行 ID（使用 thread_id）
        db: 数据库会话
        current_user: 当前用户

    Returns:
        取消结果
    """
    try:
        user_id_str = str(current_user.id)
        logger.info(
            f"[cancel_execution] 取消执行 | execution_id={id} | user_id={user_id_str}"
        )

        # 验证会话归属权
        session = await _verify_session_ownership(id, user_id_str, db)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )

        # Session 不再存储状态，状态由 ExecutionRecord 管理
        session.updated_at = datetime.now(UTC)
        await db.commit()

        # 发送 WebSocket 通知（使用统一事件）
        try:
            event = create_execution_cancelled_event(
                executionId=id,
                reason="用户请求取消",
                cancelledBy="user",
            )
            await ws_manager.send_to_thread(id, event.to_dict())
        except Exception as ws_error:
            logger.warning(f"[cancel_execution] WebSocket 发送失败 | error={ws_error}")

        return {
            "success": True,
            "message": "执行已取消",
            "execution_id": id,
            "status": "cancelled",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[cancel_execution] 取消执行失败 | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"取消执行失败: {str(e)}",
        )


@router.post("/{id}/rollback", summary="回滚执行")
async def rollback_execution(
    id: str,
    request: ExecutionRollbackRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    回滚执行到指定步骤

    Args:
        id: 执行 ID（使用 thread_id）
        request: 回滚请求
        db: 数据库会话
        current_user: 当前用户

    Returns:
        回滚结果
    """
    try:
        user_id_str = str(current_user.id)
        logger.info(
            f"[rollback_execution] 回滚执行 | execution_id={id} | "
            f"step_id={request.step_id} | user_id={user_id_str}"
        )

        # 验证会话归属权
        session = await _verify_session_ownership(id, user_id_str, db)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )

        result = await _rollback_execution(id, request.step_id, session, db)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[rollback_execution] 回滚执行失败 | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"回滚执行失败: {str(e)}",
        )


@router.post("/{id}/inject", summary="注入消息")
async def inject_message(
    id: str,
    request: ExecutionInjectRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    向执行注入消息

    Args:
        id: 执行 ID（使用 thread_id）
        request: 注入消息请求
        db: 数据库会话
        current_user: 当前用户

    Returns:
        注入结果
    """
    try:
        user_id_str = str(current_user.id)
        logger.info(
            f"[inject_message] 注入消息 | execution_id={id} | "
            f"content={request.content[:100]} | user_id={user_id_str}"
        )

        # 验证会话归属权
        session = await _verify_session_ownership(id, user_id_str, db)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )

        # 创建注入消息记录
        from sqlalchemy import func, select

        from src.db.repositories.execution_record_repo import ExecutionRecordRepository
        from src.utils.sequence_manager import get_next_sequence

        message_data = {
            "record_type": "injected_message",
            "executor": {
                "type": "user",
                "id": str(current_user.id),
                "name": current_user.username,
            },
            "is_interactive": True,
            "content": request.content,
            "status": "completed",
            "order": {
                "sequence": 0,
                "depth": 0,
            },
            "version": {
                "is_current": True,
            },
            "input": {"role": request.role, "metadata": request.metadata},
        }

        repo = ExecutionRecordRepository(db)
        record_id = await repo.save_execution_record(
            session_id=id,
            message_data=message_data,
        )

        await db.commit()

        # 发送 WebSocket 通知
        try:
            await ws_manager.send_to_thread(
                id,
                {
                    "type": "message_injected",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "data": {
                        "sessionId": id,
                        "messageId": record_id,
                        "content": request.content,
                        "role": request.role,
                    },
                },
            )
        except Exception as ws_error:
            logger.warning(f"[inject_message] WebSocket 发送失败 | error={ws_error}")

        return {
            "success": True,
            "message": "消息已注入",
            "execution_id": id,
            "message_id": record_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.exception(f"[inject_message] 注入消息失败 | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"注入消息失败: {str(e)}",
        )


@router.post("/{id}/approve", summary="审批执行")
async def approve_execution(
    id: str,
    request: ApprovalRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    审批执行（用于 HITL 人工审批场景）

    使用统一的人类交互抽象层处理审批请求。

    Args:
        id: 执行 ID（使用 thread_id）
        request: 审批请求
        db: 数据库会话
        current_user: 当前用户

    Returns:
        审批结果
    """
    try:
        from src.core.human_interaction import (
            ResponseType,
            get_human_interaction_service,
        )

        user_id_str = str(current_user.id)
        logger.info(
            f"[approve_execution] 审批执行 | execution_id={id} | "
            f"action={request.action} | user_id={user_id_str}"
        )

        # 验证会话归属权
        session = await _verify_session_ownership(id, user_id_str, db)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )

        # 使用统一的人类交互服务
        service = get_human_interaction_service()

        # 根据审批动作映射响应类型
        action = request.action.lower()
        if action == "approve":
            response_type = ResponseType.APPROVED
        elif action == "reject":
            response_type = ResponseType.REJECTED
        elif action == "modify":
            response_type = ResponseType.MODIFIED
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"不支持的审批动作: {action}",
            )

        # 查找等待审批的交互请求
        pending_requests = await service.get_pending_requests(id)
        if not pending_requests:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="未找到待审批的请求"
            )

        # 取最新的待审批请求
        pending_request = pending_requests[0]

        # 提交响应
        success = await service.submit_response(
            request_id=pending_request.request_id,
            response_type=response_type,
            responder_id=user_id_str,
            responder_name=current_user.username,
            comment=request.comment,
            modifications=request.modifications,
        )

        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="提交审批响应失败",
            )

        # 发送 WebSocket 通知
        try:
            await ws_manager.send_to_thread(
                id,
                {
                    "type": "approval_completed",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "data": {
                        "sessionId": id,
                        "requestId": pending_request.request_id,
                        "action": action,
                        "comment": request.comment,
                    },
                },
            )
        except Exception as ws_error:
            logger.warning(f"[approve_execution] WebSocket 发送失败 | error={ws_error}")

        return {
            "success": True,
            "message": f"审批已完成: {action}",
            "execution_id": id,
            "request_id": pending_request.request_id,
            "action": action,
            "status": response_type.value,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[approve_execution] 审批执行失败 | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"审批执行失败: {str(e)}",
        )


@router.get("/{id}", summary="获取执行状态")
async def get_execution_status(
    id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取执行状态

    Args:
        id: 执行 ID（使用 thread_id）
        db: 数据库会话
        current_user: 当前用户

    Returns:
        执行状态响应
    """
    try:
        user_id_str = str(current_user.id)
        logger.debug(
            f"[get_execution_status] 获取执行状态 | execution_id={id} | user_id={user_id_str}"
        )

        # 验证会话归属权
        session = await _verify_session_ownership(id, user_id_str, db)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )

        # 查找最新的执行记录
        from sqlalchemy import select

        result = await db.execute(
            select(ExecutionRecord)
            .where(
                ExecutionRecord.session_id == id,
                ExecutionRecord.message_data["version"]["is_current"]
                .as_boolean()
                .is_(True),
            )
            .order_by(
                ExecutionRecord.message_data["order"]["sequence"].as_integer().desc()
            )
            .limit(1)
        )
        record = result.scalar_one_or_none()

        if not record:
            # 会话存在但没有执行记录，表示会话处于空闲状态
            return {
                "id": id,
                "session_id": id,
                "status": "idle",  # 没有执行记录时返回 idle
                "executor_type": None,
                "executor_id": None,
                "executor_name": None,
                "input_data": None,
                "output_data": None,
                "error": None,
                "created_at": session.created_at.isoformat(),
                "updated_at": (
                    session.updated_at.isoformat() if session.updated_at else None
                ),
            }

        return {
            "id": str(record.id),
            "session_id": id,
            "status": record.status or "unknown",
            "executor_type": record.executor_type,
            "executor_id": record.executor_id,
            "executor_name": record.executor_name,
            "input_data": record.input_data,
            "output_data": record.output_data,
            "error": None,
            "created_at": (
                record.created_at.isoformat()
                if record.created_at
                else session.created_at.isoformat()
            ),
            "updated_at": (
                record.updated_at.isoformat()
                if record.updated_at
                else session.updated_at.isoformat()
            ),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[get_execution_status] 获取执行状态失败 | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取执行状态失败: {str(e)}",
        )


# 注意：/records 相关路由已移至 execution_debug.py
# 包括: /records, /records/sessions, /records/{record_id}, /records/tree/{session_id} 等


@router.get("/{id}/steps", summary="获取执行步骤列表")
async def get_execution_steps(
    id: str,
    skip: int = Query(0, ge=0, description="跳过数量"),
    limit: int = Query(100, ge=1, le=500, description="返回数量限制"),
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取执行步骤列表

    适配新的 ExecutionRecord 5 字段设计，从 message_data 提取信息。

    Args:
        id: 执行 ID（使用 thread_id）
        skip: 跳过数量
        limit: 返回数量限制
        db: 数据库会话
        current_user: 当前用户

    Returns:
        执行步骤列表
    """
    try:
        user_id_str = str(current_user.id)
        logger.debug(
            f"[get_execution_steps] 获取执行步骤 | execution_id={id} | "
            f"user_id={user_id_str} | skip={skip} | limit={limit}"
        )

        # 验证会话归属权
        session = await _verify_session_ownership(id, user_id_str, db)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )

        # 查询执行记录（适配新的 5 字段设计）
        from sqlalchemy import func, select

        query = (
            select(ExecutionRecord)
            .where(ExecutionRecord.session_id == id)
            .order_by(ExecutionRecord.created_at)
            .offset(skip)
            .limit(limit)
        )
        result = await db.execute(query)
        records = result.scalars().all()

        # 查询总数
        count_query = select(func.count(ExecutionRecord.id)).where(
            ExecutionRecord.session_id == id
        )
        count_result = await db.execute(count_query)
        total = count_result.scalar() or 0

        # 序列化执行步骤（从 message_data 提取信息）
        steps = []
        for record in records:
            message_data = record.message_data or {}
            executor = message_data.get("executor", {})
            timing = message_data.get("timing", {})
            order = message_data.get("order", {})

            steps.append(
                {
                    "id": str(record.id),
                    "execution_id": id,
                    "name": executor.get("name", "未知步骤"),
                    "type": message_data.get("record_type", "unknown"),
                    "status": message_data.get("status", "unknown"),
                    "input": message_data.get("input"),
                    "output": message_data.get("output"),
                    "error": message_data.get("error"),
                    "tool_call_id": message_data.get("tool_call_id"),  # 工具调用 ID
                    "started_at": timing.get("started_at"),
                    "completed_at": timing.get("completed_at"),
                    "sequence": order.get("sequence", 0),
                    "depth": order.get("depth", 0),
                    "duration_ms": timing.get("duration_ms"),
                }
            )

        return {
            "steps": steps,
            "total": total,
            "execution_id": id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"[get_execution_steps] 获取执行步骤失败 | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取执行步骤失败: {str(e)}",
        )


# ============================================================================
# 独立的执行控制端点（pause/resume/stop）
# ============================================================================


@router.post(
    "/{execution_id}/pause",
    summary="暂停执行",
    response_model=PauseExecutionResponse,
    responses={
        404: {"description": "会话不存在"},
        500: {"description": "服务器错误"},
    },
)
async def pause_execution(
    execution_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
) -> PauseExecutionResponse:
    """
    暂停正在执行的 Agent

    Args:
        execution_id: 执行 ID（使用 session_id/thread_id）
        db: 数据库会话
        current_user: 当前用户

    Returns:
        暂停结果
    """
    try:
        user_id_str = str(current_user.id)
        logger.info(
            f"[pause_execution] 暂停执行请求 | execution_id={execution_id} | user_id={user_id_str}"
        )

        # 验证会话归属权
        session = await _verify_session_ownership(execution_id, user_id_str, db)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )

        # 查找正在运行的执行记录
        from sqlalchemy import select

        result = await db.execute(
            select(ExecutionRecord)
            .where(
                ExecutionRecord.session_id == execution_id,
                ExecutionRecord.message_data["status"].as_string() == "running",
                ExecutionRecord.message_data["version"]["is_current"]
                .as_boolean()
                .is_(True),
            )
            .order_by(
                ExecutionRecord.message_data["order"]["sequence"].as_integer().desc()
            )
            .limit(1)
        )
        record = result.scalar_one_or_none()

        if not record:
            # 没有正在运行的执行记录，但可以保存暂停请求状态
            logger.warning(
                f"[pause_execution] 未找到运行中的执行记录 | execution_id={execution_id}"
            )

        # 更新会话时间戳
        session.updated_at = datetime.now(UTC)
        await db.commit()

        # 获取 session manager 以访问 agent loop
        from src.core.memory_session_manager import get_session_manager

        session_manager = get_session_manager()
        agent_loop = await session_manager.get_agent_loop(execution_id)

        if agent_loop:
            # 调用 agent_loop 的 pause 方法
            agent_loop.pause()
            logger.info(
                f"[pause_execution] Agent loop 已暂停 | execution_id={execution_id}"
            )

            # 更新执行记录状态
            if record and record.message_data:
                record.message_data["status"] = "paused"
                record.message_data["paused_at"] = datetime.now(UTC).isoformat()
                await db.commit()
        else:
            logger.warning(
                f"[pause_execution] Agent loop 不可用 | execution_id={execution_id}"
            )

        # 发送 WebSocket 通知
        try:
            await ws_manager.send_to_thread(
                execution_id,
                {
                    "type": "execution_paused",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "data": {
                        "execution_id": execution_id,
                        "reason": "用户请求暂停",
                        "paused_by": current_user.username,
                    },
                },
            )
        except Exception as ws_error:
            logger.warning(f"[pause_execution] WebSocket 发送失败 | error={ws_error}")

        return {
            "success": True,
            "message": "执行已暂停",
            "execution_id": execution_id,
            "status": "paused",
            "paused_at": datetime.now(UTC).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"[pause_execution] 暂停执行失败 | execution_id={execution_id} | error={e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"暂停执行失败: {str(e)}",
        )


@router.post(
    "/{execution_id}/resume",
    summary="恢复执行",
    response_model=ResumeExecutionResponse,
    responses={
        404: {"description": "会话不存在"},
        500: {"description": "服务器错误"},
    },
)
async def resume_execution_endpoint(
    execution_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
) -> ResumeExecutionResponse:
    """
    恢复已暂停的 Agent 执行

    Args:
        execution_id: 执行 ID（使用 session_id/thread_id）
        db: 数据库会话
        current_user: 当前用户

    Returns:
        恢复结果
    """
    try:
        user_id_str = str(current_user.id)
        logger.info(
            f"[resume_execution_endpoint] 恢复执行请求 | execution_id={execution_id} | user_id={user_id_str}"
        )

        # 验证会话归属权
        session = await _verify_session_ownership(execution_id, user_id_str, db)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )

        # 查找已暂停的执行记录
        from sqlalchemy import select

        result = await db.execute(
            select(ExecutionRecord)
            .where(
                ExecutionRecord.session_id == execution_id,
                ExecutionRecord.message_data["version"]["is_current"]
                .as_boolean()
                .is_(True),
            )
            .order_by(
                ExecutionRecord.message_data["order"]["sequence"].as_integer().desc()
            )
            .limit(1)
        )
        record = result.scalar_one_or_none()

        if record and record.message_data:
            current_status = record.message_data.get("status", "unknown")
            if current_status not in ["paused", "completed", "failed"]:
                logger.warning(
                    f"[resume_execution_endpoint] 当前状态不允许恢复 | status={current_status}"
                )

        # 更新会话时间戳
        session.updated_at = datetime.now(UTC)
        await db.commit()

        # 获取 session manager 以访问 agent loop
        from src.core.memory_session_manager import get_session_manager

        session_manager = get_session_manager()
        agent_loop = await session_manager.get_agent_loop(execution_id)

        if agent_loop:
            # 调用 agent_loop 的 resume 方法
            agent_loop.resume()
            logger.info(
                f"[resume_execution_endpoint] Agent loop 已恢复 | execution_id={execution_id}"
            )

            # 更新执行记录状态
            if record and record.message_data:
                record.message_data["status"] = "running"
                record.message_data["resumed_at"] = datetime.now(UTC).isoformat()
                await db.commit()
        else:
            logger.warning(
                f"[resume_execution_endpoint] Agent loop 不可用 | execution_id={execution_id}"
            )

        # 发送 WebSocket 通知
        try:
            await ws_manager.send_to_thread(
                execution_id,
                {
                    "type": "execution_resumed",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "data": {
                        "execution_id": execution_id,
                        "resumed_by": current_user.username,
                    },
                },
            )
        except Exception as ws_error:
            logger.warning(
                f"[resume_execution_endpoint] WebSocket 发送失败 | error={ws_error}"
            )

        return {
            "success": True,
            "message": "执行已恢复",
            "execution_id": execution_id,
            "status": "running",
            "resumed_at": datetime.now(UTC).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"[resume_execution_endpoint] 恢复执行失败 | execution_id={execution_id} | error={e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"恢复执行失败: {str(e)}",
        )


@router.post(
    "/{execution_id}/stop",
    summary="停止执行",
    response_model=StopExecutionResponse,
    responses={
        404: {"description": "会话不存在"},
        500: {"description": "服务器错误"},
    },
)
async def stop_execution_endpoint(
    execution_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
) -> StopExecutionResponse:
    """
    停止正在执行的 Agent

    Args:
        execution_id: 执行 ID（使用 session_id/thread_id）
        db: 数据库会话
        current_user: 当前用户

    Returns:
        停止结果
    """
    try:
        user_id_str = str(current_user.id)
        logger.info(
            f"[stop_execution_endpoint] 停止执行请求 | execution_id={execution_id} | user_id={user_id_str}"
        )

        # 验证会话归属权
        session = await _verify_session_ownership(execution_id, user_id_str, db)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )

        # 查找正在运行的执行记录
        from sqlalchemy import select

        result = await db.execute(
            select(ExecutionRecord)
            .where(
                ExecutionRecord.session_id == execution_id,
                ExecutionRecord.message_data["status"]
                .as_string()
                .in_(["running", "paused"]),
                ExecutionRecord.message_data["version"]["is_current"]
                .as_boolean()
                .is_(True),
            )
            .order_by(
                ExecutionRecord.message_data["order"]["sequence"].as_integer().desc()
            )
            .limit(1)
        )
        record = result.scalar_one_or_none()

        # 更新会话时间戳
        session.updated_at = datetime.now(UTC)
        await db.commit()

        # 获取 session manager 以访问 agent loop
        from src.core.memory_session_manager import get_session_manager

        session_manager = get_session_manager()
        agent_loop = await session_manager.get_agent_loop(execution_id)

        if agent_loop:
            # 调用 agent_loop 的 stop 方法
            agent_loop.stop()
            logger.info(
                f"[stop_execution_endpoint] Agent loop 已停止 | execution_id={execution_id}"
            )

            # 更新执行记录状态
            if record and record.message_data:
                record.message_data["status"] = "stopped"
                record.message_data["stopped_at"] = datetime.now(UTC).isoformat()
                await db.commit()
        else:
            logger.warning(
                f"[stop_execution_endpoint] Agent loop 不可用 | execution_id={execution_id}"
            )

        # 发送 WebSocket 通知
        try:
            event = create_execution_cancelled_event(
                executionId=execution_id,
                reason="用户请求停止",
                cancelled_by=current_user.username,
            )
            await ws_manager.send_to_thread(execution_id, event.to_dict())
        except Exception as ws_error:
            logger.warning(
                f"[stop_execution_endpoint] WebSocket 发送失败 | error={ws_error}"
            )

        return {
            "success": True,
            "message": "执行已停止",
            "execution_id": execution_id,
            "status": "stopped",
            "stopped_at": datetime.now(UTC).isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"[stop_execution_endpoint] 停止执行失败 | execution_id={execution_id} | error={e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"停止执行失败: {str(e)}",
        )
