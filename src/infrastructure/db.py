"""PostgreSQL 连接管理模块。

提供异步数据库会话工厂和初始化功能，基于 SQLAlchemy async engine。
如果 SQLAlchemy 不可用（ImportError），所有函数返回 None 并发出 warning，
确保系统在无 PostgreSQL 环境下可降级运行。

暴露接口：
- get_async_session: 异步会话工厂
- init_db: 数据库初始化（建表）
- get_engine: 获取全局 async engine 实例
- close_engine: 关闭全局 engine
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# 全局 engine 实例（懒初始化）
_engine: Any | None = None

# 尝试导入 SQLAlchemy，失败时标记不可用
_SQLALCHEMY_AVAILABLE: bool
try:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    _SQLALCHEMY_AVAILABLE = True
except ImportError:
    _SQLALCHEMY_AVAILABLE = False
    logger.warning("SQLAlchemy 不可用，PostgreSQL 功能已禁用。安装方法: pip install sqlalchemy psycopg2-binary")


def _get_database_url() -> str | None:
    """从环境变量获取数据库连接字符串。

    读取 ``DATABASE_URL`` 环境变量。
    如果未设置，返回 None。

    Returns:
        数据库连接字符串，未配置时返回 None
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        logger.debug("DATABASE_URL 环境变量未设置，跳过 PostgreSQL 初始化")
        return None
    return url


def get_engine() -> Any | None:
    """获取全局 async engine 实例（懒初始化）。

    首次调用时根据 ``DATABASE_URL`` 环境变量创建 engine，
    后续调用返回同一实例。

    Returns:
        AsyncEngine 实例，SQLAlchemy 不可用或未配置时返回 None
    """
    global _engine  # noqa: PLW0603

    if not _SQLALCHEMY_AVAILABLE:
        return None

    if _engine is not None:
        return _engine

    url = _get_database_url()
    if not url:
        return None

    try:
        # 同步驱动转异步驱动：postgresql:// → postgresql+asyncpg://
        async_url = url
        if url.startswith("postgresql://"):
            async_url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgres://"):
            async_url = url.replace("postgres://", "postgresql+asyncpg://", 1)

        _engine = create_async_engine(async_url, echo=False, pool_pre_ping=True)
        logger.info("PostgreSQL async engine 已创建")
        return _engine
    except Exception as exc:
        logger.warning("创建 PostgreSQL engine 失败: %s", exc)
        return None


async def get_async_session() -> Any | None:
    """创建异步数据库会话。

    基于全局 engine 创建新的 AsyncSession 实例。
    调用方负责在 ``async with`` 或 try/finally 中关闭会话。

    Returns:
        AsyncSession 实例，SQLAlchemy 不可用或未配置时返回 None

    Example::

        session = await get_async_session()
        if session:
            async with session:
                result = await session.execute(text("SELECT 1"))
    """
    engine = get_engine()
    if engine is None:
        return None

    try:
        async_session_factory = sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        session = async_session_factory()
        return session
    except Exception as exc:
        logger.warning("创建数据库会话失败: %s", exc)
        return None


async def init_db() -> bool:
    """初始化数据库（创建表）。

    执行 DDL 语句创建 ``episodes_memory`` 和 ``semantic_memory`` 表。
    如果表已存在则跳过（IF NOT EXISTS）。

    Returns:
        初始化是否成功，SQLAlchemy 不可用或未配置时返回 False
    """
    engine = get_engine()
    if engine is None:
        return False

    try:
        from sqlalchemy import text  # noqa: PLC0415

        async with engine.begin() as conn:
            # 创建情景记忆表
            await conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS episodes_memory (
                    id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    session_id VARCHAR(64),
                    intent_text TEXT DEFAULT '',
                    intent_vector VECTOR(1536),
                    plan_dag JSONB,
                    execution_summary TEXT,
                    evaluation_report JSONB,
                    final_score FLOAT,
                    tags JSONB DEFAULT '[]',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            )

            # 创建语义记忆表
            await conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS semantic_memory (
                    id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    source_type VARCHAR(64) DEFAULT '',
                    source_id VARCHAR(64),
                    content TEXT DEFAULT '',
                    embedding VECTOR(1536),
                    memory_metadata JSONB DEFAULT '{}',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            )

        logger.info("PostgreSQL 数据库表初始化完成")
        return True
    except Exception as exc:
        logger.warning("PostgreSQL 数据库初始化失败: %s", exc)
        return False


async def close_engine() -> None:
    """关闭全局 engine，释放连接池资源。

    应在应用关闭时调用，确保所有连接正确释放。
    """
    global _engine  # noqa: PLW0603

    if _engine is not None:
        try:
            await _engine.dispose()
            logger.info("PostgreSQL engine 已关闭")
        except Exception as exc:
            logger.warning("关闭 PostgreSQL engine 失败: %s", exc)
        finally:
            _engine = None
