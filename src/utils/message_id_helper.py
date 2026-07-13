"""
消息ID生成辅助模块

从数据库查询最大序列号，生成唯一的消息ID。
移除对内存序列号管理器的依赖，确保服务器重启后不会出现ID冲突。
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ExecutionRecord
from src.utils.id_encoder import decode_base36, encode_base36

logger = logging.getLogger(__name__)


class IDGenerationError(Exception):
    """ID生成错误"""

    pass


async def generate_execution_record_id(
    db: AsyncSession,
    session_id: str,
    parent_record_id: str | None = None,
) -> str:
    """
    生成执行记录ID（统一接口）

    - 顶层记录：parent_record_id=None，格式为 {session_id}-{seq}
    - 嵌套记录：parent_record_id 有值，格式为 {parent_record_id}-{seq}

    从数据库查询最大序列号，然后生成新的ID。
    这样确保了：
    1. 服务器重启后不会产生重复ID
    2. 删除消息后不会产生ID冲突
    3. 不依赖内存状态，完全基于数据库

    Args:
        db: 数据库会话
        session_id: 会话ID
        parent_record_id: 父记录ID（可选，用于嵌套记录）

    Returns:
        格式化的消息ID

    Raises:
        IDGenerationError: 无法生成唯一ID时抛出

    Examples:
        >>> # 顶层记录
        >>> await generate_execution_record_id(db, "thread-00001")
        "thread-00001-00001"

        >>> # 嵌套记录
        >>> await generate_execution_record_id(db, "thread-00001", parent_record_id="thread-00001-00001")
        "thread-00001-00001-00001"
    """
    if parent_record_id:
        result = await db.execute(
            select(func.max(ExecutionRecord.message_data["order"]["sequence"].as_integer())).where(
                ExecutionRecord.parent_record_id == parent_record_id
            )
        )
        base_id = parent_record_id
        context = f"parent_record_id={parent_record_id}"
    else:
        result = await db.execute(
            select(func.max(ExecutionRecord.message_data["order"]["sequence"].as_integer())).where(
                ExecutionRecord.session_id == session_id,
                ExecutionRecord.parent_record_id.is_(None),
            )
        )
        base_id = session_id
        context = f"session_id={session_id}"

    max_seq = result.scalar()
    new_seq = (max_seq or 0) + 1

    max_attempts = 10
    for attempt in range(max_attempts):
        formatted_seq = encode_base36(new_seq, 5)
        message_id = f"{base_id}-{formatted_seq}"

        existing = await db.execute(select(ExecutionRecord.id).where(ExecutionRecord.id == message_id))
        if existing.scalar_one_or_none():
            logger.warning(
                f"[generate_execution_record_id] ID 已存在，重试 | "
                f"{context} | message_id={message_id} | attempt={attempt + 1}/{max_attempts}"
            )
            new_seq += 1
        else:
            logger.debug(
                f"[generate_execution_record_id] 生成ID | "
                f"{context} | max_seq={max_seq} | new_seq={new_seq} | message_id={message_id}"
            )
            return message_id

    raise IDGenerationError(f"无法生成唯一ID（已尝试 {max_attempts} 次）| {context}")


def get_sequence_from_id(message_id: str) -> int | None:
    """
    从消息ID中提取序列号

    Args:
        message_id: 消息ID，如 "thread-00001-00001" 或 "thread-00001-00001-00002"

    Returns:
        序列号（如 1 或 2），如果解析失败则返回 None

    Examples:
        >>> get_sequence_from_id("thread-00001-00001")
        1
        >>> get_sequence_from_id("thread-00001-00002")
        2
        >>> get_sequence_from_id("thread-0000z")
        35
    """
    try:
        parts = message_id.split("-")
        if len(parts) >= 2:
            sequence_str = parts[-1]
            return decode_base36(sequence_str)
        return None
    except (ValueError, IndexError):
        return None


async def generate_task_id(
    db: AsyncSession,
    parent_task_id: str | None = None,
    thread_id: str | None = None,
) -> str:
    """
    生成任务ID（支持嵌套）

    - 根任务：task-{thread_seq}-{seq}
    - 子任务：{parent_task_id}-{seq}

    Raises:
        IDGenerationError: 无法生成唯一ID时抛出
    """
    from src.db.models import Task  # noqa: PLC0415

    if parent_task_id:
        result = await db.execute(select(func.max(Task.id)).where(Task.parent_task_id == parent_task_id))
        max_id = result.scalar()
        base_id = parent_task_id
    else:
        thread_seq = thread_id.split("-")[-1] if thread_id and "-" in thread_id else "0"
        result = await db.execute(
            select(func.max(Task.id)).where(
                Task.parent_task_id.is_(None),
                Task.id.like(f"task-{thread_seq}-%"),
            )
        )
        max_id = result.scalar()
        base_id = f"task-{thread_seq}"

    new_seq = (decode_base36(max_id.split("-")[-1]) + 1) if max_id else 1

    max_attempts = 10
    for _attempt in range(max_attempts):
        task_id = f"{base_id}-{encode_base36(new_seq, 5)}"
        existing = await db.execute(select(Task.id).where(Task.id == task_id))
        if not existing.scalar_one_or_none():
            return task_id
        new_seq += 1

    raise IDGenerationError(f"无法生成唯一任务ID（已尝试 {max_attempts} 次）| base_id={base_id}")
