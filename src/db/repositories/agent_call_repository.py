"""
Agent 调用记录仓储

提供 AgentCallRecord 的 CRUD 操作和查询功能
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.constants import Timeout
from src.db.models import AgentCallRecord
from src.db.repositories.base import BaseRepository


class AgentCallRepository(BaseRepository[AgentCallRecord]):
    """Agent 调用记录仓储"""

    def __init__(self, session: AsyncSession):
        super().__init__(session, AgentCallRecord)

    async def create(self, data: dict[str, Any]) -> str:
        """
        创建调用记录

        Args:
            data: 记录数据，包含:
                - execution_id: 执行 ID
                - caller_level: 调用者层级 (L1/L2)
                - target_agent_id: 目标 Agent ID
                - target_agent_name: 目标 Agent 名称
                - operation_type: 操作类型
                - instruction: 指令内容
                - instruction_summary: 指令摘要
                - context: 上下文（可选）
                - timeout: 超时时间（可选）
                - retry_count: 重试次数（可选）
                - priority: 优先级（可选）

        Returns:
            记录 ID
        """
        record_id = str(uuid.uuid4())
        record = AgentCallRecord(
            id=record_id,
            execution_id=data["execution_id"],
            caller_level=data["caller_level"],
            target_agent_id=data["target_agent_id"],
            target_agent_name=data["target_agent_name"],
            operation_type=data["operation_type"],
            instruction=data["instruction"],
            instruction_summary=data["instruction_summary"],
            context=data.get("context"),
            timeout=data.get("timeout", Timeout.DEFAULT_AGENT_TIMEOUT),
            retry_count=data.get("retry_count", 1),
            priority=data.get("priority", "normal"),
            status="pending",
            start_time=datetime.now(UTC),
        )
        self.session.add(record)
        await self.session.flush()
        return record_id

    async def get_by_execution_id(self, execution_id: str) -> AgentCallRecord | None:
        """
        按 execution_id 查询记录

        Args:
            execution_id: 执行 ID

        Returns:
            调用记录，不存在返回 None
        """
        query = select(AgentCallRecord).where(
            AgentCallRecord.execution_id == execution_id
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def update_status(
        self,
        execution_id: str,
        status: str,
    ) -> bool:
        """
        更新记录状态

        Args:
            execution_id: 执行 ID
            status: 新状态 (pending/running/completed/failed)

        Returns:
            是否更新成功
        """
        query = (
            update(AgentCallRecord)
            .where(AgentCallRecord.execution_id == execution_id)
            .values(status=status)
        )
        result = await self.session.execute(query)
        await self.session.flush()
        return result.rowcount > 0

    async def complete(
        self,
        execution_id: str,
        success: bool,
        result: dict[str, Any] | None = None,
        result_summary: str | None = None,
    ) -> bool:
        """
        完成记录

        Args:
            execution_id: 执行 ID
            success: 是否成功
            result: 执行结果
            result_summary: 结果摘要

        Returns:
            是否更新成功
        """
        # 先获取记录以计算 duration
        record = await self.get_by_execution_id(execution_id)
        if not record:
            return False

        end_time = datetime.now(UTC)
        duration = None
        if record.start_time:
            # 确保 start_time 有时区信息
            start_time = record.start_time
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=UTC)
            duration = (end_time - start_time).total_seconds()

        query = (
            update(AgentCallRecord)
            .where(AgentCallRecord.execution_id == execution_id)
            .values(
                status="completed",
                success=success,
                result=result,
                result_summary=result_summary,
                end_time=end_time,
                duration=duration,
            )
        )
        result_obj = await self.session.execute(query)
        await self.session.flush()
        return result_obj.rowcount > 0

    async def fail(
        self,
        execution_id: str,
        error: str,
    ) -> bool:
        """
        标记记录失败

        Args:
            execution_id: 执行 ID
            error: 错误信息

        Returns:
            是否更新成功
        """
        # 先获取记录以计算 duration
        record = await self.get_by_execution_id(execution_id)
        if not record:
            return False

        end_time = datetime.now(UTC)
        duration = None
        if record.start_time:
            start_time = record.start_time
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=UTC)
            duration = (end_time - start_time).total_seconds()

        query = (
            update(AgentCallRecord)
            .where(AgentCallRecord.execution_id == execution_id)
            .values(
                status="failed",
                success=False,
                error=error,
                end_time=end_time,
                duration=duration,
            )
        )
        result = await self.session.execute(query)
        await self.session.flush()
        return result.rowcount > 0

    async def query(
        self,
        execution_id: str | None = None,
        target_agent_id: str | None = None,
        caller_level: str | None = None,
        status: str | None = None,
        operation_type: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentCallRecord]:
        """
        查询调用记录

        Args:
            execution_id: 执行 ID（精确匹配）
            target_agent_id: 目标 Agent ID
            caller_level: 调用者层级
            status: 状态
            operation_type: 操作类型
            start_time: 开始时间（范围查询）
            end_time: 结束时间（范围查询）
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            调用记录列表
        """
        query = select(AgentCallRecord)
        conditions = []

        if execution_id:
            conditions.append(AgentCallRecord.execution_id == execution_id)
        if target_agent_id:
            conditions.append(AgentCallRecord.target_agent_id == target_agent_id)
        if caller_level:
            conditions.append(AgentCallRecord.caller_level == caller_level)
        if status:
            conditions.append(AgentCallRecord.status == status)
        if operation_type:
            conditions.append(AgentCallRecord.operation_type == operation_type)
        if start_time:
            conditions.append(AgentCallRecord.start_time >= start_time)
        if end_time:
            conditions.append(AgentCallRecord.start_time <= end_time)

        if conditions:
            query = query.where(and_(*conditions))

        query = query.order_by(AgentCallRecord.start_time.desc())
        query = query.limit(limit).offset(offset)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_statistics(
        self,
        target_agent_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> dict[str, Any]:
        """
        获取统计信息

        Args:
            target_agent_id: 目标 Agent ID（可选过滤）
            start_time: 开始时间（可选过滤）
            end_time: 结束时间（可选过滤）

        Returns:
            统计信息字典，包含:
                - total: 总数
                - by_status: 按状态统计
                - by_caller_level: 按调用者层级统计
                - by_operation_type: 按操作类型统计
                - success_rate: 成功率
                - avg_duration: 平均执行时间
        """
        # 构建基础条件
        conditions = []
        if target_agent_id:
            conditions.append(AgentCallRecord.target_agent_id == target_agent_id)
        if start_time:
            conditions.append(AgentCallRecord.start_time >= start_time)
        if end_time:
            conditions.append(AgentCallRecord.start_time <= end_time)

        # 总数
        total_query = select(func.count(AgentCallRecord.id))
        if conditions:
            total_query = total_query.where(and_(*conditions))
        total_result = await self.session.execute(total_query)
        total = total_result.scalar() or 0

        # 按状态统计
        status_query = select(
            AgentCallRecord.status,
            func.count(AgentCallRecord.id).label("count"),
        ).group_by(AgentCallRecord.status)
        if conditions:
            status_query = status_query.where(and_(*conditions))
        status_result = await self.session.execute(status_query)
        by_status = {row.status: row.count for row in status_result}

        # 按调用者层级统计
        level_query = select(
            AgentCallRecord.caller_level,
            func.count(AgentCallRecord.id).label("count"),
        ).group_by(AgentCallRecord.caller_level)
        if conditions:
            level_query = level_query.where(and_(*conditions))
        level_result = await self.session.execute(level_query)
        by_caller_level = {row.caller_level: row.count for row in level_result}

        # 按操作类型统计
        op_query = select(
            AgentCallRecord.operation_type,
            func.count(AgentCallRecord.id).label("count"),
        ).group_by(AgentCallRecord.operation_type)
        if conditions:
            op_query = op_query.where(and_(*conditions))
        op_result = await self.session.execute(op_query)
        by_operation_type = {row.operation_type: row.count for row in op_result}

        # 成功率
        success_count = by_status.get("completed", 0)
        success_rate = (success_count / total * 100) if total > 0 else 0

        # 平均执行时间
        avg_query = select(func.avg(AgentCallRecord.duration)).where(
            AgentCallRecord.duration.isnot(None)
        )
        if conditions:
            avg_query = avg_query.where(and_(*conditions))
        avg_result = await self.session.execute(avg_query)
        avg_duration = avg_result.scalar() or 0

        return {
            "total": total,
            "by_status": by_status,
            "by_caller_level": by_caller_level,
            "by_operation_type": by_operation_type,
            "success_rate": round(success_rate, 2),
            "avg_duration": round(avg_duration, 2) if avg_duration else 0,
        }

    async def count_by_filters(
        self,
        target_agent_id: str | None = None,
        caller_level: str | None = None,
        status: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> int:
        """
        按条件统计数量

        Args:
            target_agent_id: 目标 Agent ID
            caller_level: 调用者层级
            status: 状态
            start_time: 开始时间
            end_time: 结束时间

        Returns:
            记录数量
        """
        query = select(func.count(AgentCallRecord.id))
        conditions = []

        if target_agent_id:
            conditions.append(AgentCallRecord.target_agent_id == target_agent_id)
        if caller_level:
            conditions.append(AgentCallRecord.caller_level == caller_level)
        if status:
            conditions.append(AgentCallRecord.status == status)
        if start_time:
            conditions.append(AgentCallRecord.start_time >= start_time)
        if end_time:
            conditions.append(AgentCallRecord.start_time <= end_time)

        if conditions:
            query = query.where(and_(*conditions))

        result = await self.session.execute(query)
        return result.scalar() or 0
