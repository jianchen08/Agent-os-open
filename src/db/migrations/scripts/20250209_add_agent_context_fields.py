"""
迁移脚本：添加 Agent 四层上下文字段

添加字段：
- static_vars: 第1层静态变量配置
- dynamic_vars: 第4层动态变量配置
"""

import asyncio
import logging

from sqlalchemy import text

from src.db.connection import get_db_manager

logger = logging.getLogger(__name__)


async def migrate():
    """执行迁移"""
    manager = get_db_manager()

    async with manager.get_session() as session:
        # 检查 static_vars 字段是否存在
        result = await session.execute(
            text("SELECT COUNT(*) FROM pragma_table_info('agent_configs') WHERE name='static_vars'")
        )
        has_static_vars = result.scalar() > 0

        if not has_static_vars:
            logger.info("添加 static_vars 字段到 agent_configs 表")
            await session.execute(
                text("ALTER TABLE agent_configs ADD COLUMN static_vars JSON DEFAULT '{}'")
            )
            logger.info("static_vars 字段添加成功")
        else:
            logger.info("static_vars 字段已存在，跳过")

        # 检查 dynamic_vars 字段是否存在
        result = await session.execute(
            text("SELECT COUNT(*) FROM pragma_table_info('agent_configs') WHERE name='dynamic_vars'")
        )
        has_dynamic_vars = result.scalar() > 0

        if not has_dynamic_vars:
            logger.info("添加 dynamic_vars 字段到 agent_configs 表")
            await session.execute(
                text("ALTER TABLE agent_configs ADD COLUMN dynamic_vars JSON DEFAULT '{}'")
            )
            logger.info("dynamic_vars 字段添加成功")
        else:
            logger.info("dynamic_vars 字段已存在，跳过")

        await session.commit()
        logger.info("迁移完成")


if __name__ == "__main__":
    asyncio.run(migrate())
