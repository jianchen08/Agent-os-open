"""
思考记录工具

负责创建和管理 Agent 思考过程的执行记录
"""

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.repositories.execution_record_repo import ExecutionRecordRepository
from src.utils.id_encoder import generate_nested_id
from src.utils.sequence_manager import get_next_sequence

logger = logging.getLogger(__name__)


async def create_thinking_record(
    session_id: str,
    thinking_content: dict[str, any],
    agent_id: str,
    agent_name: str,
    db_session: AsyncSession,
    parent_record_id: str | None = None,
) -> str:
    """
    创建思考记录

    Args:
        session_id: 会话 ID
        thinking_content: 思考内容
        agent_id: Agent ID
        agent_name: Agent 名称
        db_session: 数据库会话
        parent_record_id: 父记录 ID（可选）

    Returns:
        记录 ID
    """
    try:
        # 生成记录 ID
        sequence = await get_next_sequence(parent_record_id)
        record_id = generate_nested_id(parent_record_id, sequence, "exec")

        # 构建消息数据
        message_data = {
            "record_type": "agent_thinking",
            "thinking": thinking_content,
            "status": "completed",
            "executor": {
                "type": "agent",
                "id": agent_id,
                "name": agent_name,
            },
            "timing": {
                "started_at": thinking_content.get("started_at"),
                "completed_at": datetime.now(UTC).isoformat(),
                "duration_ms": thinking_content.get("duration_ms"),
            },
        }

        # 保存记录
        repo = ExecutionRecordRepository(db_session)
        saved_id = await repo.save_execution_record(
            session_id=session_id,
            message_data=message_data,
            parent_record_id=parent_record_id,
            record_id=record_id,
        )

        logger.info(
            f"[思考记录] 创建成功 | record_id={saved_id} | agent={agent_name} | steps={len(thinking_content.get('steps', []))}"
        )

        return saved_id

    except Exception as e:
        logger.error(
            f"[思考记录] 创建失败 | agent={agent_name} | error={e}", exc_info=True
        )
        raise


async def get_thinking_records(
    session_id: str,
    db_session: AsyncSession,
    limit: int = 50,
) -> list[dict[str, any]]:
    """
    获取思考记录列表

    Args:
        session_id: 会话 ID
        db_session: 数据库会话
        limit: 返回数量限制

    Returns:
        思考记录列表
    """
    repo = ExecutionRecordRepository(db_session)
    return await repo.get_records_by_type(
        session_id=session_id,
        record_type="agent_thinking",
        limit=limit,
    )


async def get_thinking_record_by_id(
    record_id: str,
    db_session: AsyncSession,
) -> dict[str, any] | None:
    """
    根据 ID 获取思考记录

    Args:
        record_id: 记录 ID
        db_session: 数据库会话

    Returns:
        思考记录，不存在则返回 None
    """
    repo = ExecutionRecordRepository(db_session)
    record = await repo.get_record_by_id(record_id)

    if record and record.get("message_data", {}).get("record_type") == "agent_thinking":
        return record
    return None
