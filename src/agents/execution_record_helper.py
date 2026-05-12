"""
执行记录辅助函数

用于在 Agent 执行过程中创建和更新执行记录
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from src.db.models import ExecutionRecord

logger = logging.getLogger(__name__)


async def create_tool_execution_record(
    db: AsyncSession,
    session_id: str,
    tool_name: str,
    tool_args: dict[str, Any],
    parent_record_id: str | None = None,
    task_id: str | None = None,
    tool_call_id: str | None = None,
    executor_type: str | None = None,
    executor_id: str | None = None,
    executor_name: str | None = None,
) -> str:
    """
    创建工具执行记录

    工具调用记录与会话中的用户消息、Agent 响应是平级的，使用相同的 ID 格式。
    只有调用下级 agent 或工作流时，才会创建嵌套的子记录。

    Args:
        db: 数据库会话
        session_id: 会话 ID（thread_id）
        tool_name: 工具名称
        tool_args: 工具参数
        parent_record_id: 父记录 ID（仅当下级 agent/工作流时使用）
        task_id: 任务 ID（可选，用于关联执行记录与任务）
        tool_call_id: LLM 返回的工具调用 ID（用于关联 ToolMessage）
        executor_type: 执行者类型（agent/tool/workflow）
        executor_id: 执行者 ID
        executor_name: 执行者名称

    Returns:
        执行记录 ID（格式：thread-xxxxx-xxxxx，与会话其他记录平级）
    """
    try:
        from src.utils.message_id_helper import (
            generate_execution_record_id,
            get_sequence_from_id,
        )

        record_id = await generate_execution_record_id(
            db, session_id, parent_record_id
        )

        get_sequence_from_id(record_id) or 1

        # 计算深度：根据 parent_record_id 和 ID 层级计算
        from src.utils.id_encoder import parse_nested_id

        if parent_record_id:
            # 有父记录，解析父记录的深度并加 1
            try:
                parent_parsed = parse_nested_id(parent_record_id)
                parent_parsed.get("depth", 0) + 1
            except Exception:
                pass  # 默认嵌套深度为 1
        else:
            # 没有父记录，从当前 record_id 解析深度
            try:
                parsed = parse_nested_id(record_id)
                parsed.get("depth", 0)
            except Exception:
                pass  # 默认顶层深度为 0

        # 构建 message_data（适配新结构）
        message_data = {
            "type": "tool",
            "name": tool_name,
            "input": tool_args,
            "status": "running",
        }

        # 保存 tool_call_id（用于关联 ToolMessage）
        if tool_call_id:
            message_data["tool_call_id"] = tool_call_id

        # 保存 executor 信息（用于按执行者过滤）
        if executor_type or executor_id or executor_name:
            message_data["executor"] = {}
            if executor_type:
                message_data["executor"]["type"] = executor_type
            if executor_id:
                message_data["executor"]["id"] = executor_id
            if executor_name:
                message_data["executor"]["name"] = executor_name

        # 如果有关联的任务 ID，存储在 metadata 中
        if task_id:
            message_data["metadata"] = {"task_id": task_id}

        # 检查记录是否已存在（防止 ID 冲突）
        result = await db.execute(
            select(ExecutionRecord).where(ExecutionRecord.id == record_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            # 记录已存在，更新 message_data 并返回现有 ID
            existing.message_data = message_data
            await db.commit()
            logger.info(
                f"[create_tool_execution_record] 记录已存在，更新数据 | "
                f"record_id={record_id} | tool_name={tool_name}"
            )
            return record_id

        record = ExecutionRecord(
            id=record_id,
            session_id=session_id,
            parent_record_id=parent_record_id,
            message_data=message_data,
        )

        db.add(record)
        await db.commit()

        logger.info(
            f"[create_tool_execution_record] 创建工具执行记录 | "
            f"record_id={record_id} | tool_name={tool_name} | session_id={session_id} | task_id={task_id}"
        )

        return record_id

    except Exception as e:
        await db.rollback()
        logger.error(
            f"[create_tool_execution_record] 创建执行记录失败 | "
            f"tool_name={tool_name} | error={e}"
        )
        raise


async def update_tool_execution_record(
    db: AsyncSession,
    record_id: str,
    success: bool,
    output: Any = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """
    更新工具执行记录

    Args:
        db: 数据库会话
        record_id: 执行记录 ID
        success: 是否成功
        output: 输出结果
        error: 错误信息
        duration_ms: 执行时长（毫秒）
    """
    try:
        # 查询记录
        result = await db.execute(
            select(ExecutionRecord).where(ExecutionRecord.id == record_id)
        )
        record = result.scalar_one_or_none()

        if not record:
            logger.warning(
                f"[update_tool_execution_record] 执行记录不存在 | record_id={record_id}"
            )
            return

        # 更新 message_data 中的状态和输出
        if not record.message_data:
            record.message_data = {}

        record.message_data["status"] = "completed" if success else "failed"

        # 更新输出结果
        if output is not None:
            record.message_data["output"] = {"result": output}

        # 更新错误信息
        if error is not None:
            record.message_data["error"] = error

        # 更新执行时长（直接存储在顶层，不在 timing 对象中）
        if duration_ms is not None:
            record.message_data["duration_ms"] = duration_ms

        # 标记字段已修改（确保 SQLAlchemy 检测到 JSON 字段的变化）
        flag_modified(record, "message_data")

        await db.commit()

        logger.info(
            f"[update_tool_execution_record] 更新工具执行记录 | "
            f"record_id={record_id} | success={success} | duration_ms={duration_ms}"
        )

    except Exception as e:
        await db.rollback()
        logger.error(
            f"[update_tool_execution_record] 更新执行记录失败 | "
            f"record_id={record_id} | error={e}"
        )
        raise
