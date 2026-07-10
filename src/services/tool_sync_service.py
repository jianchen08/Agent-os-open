"""
工具同步服务

负责保持工具代码定义与数据库记录的一致性：
- 工具注册时同步到数据库
- 工具删除时更新数据库状态
- 启动时同步所有内置工具
- 检测工具定义变更
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import ToolLibrary
from src.tools.types import Tool, ToolExample, ToolSource, ToolStatus

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """同步结果"""

    added: list[str]  # 新增的工具
    updated: list[str]  # 更新的工具
    deprecated: list[str]  # 标记废弃的工具
    unchanged: list[str]  # 未变更的工具
    errors: list[str]  # 错误信息


class ToolSyncService:
    """
    工具同步服务

    保持工具代码定义与数据库记录的一致性。
    """

    def __init__(self, session: AsyncSession):
        """
        初始化同步服务

        Args:
            session: 数据库会话
        """
        self._session = session

    async def sync_tool_to_db(self, tool: Tool) -> str:
        """
        同步单个工具到数据库

        如果工具不存在则创建，如果存在且 checksum 不同则更新。

        Args:
            tool: 工具定义

        Returns:
            数据库记录 ID
        """
        checksum = tool.compute_checksum()

        # 查找现有记录
        stmt = select(ToolLibrary).where(ToolLibrary.name == tool.name)
        result = await self._session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is None:
            # 创建新记录
            db_tool = self._tool_to_db_model(tool, checksum)
            self._session.add(db_tool)
            await self._session.flush()
            logger.info(f"[工具同步] 新增工具: {tool.name}")
            return db_tool.id
        if existing.checksum != checksum:
            # 更新现有记录
            await self._update_db_tool(existing, tool, checksum)
            logger.info(f"[工具同步] 更新工具: {tool.name}")
            return existing.id
        # 无需更新
        logger.debug(f"[工具同步] 工具未变更: {tool.name}")
        return existing.id

    async def sync_all_builtin_tools(self) -> SyncResult:
        """
        同步所有内置工具到数据库

        对比代码定义和数据库记录：
        - 代码有、数据库无 → 新增
        - 代码有、数据库有但 checksum 不同 → 更新
        - 代码无、数据库有 → 标记废弃

        Returns:
            同步结果
        """
        from src.tools.builtin import get_all_builtin_tools  # noqa: PLC0415

        result = SyncResult(added=[], updated=[], deprecated=[], unchanged=[], errors=[])

        # 获取所有内置工具定义
        code_tools: dict[str, Tool] = {}
        try:
            for tool_instance in get_all_builtin_tools():
                tool_def = tool_instance.get_tool_definition()
                code_tools[tool_def.name] = tool_def
        except Exception as e:
            result.errors.append(f"加载内置工具失败: {e}")
            return result

        # 获取数据库中所有内置工具（source_type 为 code 或 builtin）
        stmt = select(ToolLibrary).where(ToolLibrary.source_type.in_(["code", "builtin"]))
        db_result = await self._session.execute(stmt)
        db_tools = {t.name: t for t in db_result.scalars().all()}

        # 对比并同步
        code_names = set(code_tools.keys())
        db_names = set(db_tools.keys())

        # 新增的工具
        for name in code_names - db_names:
            try:
                tool = code_tools[name]
                checksum = tool.compute_checksum()
                db_tool = self._tool_to_db_model(tool, checksum)
                self._session.add(db_tool)
                result.added.append(name)
            except Exception as e:
                result.errors.append(f"新增工具 {name} 失败: {e}")

        # 可能需要更新的工具
        for name in code_names & db_names:
            try:
                tool = code_tools[name]
                db_tool = db_tools[name]
                checksum = tool.compute_checksum()

                if db_tool.checksum != checksum:
                    await self._update_db_tool(db_tool, tool, checksum)
                    result.updated.append(name)
                else:
                    result.unchanged.append(name)
            except Exception as e:
                result.errors.append(f"更新工具 {name} 失败: {e}")

        # 代码中已删除的工具（标记废弃）
        for name in db_names - code_names:
            try:
                db_tool = db_tools[name]
                db_tool.status = "deprecated"
                result.deprecated.append(name)
            except Exception as e:
                result.errors.append(f"废弃工具 {name} 失败: {e}")

        await self._session.flush()

        logger.info(
            f"[工具同步] 完成: 新增 {len(result.added)}, "
            f"更新 {len(result.updated)}, "
            f"废弃 {len(result.deprecated)}, "
            f"未变更 {len(result.unchanged)}"
        )

        return result

    async def remove_tool_from_db(self, name: str, hard_delete: bool = False) -> bool:
        """
        从数据库删除或废弃工具

        Args:
            name: 工具名称
            hard_delete: 是否硬删除（默认软删除，标记为 deprecated）

        Returns:
            是否成功
        """
        stmt = select(ToolLibrary).where(ToolLibrary.name == name)
        result = await self._session.execute(stmt)
        db_tool = result.scalar_one_or_none()

        if db_tool is None:
            logger.warning(f"[工具同步] 工具不存在: {name}")
            return False

        if hard_delete:
            await self._session.delete(db_tool)
            logger.info(f"[工具同步] 硬删除工具: {name}")
        else:
            db_tool.status = "deprecated"
            logger.info(f"[工具同步] 软删除工具: {name}")

        await self._session.flush()
        return True

    async def load_tool_from_db(self, name: str) -> Tool | None:
        """
        从数据库加载工具定义

        Args:
            name: 工具名称

        Returns:
            工具定义，不存在返回 None
        """
        stmt = select(ToolLibrary).where(ToolLibrary.name == name, ToolLibrary.status == "active")
        result = await self._session.execute(stmt)
        db_tool = result.scalar_one_or_none()

        if db_tool is None:
            return None

        return self._db_model_to_tool(db_tool)

    async def load_all_tools_from_db(self, source_type: str | None = None, status: str = "active") -> list[Tool]:
        """
        从数据库加载所有工具定义

        Args:
            source_type: 来源类型过滤（可选）
            status: 状态过滤

        Returns:
            工具定义列表
        """
        stmt = select(ToolLibrary).where(ToolLibrary.status == status)

        if source_type:
            stmt = stmt.where(ToolLibrary.source_type == source_type)

        result = await self._session.execute(stmt)
        db_tools = result.scalars().all()

        return [self._db_model_to_tool(t) for t in db_tools]

    async def get_tool_names_from_db(self, source_type: str | None = None, status: str = "active") -> set[str]:
        """
        获取数据库中的工具名称列表（轻量查询）

        Args:
            source_type: 来源类型过滤
            status: 状态过滤

        Returns:
            工具名称集合
        """
        stmt = select(ToolLibrary.name).where(ToolLibrary.status == status)

        if source_type:
            stmt = stmt.where(ToolLibrary.source_type == source_type)

        result = await self._session.execute(stmt)
        return {row[0] for row in result.all()}

    async def record_tool_usage(self, name: str, success: bool) -> None:
        """
        记录工具使用情况

        Args:
            name: 工具名称
            success: 是否成功
        """
        stmt = select(ToolLibrary).where(ToolLibrary.name == name)
        result = await self._session.execute(stmt)
        db_tool = result.scalar_one_or_none()

        if db_tool:
            if success:
                db_tool.success_count = (db_tool.success_count or 0) + 1
            else:
                db_tool.failure_count = (db_tool.failure_count or 0) + 1
            db_tool.last_used_at = datetime.utcnow()
            await self._session.flush()

    def _tool_to_db_model(self, tool: Tool, checksum: str) -> ToolLibrary:
        """
        将 Tool 转换为数据库模型

        Args:
            tool: 工具定义
            checksum: 校验和

        Returns:
            数据库模型实例
        """
        return ToolLibrary(
            name=tool.name,
            description=tool.description,
            when_to_use=tool.when_to_use or None,
            when_not_to_use=tool.when_not_to_use or None,
            examples=[e.model_dump() for e in tool.examples] if tool.examples else None,
            caveats=tool.caveats or None,
            input_schema=tool.input_schema,
            output_schema=tool.output_schema,
            source_type=tool.source.value if tool.source else "custom",
            category=tool.category.value if tool.category else None,
            level=tool.level.value if tool.level else "user",
            version=tool.version,
            tags=tool.tags or None,
            checksum=checksum,
            status=tool.status.value if tool.status else "active",
            requires_approval=tool.requires_approval,
        )

    async def _update_db_tool(self, db_tool: ToolLibrary, tool: Tool, checksum: str) -> None:
        """
        更新数据库中的工具记录

        Args:
            db_tool: 数据库记录
            tool: 新的工具定义
            checksum: 新的校验和
        """
        db_tool.description = tool.description
        db_tool.when_to_use = tool.when_to_use or None
        db_tool.when_not_to_use = tool.when_not_to_use or None
        db_tool.examples = [e.model_dump() for e in tool.examples] if tool.examples else None
        db_tool.caveats = tool.caveats or None
        db_tool.input_schema = tool.input_schema
        db_tool.output_schema = tool.output_schema
        db_tool.category = tool.category.value if tool.category else None
        db_tool.level = tool.level.value if tool.level else "user"
        db_tool.version = tool.version
        db_tool.tags = tool.tags or None
        db_tool.checksum = checksum
        db_tool.status = tool.status.value if tool.status else "active"
        db_tool.requires_approval = tool.requires_approval

    def _db_model_to_tool(self, db_tool: ToolLibrary) -> Tool:
        """
        将数据库模型转换为 Tool

        Args:
            db_tool: 数据库记录

        Returns:
            工具定义
        """
        from src.tools.types import ToolCategory, ToolLevel  # noqa: PLC0415

        # 解析 examples
        examples = []
        if db_tool.examples:
            for e in db_tool.examples:
                examples.append(
                    ToolExample(
                        input=e.get("input", {}),
                        output=e.get("output"),
                        description=e.get("description"),
                    )
                )

        # 解析枚举值
        try:
            source = ToolSource(db_tool.source_type)
        except ValueError:
            source = ToolSource.CODE

        try:
            category = ToolCategory(db_tool.category) if db_tool.category else None
        except ValueError:
            category = None

        try:
            level = ToolLevel(db_tool.level) if db_tool.level else ToolLevel.USER
        except ValueError:
            level = ToolLevel.USER

        try:
            status = ToolStatus(db_tool.status) if db_tool.status else ToolStatus.ACTIVE
        except ValueError:
            status = ToolStatus.ACTIVE

        input_schema = db_tool.input_schema or {}
        output_schema = db_tool.output_schema

        return Tool(
            name=db_tool.name,
            description=db_tool.description or "",
            when_to_use=db_tool.when_to_use or [],
            when_not_to_use=db_tool.when_not_to_use or [],
            examples=examples,
            caveats=db_tool.caveats or [],
            input_schema=input_schema,
            output_schema=output_schema,
            source=source,
            category=category,
            level=level,
            version=db_tool.version or "1.0.0",
            tags=db_tool.tags or [],
            status=status,
            requires_approval=db_tool.requires_approval or False,
            db_id=db_tool.id,
            checksum=db_tool.checksum,
        )


async def sync_builtin_tools_on_startup(session: AsyncSession) -> SyncResult:
    """
    启动时同步内置工具（便捷函数）

    Args:
        session: 数据库会话

    Returns:
        同步结果
    """
    service = ToolSyncService(session)
    return await service.sync_all_builtin_tools()
