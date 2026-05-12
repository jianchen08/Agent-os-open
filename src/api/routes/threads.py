"""
Thread/Session 路由

提供线程（会话）管理相关的 API 端点

注意：创建会话只能通过主agent调用，直接API调用将被拒绝
"""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.constants import Pagination
from src.api.dependencies import get_current_user
from src.api.schemas.thread import ThreadCreateRequest, ThreadUpdateRequest
from src.api.websocket.handler import connection_manager
from src.auth.models import UserInDB
from src.config.llm_config import get_llm_config
from src.core.memory_session_manager import get_session_manager
from src.core.tokenizer import get_token_counter
from src.db.connection import get_async_session
from src.db.models import AgentConfig, ExecutionRecord, Session
from src.memory.types import ContextRequest, ContextType

logger = logging.getLogger(__name__)

router = APIRouter()

# 使用全局 WebSocket 连接管理器实例（从 handler 模块导入）
ws_manager = connection_manager


class ContextTokensBreakdown(BaseModel):
    """上下文Token统计明细"""

    user_intent: int = Field(0, description="用户意图Token数")
    agent_definition: int = Field(0, description="Agent定义Token数")
    domain_knowledge: int = Field(0, description="领域知识Token数")
    tool_descriptions: int = Field(0, description="工具描述Token数")
    execution_history: int = Field(0, description="执行历史Token数")
    user_preferences: int = Field(0, description="用户偏好Token数")
    error_context: int = Field(0, description="错误上下文Token数")


class ContextTokensResponse(BaseModel):
    """上下文Token统计响应"""

    total_tokens: int = Field(..., description="总Token数")
    breakdown: ContextTokensBreakdown = Field(..., description="明细")
    model_name: str = Field(..., description="使用的模型名称")
    context_window: int = Field(..., description="模型上下文窗口大小")


async def get_session_latest_status(session_id: str, db: AsyncSession) -> str:
    """
    获取会话的最新执行状态

    从最新的 ExecutionRecord 中获取实际状态，如果没有任何执行记录则返回 "idle"

    Args:
        session_id: 会话 ID
        db: 数据库会话

    Returns:
        状态字符串（idle/running/completed/failed/pending/cancelled）
    """
    try:
        # 查询最新的执行记录
        result = await db.execute(
            select(ExecutionRecord)
            .where(
                ExecutionRecord.session_id == session_id,
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
            # 没有执行记录，会话处于空闲状态
            return "idle"

        # 从 message_data 中获取实际状态
        status = record.message_data.get("status", "unknown")
        logger.debug(
            f"获取会话状态 | session_id={session_id} | status={status} | record_id={record.id}"
        )
        return status

    except Exception as e:
        logger.warning(
            f"获取会话状态失败，返回 idle | session_id={session_id} | error={e}"
        )
        return "idle"


@router.get("", summary="获取线程列表")
async def list_threads(
    page: int = Query(
        Pagination.DEFAULT_PAGE, ge=Pagination.MIN_PAGE_SIZE, description="页码"
    ),
    page_size: int = Query(
        Pagination.DEFAULT_PAGE_SIZE,
        ge=Pagination.MIN_PAGE_SIZE,
        le=Pagination.MAX_PAGE_SIZE,
        description="每页数量",
    ),
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取线程列表

    Args:
        page: 页码
        page_size: 每页数量
        db: 数据库会话
        current_user: 当前用户

    Returns:
        线程列表
    """
    try:
        user_id_str = str(current_user.id)
        logger.info(
            f"开始查询线程列表 | user_id={user_id_str} | type={type(user_id_str)}"
        )

        # 查询总数（使用 func.count 优化，避免加载所有记录到内存）
        count_query = select(func.count(Session.id)).where(
            Session.user_id == user_id_str
        )
        count_result = await db.execute(count_query)
        total = count_result.scalar() or 0

        logger.info(f"Total threads found: {total}")

        # 查询线程列表（关联 Agent 获取名称）
        # 按 created_at 降序排序（因为 updated_at 可能为 NULL）
        query = (
            select(Session, AgentConfig.name.label("agent_name"))
            .outerjoin(AgentConfig, Session.agent_id == AgentConfig.id)
            .where(Session.user_id == user_id_str)
            .order_by(Session.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await db.execute(query)
        rows = result.all()

        # 获取所有线程 ID
        thread_ids = [str(thread.id) for thread, _ in rows]

        # 批量查询每个线程的最新状态
        thread_statuses = {}
        if thread_ids:
            # 使用子查询为每个线程找到最新的执行记录
            latest_records_query = (
                select(
                    ExecutionRecord.session_id,
                    ExecutionRecord.message_data["status"].as_string().label("status"),
                )
                .where(
                    ExecutionRecord.session_id.in_(thread_ids),
                    ExecutionRecord.message_data["version"]["is_current"]
                    .as_boolean()
                    .is_(True),
                )
                .distinct(ExecutionRecord.session_id)
            )
            result = await db.execute(latest_records_query)
            for session_id, record_status in result.all():
                thread_statuses[session_id] = record_status

        # 构建响应
        threads_data = []
        for thread, agent_name in rows:
            thread_id = str(thread.id)
            # 从批量查询结果中获取状态，如果没有则返回 idle
            current_state = thread_statuses.get(thread_id, "idle")

            threads_data.append(
                {
                    "thread_id": thread_id,
                    "current_state": current_state,
                    "intent": thread.title,  # 使用 title 字段
                    "created_at": thread.created_at.isoformat(),
                    "updated_at": (
                        thread.updated_at.isoformat()
                        if thread.updated_at
                        else thread.created_at.isoformat()
                    ),
                    "agent_id": str(thread.agent_id) if thread.agent_id else None,
                    "agent_name": agent_name
                    or "智能助手",  # TODO: Use FALLBACK_AGENT_NAME from constants
                }
            )

        return {
            "threads": threads_data,
            "total": total,
        }
    except Exception as e:
        logger.exception(f"获取线程列表失败 | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="获取线程列表失败"
        )


async def _create_thread_internal(
    request: ThreadCreateRequest,
    db: AsyncSession,
    current_user: UserInDB,
) -> dict:
    """
    内部创建线程函数

    实际执行线程创建逻辑，供主agent内部调用

    Args:
        request: 创建请求
        db: 数据库会话
        current_user: 当前用户

    Returns:
        创建的线程信息
    """
    # 从数据库查询当前用户的最大会话序列号
    from sqlalchemy import func

    # 查询当前用户最大 session_seq
    result = await db.execute(
        select(func.max(Session.session_seq)).where(
            Session.user_id == str(current_user.id)
        )
    )
    max_seq = result.scalar() or 0
    new_session_seq = max_seq + 1

    # 生成会话ID：thread-{user_id_short}-{session_seq}（全局唯一）
    from src.utils.id_encoder import encode_base36

    user_id_short = str(current_user.id)[:8]
    encoded_session_seq = encode_base36(new_session_seq, 5)
    session_id = f"thread-{user_id_short}-{encoded_session_seq}"

    logger.info(
        f"生成会话ID | session_id={session_id} | user_id={current_user.id} | "
        f"session_seq={new_session_seq} | max_seq={max_seq}"
    )

    new_session = Session(
        id=session_id,
        user_id=str(current_user.id),
        session_seq=new_session_seq,
        title=request.intent or "新会话",
        status="active",
        agent_id=request.agent_id,
    )

    db.add(new_session)
    await db.commit()
    await db.refresh(new_session)

    # 获取意图（使用标题作为意图）
    intent = new_session.title or ""

    return {
        "thread_id": str(new_session.id),
        # 新创建的线程没有执行记录，状态为 idle
        "current_state": "idle",
        "intent": intent,
        "created_at": new_session.created_at.isoformat(),
        "updated_at": (
            new_session.updated_at.isoformat()
            if new_session.updated_at
            else new_session.created_at.isoformat()
        ),
        "agent_id": str(new_session.agent_id) if new_session.agent_id else None,
    }


@router.post("", summary="创建线程", status_code=status.HTTP_201_CREATED)
async def create_thread(
    request: ThreadCreateRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
    x_main_agent_request: str | None = Header(None, alias="X-Main-Agent-Request"),
):
    """创建新线程

    重要：此API仅允许通过主agent调用，直接API调用将被拒绝。
    必须通过设置 X-Main-Agent-Request: true 头部来标识主agent请求。

    从数据库查询现有最大会话ID，生成递增的会话 ID。
    格式为 thread-00001, thread-00002 等。

    Args:
        request: 创建请求
        db: 数据库会话
        current_user: 当前用户
        x_main_agent_request: 主agent请求标识头部

    Returns:
        创建的线程信息

    Raises:
        HTTPException: 如果不是主agent请求，返回403禁止访问
    """
    # 验证是否为主agent请求
    if x_main_agent_request != "true":
        logger.warning(
            f"拒绝直接创建会话请求 | user_id={current_user.id} | "
            f"intent={request.intent} | 缺少X-Main-Agent-Request头部"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="禁止直接创建会话，请通过主agent创建会话",
        )

    try:
        # 调用内部创建函数
        return await _create_thread_internal(request, db, current_user)
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.exception(
            f"创建线程失败 | user_id={current_user.id} | intent={request.intent} | error={e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建线程失败: {str(e)}",
        )


@router.get("/{thread_id}", summary="获取线程详情")
async def get_thread(
    thread_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取线程详情

    Args:
        thread_id: 线程 ID
        db: 数据库会话
        current_user: 当前用户

    Returns:
        线程详情（包含执行记录）
    """
    try:
        user_id_str = str(current_user.id)
        # 查询线程（Session.id 是 String 类型，直接比较字符串）
        session_result = await db.execute(
            select(Session).where(
                Session.id == thread_id,
                Session.user_id == user_id_str,
            )
        )
        thread = session_result.scalar_one_or_none()

        if not thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
            )

        # 查询执行记录（适配新的 ExecutionRecord 5 字段设计）
        # ExecutionRecord 只有：id, session_id, parent_record_id, message_data, created_at
        # 其他信息从 message_data JSON 字段中提取
        records_result = await db.execute(
            select(ExecutionRecord)
            .where(ExecutionRecord.session_id == thread.id)
            .order_by(ExecutionRecord.created_at)
        )
        records = records_result.scalars().all()

        # 序列化执行记录（从 message_data 提取信息）
        execution_records = []
        for record in records:
            try:
                message_data = record.message_data or {}
                executor = message_data.get("executor", {})
                timing = message_data.get("timing", {})
                order = message_data.get("order", {})

                record_dict = {
                    "id": str(record.id),
                    "thread_id": thread_id,
                    "record_type": message_data.get("record_type", "unknown"),
                    "content": message_data.get("content", ""),
                    "executor_type": executor.get("type", ""),
                    "executor_id": executor.get("id", ""),
                    "executor_name": executor.get("name", ""),
                    "status": message_data.get("status", "unknown"),
                    "timestamp": (
                        record.created_at.isoformat() if record.created_at else None
                    ),
                    "started_at": timing.get("started_at"),
                    "completed_at": timing.get("completed_at"),
                    "duration_ms": timing.get("duration_ms"),
                    "sequence": order.get("sequence", 0),
                    "depth": order.get("depth", 0),
                    "input": message_data.get("input"),
                    "output": message_data.get("output"),
                    "error": message_data.get("error"),
                    "tool_call_id": message_data.get("tool_call_id"),  # 工具调用 ID
                    "tool_calls": message_data.get("tool_calls"),
                }
                execution_records.append(record_dict)
            except Exception as e:
                logger.warning(
                    f"序列化执行记录失败 | record_id={record.id} | error={e}"
                )
                # 添加最小化信息
                execution_records.append(
                    {
                        "id": str(record.id),
                        "thread_id": thread_id,
                        "record_type": "unknown",
                        "content": "[序列化失败]",
                        "executor_type": "",
                        "executor_id": "",
                        "executor_name": "",
                        "status": "error",
                        "timestamp": (
                            record.created_at.isoformat() if record.created_at else None
                        ),
                        "started_at": None,
                        "completed_at": None,
                        "duration_ms": None,
                        "sequence": 0,
                        "depth": 0,
                        "input": None,
                        "output": None,
                        "error": str(e),
                    }
                )

        # 获取线程的实际状态
        current_state = await get_session_latest_status(str(thread.id), db)

        return {
            "thread_id": str(thread.id),
            "current_state": current_state,
            "intent": thread.title,  # 使用 title 字段
            "created_at": thread.created_at.isoformat(),
            "updated_at": (
                thread.updated_at.isoformat()
                if thread.updated_at
                else thread.created_at.isoformat()
            ),
            "agent_id": thread.agent_id,
            "execution_records": execution_records,
        }
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="无效的线程 ID 格式"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"获取线程详情失败 | thread_id={thread_id} | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="获取线程详情失败"
        )


@router.get("/{thread_id}/detail", summary="获取线程详情（别名）")
async def get_thread_detail(
    thread_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """获取线程详情的别名端点，与 get_thread 相同"""
    return await get_thread(thread_id, db, current_user)


@router.get("/{thread_id}/state", summary="获取线程状态")
async def get_thread_state(
    thread_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取线程状态

    Args:
        thread_id: 线程 ID
        db: 数据库会话
        current_user: 当前用户

    Returns:
        线程状态
    """
    try:
        user_id_str = str(current_user.id)
        # Session.id 是 String 类型，直接比较字符串
        session_result = await db.execute(
            select(Session).where(
                Session.id == thread_id,
                Session.user_id == user_id_str,
            )
        )
        thread = session_result.scalar_one_or_none()

        if not thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
            )

        # 获取线程的实际状态
        current_state = await get_session_latest_status(thread_id, db)

        return {
            "thread_id": str(thread.id),
            "current_state": current_state,
            "intent": thread.title,  # 使用 title 字段
            "created_at": thread.created_at.isoformat(),
            "updated_at": (
                thread.updated_at.isoformat()
                if thread.updated_at
                else thread.created_at.isoformat()
            ),
            "agent_id": thread.agent_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"获取线程状态失败 | thread_id={thread_id} | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="获取线程状态失败"
        )


@router.get("/{thread_id}/history", summary="获取线程历史")
async def get_thread_history(
    thread_id: str,
    parent_id: str | None = Query(None, description="父记录ID，用于获取嵌套子记录"),
    depth: int | None = Query(None, ge=0, description="嵌套深度，用于筛选特定深度的记录"),
    agent_id: str | None = Query(None, description="按Agent ID过滤记录"),
    executor_type: str | None = Query(None, description="执行器类型(agent/tool/user)过滤"),
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取线程历史执行记录（支持嵌套结构）

    Args:
        thread_id: 线程 ID
        parent_id: 父记录ID，用于获取嵌套子记录（可选）
        depth: 嵌套深度，用于筛选特定深度的记录（可选）
        agent_id: Agent ID过滤（可选）
        executor_type: 执行器类型过滤（可选）
        db: 数据库会话
        current_user: 当前用户

    Returns:
        执行记录列表
    """
    try:
        user_id_str = str(current_user.id)
        # Session.id 是 String 类型，直接比较字符串
        session_result = await db.execute(
            select(Session).where(
                Session.id == thread_id,
                Session.user_id == user_id_str,
            )
        )
        thread = session_result.scalar_one_or_none()

        if not thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
            )

        # 构建查询条件
        query = select(ExecutionRecord).where(
            ExecutionRecord.session_id == thread.id
        )

        # 应用嵌套筛选
        if parent_id is not None:
            query = query.where(ExecutionRecord.parent_record_id == parent_id)

        if depth is not None:
            query = query.where(
                ExecutionRecord.message_data["order"]["depth"].as_integer() == depth
            )

        if agent_id is not None:
            query = query.where(
                ExecutionRecord.message_data["executor"]["id"].as_string() == agent_id
            )

        if executor_type is not None:
            query = query.where(
                ExecutionRecord.message_data["executor"]["type"].as_string() == executor_type
            )

        # 查询执行记录
        records_result = await db.execute(
            query.order_by(ExecutionRecord.created_at)
        )
        records = records_result.scalars().all()

        # 序列化执行记录（从 message_data 提取信息）
        execution_records = []
        for record in records:
            try:
                message_data = record.message_data or {}
                executor = message_data.get("executor", {})
                order = message_data.get("order", {})

                record_dict = {
                    "id": str(record.id),
                    "thread_id": thread_id,
                    "parent_id": record.parent_record_id,
                    "record_type": message_data.get("record_type", "unknown"),
                    "content": message_data.get("content", ""),
                    "executor": {
                        "type": executor.get("type", ""),
                        "id": executor.get("id", ""),
                        "name": executor.get("name", ""),
                    },
                    "status": message_data.get("status", "unknown"),
                    "timestamp": record.created_at.isoformat() if record.created_at else None,
                    "depth": order.get("depth", 0),
                    "sequence": order.get("sequence", 0),
                    "input_data": message_data.get("input"),
                    "output_data": message_data.get("output"),
                    "tool_call_id": message_data.get("tool_call_id"),
                    "tool_calls": message_data.get("tool_calls", []),
                }
                execution_records.append(record_dict)
            except Exception as e:
                logger.warning(
                    f"序列化执行记录失败 | record_id={record.id} | error={e}"
                )

        return {
            "execution_records": execution_records,
            "total": len(records),
            "filters": {
                "parent_id": parent_id,
                "depth": depth,
                "agent_id": agent_id,
                "executor_type": executor_type,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"获取线程历史失败 | thread_id={thread_id} | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="获取线程历史失败"
        )


@router.put("/{thread_id}", summary="更新线程")
async def update_thread(
    thread_id: str,
    request: ThreadUpdateRequest,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    更新线程信息

    Args:
        thread_id: 线程 ID
        request: 更新请求
        db: 数据库会话
        current_user: 当前用户

    Returns:
        更新后的线程信息
    """
    try:
        user_id_str = str(current_user.id)
        # Session.id 是 String 类型，直接比较字符串
        session_result = await db.execute(
            select(Session).where(
                Session.id == thread_id,
                Session.user_id == user_id_str,
            )
        )
        thread = session_result.scalar_one_or_none()

        if not thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
            )

        # 更新 context_data
        if request.metadata is not None:
            if thread.context_data is None:
                thread.context_data = {}
            thread.context_data.update(request.metadata)

        # 更新 agent_id
        if request.agent_id is not None:
            thread.agent_id = request.agent_id

        thread.updated_at = datetime.now(UTC)

        await db.commit()
        await db.refresh(thread)

        # 获取线程的实际状态
        current_state = await get_session_latest_status(thread_id, db)

        return {
            "thread_id": str(thread.id),
            "current_state": current_state,
            "intent": thread.title,  # 使用 title 字段
            "created_at": thread.created_at.isoformat(),
            "updated_at": thread.updated_at.isoformat(),
            "agent_id": thread.agent_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.exception(f"更新线程失败 | thread_id={thread_id} | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="更新线程失败"
        )


@router.delete("/{thread_id}", summary="删除线程")
async def delete_thread(
    thread_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    删除线程

    Args:
        thread_id: 线程 ID
        db: 数据库会话
        current_user: 当前用户

    Returns:
        删除结果
    """
    from sqlalchemy import text, update

    from src.db.models import EpisodesMemory, Task, UsageRecord

    try:
        user_id_str = str(current_user.id)
        # Session.id 是 String 类型，直接比较字符串
        session_result = await db.execute(
            select(Session).where(
                Session.id == thread_id,
                Session.user_id == user_id_str,
            )
        )
        thread = session_result.scalar_one_or_none()

        if not thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
            )

        # 清理关联数据（处理没有 CASCADE 的外键）
        # 1. 清理 messages 表（旧表，可能仍存在于数据库中）
        try:
            await db.execute(
                text("DELETE FROM messages WHERE session_id = :session_id"),
                {"session_id": thread.id},
            )
        except Exception as e:
            logger.debug(f"清理 messages 表时出错（可能表不存在）: {e}")

        # 2. 清理执行记录（有 CASCADE，但显式删除更安全）
        # 通过 session_id 关联到 Session 表，确保只删除当前用户的数据
        await db.execute(
            delete(ExecutionRecord).where(
                ExecutionRecord.session_id == thread.id,
                ExecutionRecord.session_id.in_(
                    select(Session.id).where(Session.user_id == user_id_str)
                ),
            )
        )

        # 3. 清理情景记忆（添加 user_id 筛选）
        await db.execute(
            delete(EpisodesMemory).where(
                EpisodesMemory.session_id == thread.id,
                EpisodesMemory.user_id == user_id_str,
            )
        )

        # 4. 清理任务关联（设为 NULL，添加 user_id 筛选）
        await db.execute(
            update(Task)
            .where(
                Task.session_id == thread.id,
                Task.user_id == user_id_str,
            )
            .values(session_id=None)
        )

        # 5. 清理用量记录关联（设为 NULL，添加 user_id 筛选）
        await db.execute(
            update(UsageRecord)
            .where(
                UsageRecord.session_id == thread.id,
                UsageRecord.user_id == user_id_str,
            )
            .values(session_id=None)
        )

        # 删除线程（使用复合主键确保只删除当前用户的会话）
        await db.execute(
            delete(Session).where(
                Session.id == thread.id,
                Session.user_id == user_id_str,
            )
        )

        await db.commit()

        return {
            "success": True,
            "message": "线程已删除",
            "thread_id": thread_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.exception(f"删除线程失败 | thread_id={thread_id} | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除线程失败: {str(e)}",
        )


@router.delete("/{thread_id}/messages/{message_id}", summary="删除消息及后续消息")
async def delete_message(
    thread_id: str,
    message_id: str,
    include_target: bool = Query(True, description="是否包含目标消息本身"),
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    删除指定消息及其后续所有消息

    按照 ChatGPT 的设计理念，删除一条消息时会同时删除其后续所有消息，
    以保持上下文的一致性。

    Args:
        thread_id: 线程 ID
        message_id: 消息 ID（ExecutionRecord ID）
        include_target: 是否包含目标消息本身（默认 True）
        db: 数据库会话
        current_user: 当前用户

    Returns:
        删除结果
    """
    try:
        user_id_str = str(current_user.id)
        logger.info(
            f"[delete_message] 收到删除请求 | thread_id={thread_id} | message_id={message_id} | include_target={include_target}"
        )

        # 验证线程归属
        session_result = await db.execute(
            select(Session).where(
                Session.id == thread_id,
                Session.user_id == user_id_str,
            )
        )
        thread = session_result.scalar_one_or_none()

        if not thread:
            logger.warning(f"[delete_message] 线程不存在 | thread_id={thread_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
            )

        # 查找目标消息
        logger.info(f"[delete_message] 查找目标消息 | message_id={message_id}")
        record_result = await db.execute(
            select(ExecutionRecord).where(
                ExecutionRecord.id == message_id,
                ExecutionRecord.session_id == thread_id,
            )
        )
        target_record = record_result.scalar_one_or_none()

        if not target_record:
            logger.warning(
                f"[delete_message] 消息不存在 | message_id={message_id} | thread_id={thread_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在"
            )

        # 从 message_data 中提取 record_type 和 sequence
        record_type = target_record.message_data.get("record_type", "unknown")
        order = target_record.message_data.get("order", {})
        target_sequence = order.get("sequence", 0)

        logger.info(
            f"[delete_message] 找到目标消息 | id={target_record.id} | type={record_type} | sequence={target_sequence}"
        )

        # 🔧 修复：使用 created_at 字段来精确删除
        # 原因：
        # 1. sequence 字段按会话隔离，但可能存在跨会话的相同 sequence
        # 2. 使用 created_at 时间戳来确保只删除当前会话中目标消息之后的记录
        # 3. 添加 session_id 筛选，确保不会删除其他会话的消息

        from sqlalchemy import and_, or_

        # BUG-FIX-fix_20260225_msg_delete: 修复删除条件逻辑错误
        # 问题根因: 原条件使用 OR(created_at > target, AND(created_at == target, id >= target))
        #          当 created_at 唯一时，两个条件都不匹配目标消息，导致目标消息不被删除
        # 修复方案: include_target=True 时添加 OR(id == target_id) 条件确保目标消息被删除
        # 影响范围: 消息删除功能
        # 修复日期: 2026-02-25
        if include_target:
            # 包含目标消息：删除目标消息及其后续所有消息
            # 条件1: created_at > target_created_at (后续消息)
            # 条件2: created_at == target_created_at AND id >= target_id (相同时间戳的目标及后续)
            # 条件3: id == target_id (目标消息本身，确保在 created_at 唯一时也能删除)
            delete_condition = or_(
                ExecutionRecord.created_at > target_record.created_at,
                and_(
                    ExecutionRecord.created_at == target_record.created_at,
                    ExecutionRecord.id >= target_record.id,
                ),
                ExecutionRecord.id == target_record.id,
            )
        else:
            # 不包含目标消息：删除 created_at > target_created_at 的消息
            # 或者相同时间戳但 id > 目标的消息
            delete_condition = or_(
                ExecutionRecord.created_at > target_record.created_at,
                and_(
                    ExecutionRecord.created_at == target_record.created_at,
                    ExecutionRecord.id > target_record.id,
                ),
            )

        # 先统计要删除的数量
        count_result = await db.execute(
            select(func.count(ExecutionRecord.id)).where(
                ExecutionRecord.session_id == thread_id,
                delete_condition,
            )
        )
        deleted_count = count_result.scalar() or 0

        # 执行删除
        await db.execute(
            delete(ExecutionRecord).where(
                ExecutionRecord.session_id == thread_id,
                delete_condition,
            )
        )

        await db.commit()

        # 强制刷新数据库会话，确保删除操作立即生效
        await db.refresh(thread)

        # 注意：不再需要重置序列号管理器
        # 新的ID生成方式直接从数据库查询最大序列号，删除消息后不会产生冲突

        # Note: MessageCache 已移除（简化架构）
        # 原因：数据库查询性能足够，缓存维护复杂度高

        # 清除 LangGraph checkpoint，防止删除的消息仍然出现在 LLM 上下文中
        try:
            from src.agents.langgraph_checkpoint import get_checkpoint_manager

            checkpoint_manager = get_checkpoint_manager()
            await checkpoint_manager.clear_thread_checkpoints(thread_id)
            logger.info(f"[delete_message] 已清除 LangGraph checkpoint | thread_id={thread_id}")
        except Exception as checkpoint_error:
            # Checkpoint 清除失败不影响删除操作，但记录日志
            logger.warning(f"[delete_message] 清除 LangGraph checkpoint 失败 | thread_id={thread_id} | error={checkpoint_error}")

        # 清除 LayeredContextStore 缓存，防止删除的消息仍然出现在 LLM 上下文中
        try:
            from src.memory.compressor.store_manager import (
                get_layered_context_store_manager,
            )

            store_manager = get_layered_context_store_manager()
            cleared_count = await store_manager.clear_session_cache(thread_id)
            logger.info(f"[delete_message] 已清除 LayeredContextStore 缓存 | thread_id={thread_id} | count={cleared_count}")

            # 关键修复：将 LayeredContextStore 的 db_session 设置为当前会话
            # 确保后续查询能看到删除后的最新数据（避免事务隔离级别问题）
            store = store_manager.get_store(thread_id)
            if store:
                # 获取当前用户ID（从 thread 对象）
                user_id = thread.user_id if thread else None
                store.set_db_session(db, user_id, thread_id)
                logger.info(f"[delete_message] 已更新 LayeredContextStore 的数据库会话 | thread_id={thread_id}")
        except Exception as store_error:
            # 缓存清除失败不影响删除操作，但记录日志
            logger.warning(f"[delete_message] 清除 LayeredContextStore 缓存失败 | thread_id={thread_id} | error={store_error}")

        # 通知 AgentLoop 重置图，确保下次执行时重新构建图
        try:
            from src.agents.registry import get_agent_loop_registry

            registry = get_agent_loop_registry()
            reset_result = registry.reset_graph(thread_id)
            if reset_result:
                logger.info(f"[delete_message] 已通知 AgentLoop 重置图 | thread_id={thread_id}")
            else:
                logger.debug(f"[delete_message] 未找到活跃的 AgentLoop 实例 | thread_id={thread_id}")
        except Exception as agent_error:
            # AgentLoop 重置失败不影响删除操作，但记录日志
            logger.warning(f"[delete_message] 通知 AgentLoop 重置图失败 | thread_id={thread_id} | error={agent_error}")

        logger.info(
            f"删除消息成功 | thread_id={thread_id} | message_id={message_id} | deleted_count={deleted_count}"
        )

        # 发送 WebSocket 事件通知前端
        try:
            await ws_manager.send_to_thread(
                thread_id,
                {
                    "type": "message_deleted",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "data": {
                        "sessionId": thread_id,
                        "messageId": message_id,
                        "deletedCount": deleted_count,
                    },
                },
            )
            logger.info(
                f"[delete_message] 已发送 WebSocket 事件 | thread_id={thread_id}"
            )
        except Exception as ws_error:
            # WebSocket 发送失败不影响删除操作
            logger.warning(
                f"[delete_message] WebSocket 事件发送失败 | error={ws_error}"
            )

        return {
            "success": True,
            "message": f"已删除 {deleted_count} 条消息",
            "message_id": message_id,  # 前端期望的字段名
            "deleted_count": deleted_count,  # 保留额外信息
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.exception(
            f"删除消息失败 | thread_id={thread_id} | message_id={message_id} | error={e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除消息失败: {str(e)}",
        )


@router.get("/{thread_id}/messages", summary="获取线程消息列表")
async def get_thread_messages(
    thread_id: str,
    skip: int = Query(Pagination.DEFAULT_SKIP, ge=0, description="跳过数量"),
    limit: int = Query(
        Pagination.DEFAULT_LIMIT,
        ge=Pagination.MIN_LIMIT,
        le=Pagination.MAX_LIMIT,
        description="返回数量限制",
    ),
    agent_id: str | None = Query(None, description="按Agent ID过滤消息"),
    parent_id: str | None = Query(None, description="父记录ID，用于获取嵌套子记录"),
    depth: int | None = Query(None, ge=0, description="嵌套深度，用于筛选特定深度的记录"),
    executor_type: str | None = Query(None, description="执行器类型(agent/tool/user)过滤"),
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取线程消息列表（优化版本 - 支持嵌套结构）

    优化改进:
    1. 使用 is_current 布尔字段替代 JSON 过滤
    2. 使用 MessageView 层简化转换逻辑
    3. 支持消息缓存提升性能
    4. 优化数据库查询条件
    5. 支持嵌套结构查询（parent_id/depth）

    Args:
        thread_id: 线程 ID
        skip: 跳过数量
        limit: 返回数量限制
        agent_id: Agent ID过滤（可选）
        parent_id: 父记录ID，用于获取嵌套子记录（可选）
        depth: 嵌套深度，用于筛选特定深度的记录（可选）
        executor_type: 执行器类型过滤（可选）
        db: 数据库会话
        current_user: 当前用户

    Returns:
        消息列表响应
    """
    try:
        from sqlalchemy import and_

        from src.api.views.message_view import (
            MessageQueryBuilder,
            MessageView,
        )

        user_id_str = str(current_user.id)
        logger.info(
            f"[get_thread_messages] 优化版查询 | thread_id={thread_id} | "
            f"user_id={user_id_str} | agent_id={agent_id} | parent_id={parent_id} | depth={depth}"
        )

        # Note: MessageCache 已移除（简化架构）
        # 直接查询数据库，性能足够（有索引优化）

        # 🚀 构建优化的查询条件（支持嵌套结构）
        base_conditions = MessageQueryBuilder.build_base_conditions(
            thread_id, user_id_str, parent_id=parent_id, depth=depth
        )

        # 🎯 Agent ID过滤逻辑
        if agent_id:
            agent_condition = MessageQueryBuilder.build_agent_filter_condition(agent_id)
            base_conditions.append(agent_condition)
            logger.info(f"[get_thread_messages] 启用Agent过滤 | agent_id={agent_id}")

        # 🎯 执行器类型过滤逻辑
        if executor_type:
            type_condition = MessageQueryBuilder.build_executor_type_condition(executor_type)
            base_conditions.append(type_condition)
            logger.info(f"[get_thread_messages] 启用类型过滤 | executor_type={executor_type}")

        # 🚀 单次JOIN查询获取消息和会话信息
        query = (
            select(ExecutionRecord, Session.agent_id)
            .join(Session, ExecutionRecord.session_id == Session.id)
            .where(and_(*base_conditions))
            .order_by(ExecutionRecord.created_at)  # 使用 created_at 排序
            .offset(skip)
            .limit(limit)
        )

        result = await db.execute(query)
        records_with_session = result.all()

        if not records_with_session:
            # 验证线程是否存在（用于错误提示）
            session_check = await db.execute(
                select(Session).where(
                    Session.id == thread_id,
                    Session.user_id == user_id_str,
                )
            )
            if not session_check.scalar_one_or_none():
                logger.warning(
                    f"[get_thread_messages] 线程不存在 | thread_id={thread_id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
                )

        logger.info(
            f"[get_thread_messages] 查询到记录 | count={len(records_with_session)}"
        )

        # 🚀 单次查询获取总数
        count_query = (
            select(func.count(ExecutionRecord.id))
            .join(Session, ExecutionRecord.session_id == Session.id)
            .where(and_(*base_conditions))
        )
        count_result = await db.execute(count_query)
        total = count_result.scalar() or 0

        # 🚀 使用 MessageView 批量转换（性能优化）
        messages = MessageView.batch_convert_records(records_with_session, current_user)

        logger.info(
            f"[get_thread_messages] 返回消息 | count={len(messages)} | total={total} | agent_filter={agent_id}"
        )

        response = {
            "messages": messages,
            "total": total,
            "session_id": thread_id,  # 兼容前端期望
            "agent_id": agent_id,  # 当前过滤的Agent ID
        }

        # Note: MessageCache 已移除（简化架构）
        # 直接返回结果，不缓存

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"获取消息列表失败 | thread_id={thread_id} | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="获取消息列表失败"
        )


@router.get("/{thread_id}/records", summary="获取线程执行记录列表")
async def get_thread_records(
    thread_id: str,
    skip: int = Query(Pagination.DEFAULT_SKIP, ge=0, description="跳过数量"),
    limit: int = Query(
        Pagination.DEFAULT_LIMIT,
        ge=Pagination.MIN_LIMIT,
        le=Pagination.MAX_LIMIT,
        description="返回数量限制",
    ),
    parent_id: str | None = Query(
        None, description="父记录ID，用于获取嵌套子记录"
    ),
    agent_id: str | None = Query(
        None, description="Agent ID，用于筛选特定Agent的记录"
    ),
    executor_type: str | None = Query(
        None, description="执行器类型(agent/tool/user/workflow)，用于筛选特定类型的记录"
    ),
    depth: int | None = Query(
        None, ge=0, description="嵌套深度，用于筛选特定深度的记录"
    ),
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取线程执行记录列表（支持嵌套结构筛选）

    Args:
        thread_id: 线程 ID
        skip: 跳过数量
        limit: 返回数量限制
        parent_id: 父记录ID，用于获取嵌套子记录（不传则获取顶层记录）
        agent_id: Agent ID，用于筛选特定Agent的记录
        executor_type: 执行器类型，用于筛选特定类型的记录
        depth: 嵌套深度，用于筛选特定深度的记录
        db: 数据库会话
        current_user: 当前用户

    Returns:
        执行记录列表响应，包含嵌套结构信息
    """
    try:
        user_id_str = str(current_user.id)
        # 验证线程归属
        session_result = await db.execute(
            select(Session).where(
                Session.id == thread_id,
                Session.user_id == user_id_str,
            )
        )
        thread = session_result.scalar_one_or_none()

        if not thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
            )

        # 构建基础查询
        query = select(ExecutionRecord).where(
            ExecutionRecord.session_id == thread.id
        )

        # 应用 parent_id 筛选
        if parent_id is not None:
            query = query.where(ExecutionRecord.parent_record_id == parent_id)

        # 应用 depth 筛选（从 message_data JSON 字段中提取）
        if depth is not None:
            # 使用 JSON 路径查询
            query = query.where(
                ExecutionRecord.message_data["order"]["depth"].as_integer() == depth
            )

        # 应用 agent_id 筛选（从 message_data JSON 字段中提取）
        if agent_id is not None:
            query = query.where(
                ExecutionRecord.message_data["executor"]["id"].as_string() == agent_id
            )

        # 应用 executor_type 筛选
        if executor_type is not None:
            query = query.where(
                ExecutionRecord.message_data["executor"]["type"].as_string() == executor_type
            )

        # 排序和分页
        query = (
            query.order_by(ExecutionRecord.created_at)
            .offset(skip)
            .limit(limit)
        )

        result = await db.execute(query)
        records = result.scalars().all()

        # 查询总数（使用相同的筛选条件）
        count_query = select(func.count(ExecutionRecord.id)).where(
            ExecutionRecord.session_id == thread.id
        )
        if parent_id is not None:
            count_query = count_query.where(
                ExecutionRecord.parent_record_id == parent_id
            )
        if depth is not None:
            count_query = count_query.where(
                ExecutionRecord.message_data["order"]["depth"].as_integer() == depth
            )
        if agent_id is not None:
            count_query = count_query.where(
                ExecutionRecord.message_data["executor"]["id"].as_string() == agent_id
            )
        if executor_type is not None:
            count_query = count_query.where(
                ExecutionRecord.message_data["executor"]["type"].as_string() == executor_type
            )

        count_result = await db.execute(count_query)
        total = count_result.scalar() or 0

        # 构建响应数据
        execution_records = []
        for record in records:
            message_data = record.message_data or {}
            executor = message_data.get("executor", {})
            order = message_data.get("order", {})

            record_dict = {
                "id": str(record.id),
                "thread_id": thread_id,
                "parent_id": record.parent_record_id,
                "record_type": message_data.get("record_type", "unknown"),
                "content": message_data.get("content", ""),
                "executor": {
                    "type": executor.get("type", ""),
                    "id": executor.get("id", ""),
                    "name": executor.get("name", ""),
                },
                "status": message_data.get("status", "unknown"),
                "timestamp": record.created_at.isoformat() if record.created_at else None,
                "depth": order.get("depth", 0),
                "sequence": order.get("sequence", 0),
                "input_data": message_data.get("input", {}),
                "output_data": message_data.get("output", {}),
                "tool_call_id": message_data.get("tool_call_id"),
                "tool_calls": message_data.get("tool_calls", []),
                "has_children": False,  # 将在下面检查
            }

            # 检查是否有子记录
            children_count = await db.execute(
                select(func.count(ExecutionRecord.id)).where(
                    ExecutionRecord.parent_record_id == record.id
                )
            )
            record_dict["has_children"] = children_count.scalar() > 0

            execution_records.append(record_dict)

        return {
            "execution_records": execution_records,
            "total": total,
            "filters": {
                "parent_id": parent_id,
                "agent_id": agent_id,
                "executor_type": executor_type,
                "depth": depth,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"获取执行记录列表失败 | thread_id={thread_id} | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取执行记录列表失败",
        )


@router.get("/{thread_id}/tree", summary="获取线程执行记录树状结构")
async def get_thread_tree(
    thread_id: str,
    max_depth: int = Query(10, ge=1, le=20, description="最大递归深度"),
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取线程执行记录的树状结构（支持嵌套Agent）

    用于前端展示嵌套的Agent执行过程，每个节点可以展开查看子记录。
    树状结构确保主Agent和子Agent的记录是隔离的。

    Args:
        thread_id: 线程 ID
        max_depth: 最大递归深度（防止无限递归）
        db: 数据库会话
        current_user: 当前用户

    Returns:
        树状结构的执行记录
    """
    try:
        user_id_str = str(current_user.id)
        # 验证线程归属
        session_result = await db.execute(
            select(Session).where(
                Session.id == thread_id,
                Session.user_id == user_id_str,
            )
        )
        thread = session_result.scalar_one_or_none()

        if not thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
            )

        async def build_tree(parent_id: str | None, current_depth: int) -> list[dict]:
            """递归构建树状结构"""
            if current_depth > max_depth:
                return []

            query = (
                select(ExecutionRecord)
                .where(
                    ExecutionRecord.session_id == thread.id,
                    ExecutionRecord.parent_record_id == parent_id,
                )
                .order_by(ExecutionRecord.created_at)
            )
            result = await db.execute(query)
            records = result.scalars().all()

            nodes = []
            for record in records:
                message_data = record.message_data or {}
                executor = message_data.get("executor", {})
                order = message_data.get("order", {})

                node = {
                    "id": str(record.id),
                    "thread_id": thread_id,
                    "parent_id": record.parent_record_id,
                    "record_type": message_data.get("record_type", "unknown"),
                    "content": message_data.get("content", ""),
                    "executor": {
                        "type": executor.get("type", ""),
                        "id": executor.get("id", ""),
                        "name": executor.get("name", ""),
                    },
                    "status": message_data.get("status", "unknown"),
                    "timestamp": record.created_at.isoformat() if record.created_at else None,
                    "depth": order.get("depth", current_depth),
                    "sequence": order.get("sequence", 0),
                    "tool_calls": message_data.get("tool_calls", []),
                    "children": [],  # 子记录
                }

                # 递归获取子记录
                node["children"] = await build_tree(record.id, current_depth + 1)
                nodes.append(node)

            return nodes

        # 从根节点开始构建树
        tree = await build_tree(None, 0)

        return {
            "thread_id": thread_id,
            "tree": tree,
            "total_nodes": len(tree),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"获取线程树状结构失败 | thread_id={thread_id} | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="获取线程树状结构失败",
        )


@router.put(
    "/{thread_id}/agent",
    summary="更新会话绑定的 Agent",
    description="更新会话绑定的 Agent ID，null 表示使用默认助手",
)
async def update_thread_agent(
    thread_id: str,
    agent_id: str | None = None,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    更新会话绑定的 Agent

    Args:
        thread_id: 会话 ID
        agent_id: Agent ID，None 表示使用默认助手
        db: 数据库会话
        current_user: 当前用户

    Returns:
        更新后的会话信息
    """
    try:
        user_id_str = str(current_user.id)
        session_result = await db.execute(
            select(Session).where(
                Session.id == thread_id,
                Session.user_id == user_id_str,
            )
        )
        thread = session_result.scalar_one_or_none()

        if not thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
            )

        # 如果提供了 agent_id，验证其有效性
        if agent_id:
            try:
                agent_result = await db.execute(
                    select(AgentConfig).where(AgentConfig.id == agent_id)
                )
                agent = agent_result.scalar_one_or_none()
                if not agent:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Agent {agent_id} 不存在",
                    )
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="无效的 Agent ID 格式",
                )

        # 更新 agent_id（直接存储在 Session 表的 agent_id 字段）
        thread.agent_id = agent_id
        thread.updated_at = datetime.now(UTC)

        await db.commit()
        await db.refresh(thread)

        return {
            "thread_id": str(thread.id),
            "agent_id": agent_id,
            "updated_at": thread.updated_at.isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.exception(f"更新会话 Agent 失败 | thread_id={thread_id} | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="更新会话 Agent 失败",
        )


@router.post(
    "/{thread_id}/messages/{message_id}/retry",
    summary="重新生成AI回复（创建新版本）",
    description="重新生成指定AI消息的回复，支持原位重试和版本管理",
)
async def retry_message(
    thread_id: str,
    message_id: str,
    request: dict | None = None,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    重新生成AI回复（消息重试）

    Args:
        thread_id: 线程 ID
        message_id: 要重试的消息 ID（ExecutionRecord ID）
        request: 请求体（可选，包含 new_content 等参数）
        db: 数据库会话
        current_user: 当前用户

    Returns:
        重试结果
    """
    try:
        user_id_str = str(current_user.id)

        logger.info(
            f"[retry_message] 开始重试消息 | thread_id={thread_id} | message_id={message_id}"
        )

        # 1. 验证线程归属
        session_result = await db.execute(
            select(Session).where(
                Session.id == thread_id,
                Session.user_id == user_id_str,
            )
        )
        thread = session_result.scalar_one_or_none()

        if not thread:
            logger.warning(
                f"[retry_message] 线程不存在 | thread_id={thread_id} | user_id={user_id_str}"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
            )

        # 2. 查找目标消息
        record_result = await db.execute(
            select(ExecutionRecord).where(
                ExecutionRecord.id == message_id,
                ExecutionRecord.session_id == thread_id,
            )
        )
        target_record = record_result.scalar_one_or_none()

        if not target_record:
            logger.warning(
                f"[retry_message] 消息不存在 | message_id={message_id} | thread_id={thread_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在"
            )

        # 从 message_data 中提取字段
        record_type = target_record.message_data.get("record_type", "unknown")
        status_value = target_record.message_data.get("status", "unknown")

        logger.info(
            f"[retry_message] 找到消息 | record_type={record_type} | status={status_value}"
        )

        # 3. 验证消息类型（只能重试AI消息）
        if record_type not in [
            "ai_response",
            "agent_think",
            "assistant",
            "llm_response",
        ]:
            logger.warning(
                f"[retry_message] 消息类型不支持重试 | record_type={record_type}"
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"只能重试AI助手消息，当前消息类型: {record_type}",
            )

        # 4. 获取请求参数
        if request:
            request.get("new_content")

        # 5. 创建新版本记录（原位重试）
        # 使用相同的sequence，但更新内容
        output_data = target_record.output_data or {}
        new_version = output_data.get("version", 1) + 1

        # [修复] 保存旧版本（用于版本管理）
        from src.db.repositories.execution_record_repo import ExecutionRecordRepository
        from src.utils.sequence_manager import get_next_sequence

        repo = ExecutionRecordRepository(db)

        old_version_message_data = {
            **target_record.message_data,
            "version": {"is_current": False},
            "status": "completed",
        }

        sequence = await get_next_sequence(target_record.id)

        old_version_id = await repo.save_execution_record(
            session_id=thread_id,
            message_data=old_version_message_data,
            parent_record_id=target_record.id,
        )

        # 更新原记录（不创建新记录，直接更新）
        target_record.output_data = {
            **output_data,
            "version": new_version,
            "is_current": True,
            "has_history": True,  # 标记有历史版本
        }
        # ✅ 修复：不要立即清空内容，而是在开始生成时再清空
        # target_record.content = ""  # 不要立即清空
        target_record.status = "pending"
        target_record.is_current = True  # [修复] 确保当前版本的布尔字段为 True

        await db.commit()
        await db.refresh(target_record)

        logger.info(
            f"消息重试 | thread_id={thread_id} | message_id={message_id} | "
            f"version={new_version} | old_version_id={old_version_id}"
        )

        # [修复] 触发AI生成新内容
        # 获取对话历史（不包括当前重试的消息，以避免无限循环）
        from src.api.services.chat_service import get_chat_service

        chat_service = get_chat_service()

        # 获取父消息（用户消息）作为输入
        parent_result = await db.execute(
            select(ExecutionRecord)
            .where(
                ExecutionRecord.session_id == thread_id,
                ExecutionRecord.created_at < target_record.created_at,
            )
            .order_by(ExecutionRecord.created_at.desc())
            .limit(1)
        )
        parent_record = parent_result.scalar_one_or_none()

        if parent_record:
            user_content = parent_record.content
            logger.info(
                f"[retry_message] 使用父消息作为输入 | parent_id={parent_record.id} | content={user_content[:100]}"
            )

            # 流式生成新内容
            full_response = ""
            # ✅ 修复：在开始生成时才清空内容，避免创建空版本
            target_record.content = ""
            await db.commit()

            # ✅ 修复：发送 stream_start 事件，通知前端开始流式输出
            try:
                from src.api.websocket.handler import connection_manager

                await connection_manager.send_to_thread(
                    thread_id,
                    {
                        "type": "stream_start",
                        "thread_id": thread_id,
                        "message_id": message_id,
                        "data": {
                            "ai_message_id": message_id,
                            "is_retry": True,  # 标记为重试
                        },
                    },
                )
                logger.info(
                    f"[retry_message] 已发送 stream_start 事件 | message_id={message_id}"
                )
            except Exception as ws_error:
                logger.warning(
                    f"[retry_message] 发送 stream_start 失败 | error={ws_error}"
                )

            async for chunk in chat_service.stream_response(
                db, thread_id, user_content
            ):
                full_response += chunk

                # 实时更新记录的content
                target_record.content = full_response
                await db.commit()

                # 通过WebSocket发送流式更新（使用原messageId）
                # ✅ 修复：发送增量 chunk 而不是完整内容，与 user_input 处理保持一致
                try:
                    from src.api.websocket.handler import connection_manager

                    # 使用 STREAM_CHUNK 事件类型，前端已定义
                    await connection_manager.send_to_thread(
                        thread_id,
                        {
                            "type": "stream_chunk",
                            "thread_id": thread_id,
                            "message_id": message_id,
                            "data": {
                                "chunk": chunk,  # ✅ 修复：发送增量 chunk
                                "ai_message_id": message_id,
                            },
                        },
                    )
                except Exception as ws_error:
                    logger.warning(
                        f"[retry_message] WebSocket发送失败 | error={ws_error}"
                    )

            # 更新状态为completed
            target_record.status = "completed"
            await db.commit()

            # 发送流式结束事件
            try:
                from src.api.websocket.handler import connection_manager

                await connection_manager.send_to_thread(
                    thread_id,
                    {
                        "type": "stream_end",
                        "thread_id": thread_id,
                        "message_id": message_id,
                        "data": {
                            "ai_message_id": message_id,
                        },
                    },
                )
            except Exception as ws_error:
                logger.warning(f"[retry_message] 发送stream_end失败 | error={ws_error}")

            logger.info(
                f"[retry_message] AI重新生成完成 | thread_id={thread_id} | "
                f"message_id={message_id} | content_length={len(full_response)}"
            )
        else:
            logger.warning("[retry_message] 未找到父消息，无法生成新内容")
            target_record.status = "failed"
            target_record.content = "抱歉，无法重新生成：未找到对话上下文"
            await db.commit()

        return {
            "success": True,
            "message": "已重新生成",
            "message_id": message_id,  # 返回原消息ID
            "version": new_version,
            "session_id": thread_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.exception(
            f"重试消息失败 | thread_id={thread_id} | message_id={message_id} | error={e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"重试消息失败: {str(e)}",
        )


@router.get(
    "/{thread_id}/messages/{message_id}/versions",
    summary="获取消息的所有版本",
    description="获取指定消息的所有历史版本（只返回有效的、未删除的版本）",
)
async def get_message_versions(
    thread_id: str,
    message_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取消息的所有版本（已禁用）

    暂时禁用版本管理功能，只返回空列表

    Args:
        thread_id: 线程 ID
        message_id: 消息 ID
        db: 数据库会话
        current_user: 当前用户

    Returns:
        空版本列表
    """
    # 版本管理功能已禁用，返回简化响应
    logger.info(
        f"[get_message_versions] 版本管理已禁用 | thread_id={thread_id} | message_id={message_id}"
    )
    return {
        "versions": [],
        "total": 0,
        "current_version": 1,
    }


@router.put(
    "/{thread_id}/messages/{message_id}",
    summary="编辑消息内容",
    description="编辑指定消息的内容（支持用户消息、系统消息）",
)
async def edit_message(
    thread_id: str,
    message_id: str,
    request: dict,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    编辑消息内容

    Args:
        thread_id: 线程 ID
        message_id: 消息 ID
        request: 请求体 { content: str, trigger_regenerate: bool }
        db: 数据库会话
        current_user: 当前用户

    Returns:
        编辑后的消息
    """
    try:
        user_id_str = str(current_user.id)

        # 1. 验证线程归属
        session_result = await db.execute(
            select(Session).where(
                Session.id == thread_id,
                Session.user_id == user_id_str,
            )
        )
        thread = session_result.scalar_one_or_none()

        if not thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
            )

        # 2. 查找目标消息
        record_result = await db.execute(
            select(ExecutionRecord).where(
                ExecutionRecord.id == message_id,
                ExecutionRecord.session_id == thread_id,
            )
        )
        target_record = record_result.scalar_one_or_none()

        if not target_record:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在"
            )

        # 3. 获取新内容和是否触发重新生成的标志
        new_content = request.get("content", "").strip()
        trigger_regenerate = request.get("trigger_regenerate", False)

        if not new_content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="消息内容不能为空"
            )

        # 4. 更新消息内容
        old_content = target_record.content
        target_record.content = new_content
        target_record.extra_data = target_record.extra_data or {}
        target_record.extra_data["edited"] = True
        target_record.extra_data["edited_at"] = datetime.now(UTC).isoformat()

        # 如果内容有变化，记录原始内容
        if old_content != new_content:
            target_record.extra_data["original_content"] = old_content

        await db.commit()
        # 从 message_data 中提取 record_type
        record_type = target_record.message_data.get("record_type", "unknown")
        logger.info(
            f"消息编辑成功 | thread_id={thread_id} | message_id={message_id} | "
            f"record_type={record_type} | trigger_regenerate={trigger_regenerate}"
        )

        # 5. 如果是用户消息且要求触发重新生成，则重新生成AI回复
        should_regenerate = False
        if trigger_regenerate and record_type == "user_input":
            # 查找该用户消息之后的第一条AI消息
            next_ai_result = await db.execute(
                select(ExecutionRecord)
                .where(
                    ExecutionRecord.session_id == thread_id,
                    ExecutionRecord.created_at > target_record.created_at,
                )
                .order_by(ExecutionRecord.created_at.asc())
                .limit(10)  # 获取多条，在 Python 中过滤 record_type
            )
            all_records = next_ai_result.scalars().all()

            # 在 Python 中过滤出第一条 AI 响应
            next_ai_record = None
            for record in all_records:
                if record.message_data.get("record_type") == "ai_response":
                    next_ai_record = record
                    break

            if next_ai_record:
                should_regenerate = True
                logger.info(f"编辑后触发AI重新生成 | ai_message_id={next_ai_record.id}")

                # 触发AI重新生成（调用重试逻辑）
                from src.api.services.chat_service import get_chat_service

                chat_service = get_chat_service()

                # 流式生成新内容
                full_response = ""
                async for chunk in chat_service.stream_response(
                    db, thread_id, new_content
                ):
                    full_response += chunk

                    # 实时更新AI消息的content
                    next_ai_record.content = full_response
                    await db.commit()

                    # 通过WebSocket发送流式更新
                    try:
                        from src.api.websocket.handler import connection_manager

                        await connection_manager.send_to_thread(
                            thread_id,
                            {
                                "type": "message_update",
                                "sessionId": thread_id,
                                "messageId": str(next_ai_record.id),
                                "content": full_response,
                                "append": False,
                            },
                        )
                    except Exception as ws_error:
                        logger.warning(
                            f"[edit_message] WebSocket发送失败 | error={ws_error}"
                        )

                # 更新状态为completed
                next_ai_record.status = "completed"
                await db.commit()

                logger.info(
                    f"AI重新生成完成 | ai_message_id={next_ai_record.id} | content_length={len(full_response)}"
                )

        return {
            "success": True,
            "message": "消息已更新" + ("，AI正在重新生成" if should_regenerate else ""),
            "message_id": message_id,
            "content": new_content,
            "edited_at": target_record.extra_data.get("edited_at"),
            "triggered_regenerate": should_regenerate,
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.exception(
            f"编辑消息失败 | thread_id={thread_id} | message_id={message_id} | error={e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"编辑消息失败: {str(e)}",
        )


@router.get("/{thread_id}/tool_calls", summary="获取线程的工具调用记录")
async def get_thread_tool_calls(
    thread_id: str,
    skip: int = Query(Pagination.DEFAULT_SKIP, ge=0, description="跳过数量"),
    limit: int = Query(
        Pagination.DEFAULT_LIMIT,
        ge=Pagination.MIN_LIMIT,
        le=Pagination.MAX_LIMIT,
        description="返回数量限制",
    ),
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取线程的工具调用记录列表

    Args:
        thread_id: 线程 ID
        skip: 跳过数量
        limit: 返回数量限制
        db: 数据库会话
        current_user: 当前用户

    Returns:
        工具调用记录列表
    """
    try:
        from sqlalchemy import and_

        user_id_str = str(current_user.id)

        # 验证线程存在且属于当前用户
        session_check = await db.execute(
            select(Session).where(
                and_(
                    Session.id == thread_id,
                    Session.user_id == user_id_str,
                )
            )
        )
        if not session_check.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
            )

        # 查询工具调用记录
        query = (
            select(ExecutionRecord)
            .where(
                and_(
                    ExecutionRecord.session_id == thread_id,
                    ExecutionRecord.message_data["record_type"].as_string()
                    == "tool_call",
                    ExecutionRecord.message_data["version"]["is_current"]
                    .as_boolean()
                    .is_(True),
                )
            )
            .order_by(ExecutionRecord.message_data["order"]["sequence"].as_integer())
            .offset(skip)
            .limit(limit)
        )

        result = await db.execute(query)
        records = result.scalars().all()

        # 转换为响应格式
        tool_calls = []
        for record in records:
            tool_calls.append(
                {
                    "id": str(record.id),
                    "session_id": str(record.session_id),
                    "record_type": record.message_data.get("record_type", "unknown"),
                    "executor_type": record.message_data.get("executor", {}).get(
                        "type", ""
                    ),
                    "executor_id": record.message_data.get("executor", {}).get(
                        "id", ""
                    ),
                    "executor_name": record.message_data.get("executor", {}).get(
                        "name", ""
                    ),
                    "input_data": record.message_data.get("input", {}),
                    "output_data": record.message_data.get("output", {}),
                    "status": record.message_data.get("status", "unknown"),
                    "tool_call_id": record.message_data.get(
                        "tool_call_id"
                    ),  # 工具调用 ID
                    "created_at": (
                        record.created_at.isoformat() if record.created_at else None
                    ),
                    "updated_at": (
                        record.updated_at.isoformat() if record.updated_at else None
                    ),
                    # sequence 已移除，使用 created_at 排序
                }
            )

        return {"records": tool_calls, "total": len(tool_calls)}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"获取工具调用失败 | thread_id={thread_id} | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取工具调用记录失败: {str(e)}",
        )


@router.get("/{thread_id}/context-tokens", summary="获取线程的上下文Token统计")
async def get_thread_context_tokens(
    thread_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取线程的上下文Token统计

    根据上下文引擎构建的上下文进行Token统计，包括：
    - 用户意图
    - Agent定义
    - 领域知识
    - 工具描述（包括工具描述本身）
    - 执行历史
    - 用户偏好
    - 错误上下文

    Args:
        thread_id: 线程 ID
        db: 数据库会话
        current_user: 当前用户

    Returns:
        上下文Token统计详情
    """
    try:
        # 查询线程
        result = await db.execute(
            select(Session).where(
                Session.id == thread_id, Session.user_id == str(current_user.id)
            )
        )
        thread = result.scalar_one_or_none()

        if not thread:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"线程 {thread_id} 不存在",
            )

        # 获取记忆服务
        session_manager = get_session_manager()
        memory_service = await session_manager.get_memory_service(thread_id)

        if not memory_service:
            logger.warning(f"无法获取记忆服务 - thread_id: {thread_id}")
            # 返回默认值
            return ContextTokensResponse(
                total_tokens=0,
                breakdown=ContextTokensBreakdown(),
                model_name="unknown",
                context_window=128000,
            )

        # 构建上下文请求（使用所有类型的上下文）
        context_request = ContextRequest(
            required_memories=[
                ContextType.USER_INTENT,
                ContextType.AGENT_DEFINITION,
            ],
            optional_memories=[
                ContextType.DOMAIN_KNOWLEDGE,
                ContextType.TOOL_DESCRIPTIONS,
                ContextType.EXECUTION_HISTORY,
                ContextType.USER_PREFERENCES,
                ContextType.ERROR_CONTEXT,
            ],
            max_context_tokens=128000,
        )

        # 获取上下文
        context = await memory_service.get_context(
            user_id=current_user.id,
            request=context_request,
            user_intent=None,  # 不需要具体意图
        )

        # 计算各组件的Token数
        token_counter = get_token_counter()
        breakdown = ContextTokensBreakdown()

        # 用户意图
        if context.user_intent:
            breakdown.user_intent = token_counter.count_tokens(context.user_intent)

        # Agent定义
        if context.agent_definition:
            breakdown.agent_definition = token_counter.count_tokens(
                str(context.agent_definition)
            )

        # 领域知识
        if context.domain_knowledge:
            breakdown.domain_knowledge = sum(
                token_counter.count_tokens(k) for k in context.domain_knowledge
            )

        # 工具描述（包括工具描述本身）
        if context.tool_descriptions:
            for tool in context.tool_descriptions:
                # 工具描述的token
                tool_text = f"{tool.get('name', '')}: {tool.get('description', '')}"
                breakdown.tool_descriptions += token_counter.count_tokens(tool_text)
                # 工具参数schema的token
                if tool.get("args_schema"):
                    breakdown.tool_descriptions += token_counter.count_tokens(
                        str(tool["args_schema"])
                    )

        # 执行历史
        if context.execution_history:
            for history in context.execution_history:
                breakdown.execution_history += token_counter.count_tokens(str(history))

        # 用户偏好
        if context.user_preferences:
            breakdown.user_preferences = token_counter.count_tokens(
                str(context.user_preferences)
            )

        # 错误上下文
        if context.error_context:
            breakdown.error_context = token_counter.count_tokens(
                str(context.error_context)
            )

        # 获取模型信息
        model_name = "unknown"
        context_window = 128000

        # 尝试从线程的agent配置获取模型信息
        if thread.agent_id:
            result = await db.execute(
                select(AgentConfig).where(AgentConfig.id == thread.agent_id)
            )
            agent = result.scalar_one_or_none()
            if agent:
                model_name = agent.model_name
                # 从 model_params 获取 context_window
                if agent.model_params and isinstance(agent.model_params, dict):
                    context_window = agent.model_params.get("context_window", 128000)

        # 获取模型的实际上下文窗口大小
        try:
            config_manager = get_llm_config()
            if model_name != "unknown":
                # 尝试获取模型配置
                model_config = config_manager.get_model(model_name)
                if model_config:
                    window = model_config.context_window
                    if window > 0:
                        context_window = window
        except Exception as e:
            logger.warning(f"获取模型上下文窗口失败，使用默认值: {e}")
            context_window = 128000

        return ContextTokensResponse(
            total_tokens=context.total_tokens,
            breakdown=breakdown,
            model_name=model_name,
            context_window=context_window,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取上下文Token统计失败 | thread_id={thread_id} | error={e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取上下文Token统计失败: {str(e)}",
        )


@router.get(
    "/{thread_id}/messages/{record_id}/children",
    summary="获取执行记录的子节点",
    description="查询指定执行记录的所有子节点记录（支持嵌套结构）",
)
async def get_record_children(
    thread_id: str,
    record_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: UserInDB = Depends(get_current_user),
):
    """
    获取执行记录的子节点

    适配新的 ExecutionRecord 5 字段设计，通过 parent_record_id 查询子节点。

    Args:
        thread_id: 线程 ID
        record_id: 父记录 ID
        db: 数据库会话
        current_user: 当前用户

    Returns:
        子节点记录列表
    """
    try:
        from sqlalchemy import and_

        user_id_str = str(current_user.id)

        # 验证线程存在且属于当前用户
        session_check = await db.execute(
            select(Session).where(
                and_(
                    Session.id == thread_id,
                    Session.user_id == user_id_str,
                )
            )
        )
        if not session_check.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="线程不存在"
            )

        # 验证父记录存在
        parent_check = await db.execute(
            select(ExecutionRecord).where(
                and_(
                    ExecutionRecord.id == record_id,
                    ExecutionRecord.session_id == thread_id,
                )
            )
        )
        if not parent_check.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="父记录不存在"
            )

        # 查询子节点记录
        query = (
            select(ExecutionRecord)
            .where(
                and_(
                    ExecutionRecord.session_id == thread_id,
                    ExecutionRecord.parent_record_id == record_id,
                )
            )
            .order_by(ExecutionRecord.created_at)
        )
        result = await db.execute(query)
        child_records = result.scalars().all()

        # 序列化子节点记录（从 message_data 提取信息）
        children = []
        for record in child_records:
            try:
                message_data = record.message_data or {}
                executor = message_data.get("executor", {})
                timing = message_data.get("timing", {})
                order = message_data.get("order", {})

                children.append(
                    {
                        "id": str(record.id),
                        "parent_record_id": record.parent_record_id,
                        "record_type": message_data.get("record_type", "unknown"),
                        "content": message_data.get("content", ""),
                        "executor_type": executor.get("type", ""),
                        "executor_id": executor.get("id", ""),
                        "executor_name": executor.get("name", ""),
                        "status": message_data.get("status", "unknown"),
                        "timestamp": (
                            record.created_at.isoformat() if record.created_at else None
                        ),
                        "started_at": timing.get("started_at"),
                        "completed_at": timing.get("completed_at"),
                        "duration_ms": timing.get("duration_ms"),
                        "sequence": order.get("sequence", 0),
                        "depth": order.get("depth", 0),
                        "tool_call_id": message_data.get("tool_call_id"),  # 工具调用 ID
                        "input": message_data.get("input"),
                        "output": message_data.get("output"),
                        "error": message_data.get("error"),
                    }
                )
            except Exception as e:
                logger.warning(
                    f"序列化子节点记录失败 | record_id={record.id} | error={e}"
                )

        return {
            "parent_record_id": record_id,
            "children": children,
            "total": len(children),
            "thread_id": thread_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"获取子节点记录失败 | thread_id={thread_id} | record_id={record_id} | error={e}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取子节点记录失败: {str(e)}",
        )
