"""嵌套执行记录管理。

从 ToolExecutor 中提取的评估器嵌套执行记录的创建与更新逻辑。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class NestedRecordManager:
    """嵌套执行记录管理器。

    负责创建和更新评估器上下文中的嵌套工具执行记录。

    Args:
        db_session: 可选的数据库会话，为 None 时自动获取
    """

    def __init__(self, db_session: Any | None = None) -> None:
        self._db_session = db_session

    async def create_nested_execution_record(
        self,
        parent_record_id: str,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str | None = None,
    ) -> str | None:
        """创建嵌套的评估器执行记录。"""
        try:
            db = self._db_session
            if db is None:
                from infrastructure.db import get_async_session  # noqa: PLC0415

                db_session = await get_async_session()
                return await self._create_nested_record_in_session(
                    db_session,
                    parent_record_id,
                    session_id,
                    tool_name,
                    tool_args,
                    tool_call_id,
                )
            return await self._create_nested_record_in_session(
                db, parent_record_id, session_id, tool_name, tool_args, tool_call_id
            )

        except Exception as e:
            logger.warning(f"[ToolExecutor] 创建嵌套执行记录失败 | tool_name={tool_name} | error={e}")
            return None

    async def _create_nested_record_in_session(
        self,
        db: Any,
        parent_record_id: str,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_call_id: str | None = None,
    ) -> str | None:
        """在指定会话中创建嵌套执行记录。"""
        from db.models import ExecutionRecord  # noqa: PLC0415
        from db.repositories.execution_record_repo import ExecutionRecordRepository  # noqa: PLC0415
        from sqlalchemy import select  # noqa: PLC0415

        repo = ExecutionRecordRepository(db)

        parent_record = await db.execute(select(ExecutionRecord).where(ExecutionRecord.id == parent_record_id))
        parent = parent_record.scalar_one_or_none()

        actual_session_id = session_id
        if parent:
            actual_session_id = parent.session_id
        else:
            logger.warning(
                f"[ToolExecutor] 父执行记录不存在，使用传入的 session_id | "
                f"parent_record_id={parent_record_id} | session_id={session_id}"
            )

        message_data: dict[str, Any] = {
            "name": tool_name,
            "input": tool_args,
        }

        if tool_call_id:
            message_data["tool_call_id"] = tool_call_id

        record_id = await repo.save_execution_record(
            session_id=actual_session_id,
            message_data=message_data,
            type="tool",
            status="running",
            parent_record_id=parent_record_id,
            auto_commit=db is not self._db_session,
        )
        record_id = record_id["record_id"]

        logger.info(
            f"[ToolExecutor] 创建嵌套执行记录 | "
            f"record_id={record_id} | session_id={actual_session_id} | "
            f"parent_id={parent_record_id} | tool_name={tool_name}"
        )

        return record_id

    async def update_nested_execution_record(
        self,
        record_id: str,
        success: bool,
        output: Any = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """更新嵌套的评估器执行记录。"""
        try:
            db = self._db_session
            if db is None:
                from infrastructure.db import get_async_session  # noqa: PLC0415

                db_session = await get_async_session()
                await self._update_nested_record_in_session(db_session, record_id, success, output, error, duration_ms)
            else:
                await self._update_nested_record_in_session(db, record_id, success, output, error, duration_ms)

        except Exception as e:
            logger.warning(f"[ToolExecutor] 更新嵌套执行记录失败 | record_id={record_id} | error={e}")

    async def _update_nested_record_in_session(
        self,
        db: Any,
        record_id: str,
        success: bool,
        output: Any = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """在指定会话中更新嵌套执行记录。"""
        from db.models import ExecutionRecord  # noqa: PLC0415
        from sqlalchemy import select  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        result = await db.execute(select(ExecutionRecord).where(ExecutionRecord.id == record_id))
        record = result.scalar_one_or_none()

        if not record:
            logger.warning(f"[ToolExecutor] 嵌套执行记录不存在 | record_id={record_id}")
            return

        status_value = "completed" if success else "failed"
        record.status = status_value

        if output is not None:
            record.message_data["output"] = {"result": output}

        if error is not None:
            record.message_data["error"] = error

        if duration_ms is not None:
            record.message_data["duration_ms"] = duration_ms

        flag_modified(record, "message_data")

        if db is not self._db_session:
            await db.commit()

        logger.info(
            f"[ToolExecutor] 更新嵌套执行记录 | record_id={record_id} | success={success} | duration_ms={duration_ms}"
        )
