"""
执行记录仓储

提供 ExecutionRecord 的 CRUD 操作和查询功能
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from src.db.models import ExecutionRecord
from src.db.repositories.base import BaseRepository

logger = logging.getLogger(__name__)


class ExecutionRecordRepository(BaseRepository[ExecutionRecord]):
    """执行记录仓储"""

    def __init__(self, session: AsyncSession):
        super().__init__(session, ExecutionRecord)

    async def save_execution_record(
        self,
        session_id: str,
        message_data: dict[str, Any],
        parent_record_id: str | None = None,
        record_id: str | None = None,
        auto_commit: bool = True,
    ) -> str:
        """
        保存执行记录（统一接口）

        实现 upsert 语义：存在则更新，不存在则创建。
        所有执行记录的创建和更新都应通过此接口进行。

        Args:
            session_id: 会话 ID
            message_data: 消息数据（JSON 格式），由调用方构建
            parent_record_id: 父记录 ID（用于嵌套）
            record_id: 指定的记录 ID（可选，不指定则自动生成）
            auto_commit: 是否自动提交事务（默认 True）

        Returns:
            记录 ID

        Raises:
            ValueError: 如果无法生成有效 ID
        """
        if record_id is None:
            from src.utils.message_id_helper import generate_execution_record_id

            record_id = await generate_execution_record_id(
                self.session, session_id, parent_record_id
            )

        result = await self.session.execute(
            select(ExecutionRecord).where(ExecutionRecord.id == record_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.message_data = message_data
            flag_modified(existing, "message_data")
            logger.info(
                f"[ExecutionRecordRepo] 记录已存在，更新数据 | record_id={record_id}"
            )
        else:
            record = ExecutionRecord(
                id=record_id,
                session_id=session_id,
                parent_record_id=parent_record_id,
                message_data=message_data,
                created_at=datetime.now(UTC),
            )
            self.session.add(record)
            logger.info(
                f"[ExecutionRecordRepo] 创建新记录 | record_id={record_id} | "
                f"session_id={session_id}"
            )

        if auto_commit:
            await self.session.commit()

        return record_id

    async def get_execution_records(
        self,
        session_id: str,
        parent_record_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        获取会话的执行记录列表

        Args:
            session_id: 会话 ID
            parent_record_id: 父记录 ID（用于获取子记录）
            limit: 返回数量限制

        Returns:
            执行记录字典列表
        """
        query = select(ExecutionRecord).where(ExecutionRecord.session_id == session_id)

        if parent_record_id is not None:
            query = query.where(ExecutionRecord.parent_record_id == parent_record_id)
        else:
            # 只获取顶级记录（无父记录）
            query = query.where(ExecutionRecord.parent_record_id.is_(None))

        query = query.order_by(ExecutionRecord.created_at.desc())

        if limit is not None:
            query = query.limit(limit)

        result = await self.session.execute(query)
        records = result.scalars().all()

        # 转换为字典格式
        return [
            {
                "id": record.id,
                "session_id": record.session_id,
                "parent_record_id": record.parent_record_id,
                "message_data": record.message_data,
                "created_at": (
                    record.created_at.isoformat() if record.created_at else None
                ),
            }
            for record in records
        ]

    async def get_record_by_id(self, record_id: str) -> dict[str, Any] | None:
        """
        根据 ID 获取执行记录

        Args:
            record_id: 记录 ID

        Returns:
            执行记录字典，不存在返回 None
        """
        query = select(ExecutionRecord).where(ExecutionRecord.id == record_id)
        result = await self.session.execute(query)
        record = result.scalar_one_or_none()

        if record is None:
            return None

        return {
            "id": record.id,
            "session_id": record.session_id,
            "parent_record_id": record.parent_record_id,
            "message_data": record.message_data,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }

    async def get_children_records(self, parent_record_id: str) -> list[dict[str, Any]]:
        """
        获取子记录列表

        Args:
            parent_record_id: 父记录 ID

        Returns:
            子记录字典列表
        """
        query = (
            select(ExecutionRecord)
            .where(ExecutionRecord.parent_record_id == parent_record_id)
            .order_by(ExecutionRecord.created_at.asc())
        )

        result = await self.session.execute(query)
        records = result.scalars().all()

        return [
            {
                "id": record.id,
                "session_id": record.session_id,
                "parent_record_id": record.parent_record_id,
                "message_data": record.message_data,
                "created_at": (
                    record.created_at.isoformat() if record.created_at else None
                ),
            }
            for record in records
        ]

    async def update_execution_record(
        self, record_id: str, message_data: dict[str, Any]
    ) -> bool:
        """
        更新执行记录

        Args:
            record_id: 记录 ID
            message_data: 新的消息数据

        Returns:
            是否更新成功
        """
        query = (
            update(ExecutionRecord)
            .where(ExecutionRecord.id == record_id)
            .values(message_data=message_data)
        )

        result = await self.session.execute(query)
        await self.session.flush()
        return result.rowcount > 0

    async def get_execution_tree(
        self, session_id: str, max_depth: int = 5
    ) -> list[dict[str, Any]]:
        """
        获取会话的完整执行树

        使用批量查询优化，一次性加载所有记录后在内存中构建树，
        避免 N+1 查询问题。

        Args:
            session_id: 会话 ID
            max_depth: 最大嵌套深度

        Returns:
            执行树（嵌套字典列表）
        """
        # 一次性查询会话的所有记录，避免 N+1 查询
        query = (
            select(ExecutionRecord)
            .where(ExecutionRecord.session_id == session_id)
            .order_by(ExecutionRecord.created_at)
        )

        result = await self.session.execute(query)
        all_records = result.scalars().all()

        # 转换为字典列表
        records_dict = [
            {
                "id": r.id,
                "session_id": r.session_id,
                "parent_record_id": r.parent_record_id,
                "message_data": r.message_data,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in all_records
        ]

        # 按 parent_record_id 分组，构建父子关系映射
        children_map: dict[str, list[dict[str, Any]]] = {}
        top_records = []

        for record in records_dict:
            parent_id = record.get("parent_record_id")
            if parent_id is None:
                # 顶级记录
                top_records.append(record)
            else:
                # 子记录
                if parent_id not in children_map:
                    children_map[parent_id] = []
                children_map[parent_id].append(record)

        # 递归构建树（使用内存中的映射，不再查询数据库）
        def build_tree(record: dict[str, Any], current_depth: int) -> dict[str, Any]:
            if current_depth >= max_depth:
                return record

            # 从内存映射中获取子记录
            children = children_map.get(record["id"], [])
            record["children"] = [
                build_tree(child, current_depth + 1) for child in children
            ]

            return record

        # 构建完整的树
        tree = [build_tree(record, 0) for record in top_records]

        return tree

    async def count_by_session(self, session_id: str) -> int:
        """
        统计会话的执行记录数量

        Args:
            session_id: 会话 ID

        Returns:
            记录数量
        """
        query = select(func.count(ExecutionRecord.id)).where(
            ExecutionRecord.session_id == session_id
        )
        result = await self.session.execute(query)
        return result.scalar() or 0

    async def delete_by_session(self, session_id: str) -> int:
        """
        删除会话的所有执行记录

        Args:
            session_id: 会话 ID

        Returns:
            删除的记录数量
        """
        query = delete(ExecutionRecord).where(ExecutionRecord.session_id == session_id)
        result = await self.session.execute(query)
        await self.session.flush()
        return result.rowcount

    async def get_records_by_type(
        self, session_id: str, record_type: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        按记录类型查询

        Args:
            session_id: 会话 ID
            record_type: 记录类型（从 message_data.record_type 读取）
            limit: 返回数量限制

        Returns:
            执行记录列表
        """
        # 获取所有记录，然后在 Python 中过滤（兼容性更好）
        query = (
            select(ExecutionRecord)
            .where(ExecutionRecord.session_id == session_id)
            .order_by(ExecutionRecord.created_at.desc())
        )

        if limit:
            query = query.limit(limit)

        result = await self.session.execute(query)
        all_records = result.scalars().all()

        # 在 Python 中过滤 JSON 字段
        records = [
            r for r in all_records if r.message_data.get("record_type") == record_type
        ]

        return [
            {
                "id": record.id,
                "session_id": record.session_id,
                "parent_record_id": record.parent_record_id,
                "message_data": record.message_data,
                "created_at": (
                    record.created_at.isoformat() if record.created_at else None
                ),
            }
            for record in records
        ]

    async def get_records_by_executor(
        self, session_id: str, executor_type: str, executor_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        按执行者查询

        Args:
            session_id: 会话 ID
            executor_type: 执行者类型（agent/tool/workflow）
            executor_id: 执行者 ID
            limit: 返回数量限制

        Returns:
            执行记录列表
        """
        # 获取所有记录，然后在 Python 中过滤（兼容性更好）
        query = (
            select(ExecutionRecord)
            .where(ExecutionRecord.session_id == session_id)
            .order_by(ExecutionRecord.created_at.desc())
        )

        if limit:
            query = query.limit(limit)

        result = await self.session.execute(query)
        all_records = result.scalars().all()

        # 在 Python 中过滤 JSON 字段
        records = [
            r
            for r in all_records
            if r.message_data.get("executor", {}).get("type") == executor_type
            and r.message_data.get("executor", {}).get("id") == executor_id
        ]

        return [
            {
                "id": record.id,
                "session_id": record.session_id,
                "parent_record_id": record.parent_record_id,
                "message_data": record.message_data,
                "created_at": (
                    record.created_at.isoformat() if record.created_at else None
                ),
            }
            for record in records
        ]
