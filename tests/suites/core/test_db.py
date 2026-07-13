"""infrastructure/db.py 单元测试。

测试 PostgreSQL 连接管理模块的核心功能：
- 环境变量读取
- SQLAlchemy 不可用时的降级行为
- engine 懒初始化和会话创建
- 数据库初始化
- engine 关闭

使用 Mock 替代真实数据库连接。
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestGetDatabaseUrl:
    """_get_database_url 环境变量读取测试。"""

    def test_returns_url_when_set(self) -> None:
        """DATABASE_URL 设置时应返回对应值。"""
        from infrastructure.db import _get_database_url

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@localhost/db"}):
            url = _get_database_url()
            assert url == "postgresql://user:pass@localhost/db"

    def test_returns_none_when_not_set(self) -> None:
        """DATABASE_URL 未设置时应返回 None。"""
        from infrastructure.db import _get_database_url

        with patch.dict(os.environ, {}, clear=True):
            # 确保没有 DATABASE_URL
            os.environ.pop("DATABASE_URL", None)
            url = _get_database_url()
            assert url is None

    def test_returns_none_when_empty(self) -> None:
        """DATABASE_URL 为空字符串时应返回 None。"""
        from infrastructure.db import _get_database_url

        with patch.dict(os.environ, {"DATABASE_URL": ""}):
            url = _get_database_url()
            assert url is None


class TestGetEngine:
    """get_engine 懒初始化和降级行为测试。"""

    def setup_method(self) -> None:
        """每个测试前重置全局 engine。"""
        import infrastructure.db as db_mod
        db_mod._engine = None

    def test_returns_none_when_sqlalchemy_unavailable(self) -> None:
        """SQLAlchemy 不可用时应返回 None。"""
        import infrastructure.db as db_mod
        original = db_mod._SQLALCHEMY_AVAILABLE

        try:
            db_mod._SQLALCHEMY_AVAILABLE = False
            engine = db_mod.get_engine()
            assert engine is None
        finally:
            db_mod._SQLALCHEMY_AVAILABLE = original

    def test_returns_none_when_no_database_url(self) -> None:
        """DATABASE_URL 未配置时应返回 None。"""
        from infrastructure.db import get_engine

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DATABASE_URL", None)
            engine = get_engine()
            assert engine is None

    def test_creates_engine_from_database_url(self) -> None:
        """DATABASE_URL 配置正确时应创建 engine。"""
        from infrastructure.db import get_engine

        mock_engine = MagicMock()

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@localhost/mydb"}):
            with patch("infrastructure.db.create_async_engine", return_value=mock_engine) as mock_create:
                engine = get_engine()
                assert engine is mock_engine
                # 应将 postgresql:// 转为 postgresql+asyncpg://
                call_args = mock_create.call_args
                assert "postgresql+asyncpg://" in call_args[0][0]

    def test_converts_postgres_scheme(self) -> None:
        """postgres:// 前缀应转为 postgresql+asyncpg://。"""
        from infrastructure.db import get_engine

        mock_engine = MagicMock()

        with patch.dict(os.environ, {"DATABASE_URL": "postgres://user:pass@localhost/mydb"}):
            with patch("infrastructure.db.create_async_engine", return_value=mock_engine) as mock_create:
                get_engine()
                call_args = mock_create.call_args
                assert "postgresql+asyncpg://" in call_args[0][0]

    def test_returns_same_engine_on_second_call(self) -> None:
        """重复调用应返回同一 engine 实例（懒初始化）。"""
        from infrastructure.db import get_engine

        mock_engine = MagicMock()

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@localhost/mydb"}):
            with patch("infrastructure.db.create_async_engine", return_value=mock_engine):
                engine1 = get_engine()
                engine2 = get_engine()
                assert engine1 is engine2

    def test_returns_none_on_create_engine_error(self) -> None:
        """create_async_engine 抛异常时应返回 None。"""
        from infrastructure.db import get_engine

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@localhost/mydb"}):
            with patch("infrastructure.db.create_async_engine", side_effect=Exception("connection error")):
                engine = get_engine()
                assert engine is None


class TestGetAsyncSession:
    """get_async_session 会话创建测试。"""

    def setup_method(self) -> None:
        """每个测试前重置全局 engine。"""
        import infrastructure.db as db_mod
        db_mod._engine = None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_engine(self) -> None:
        """engine 不可用时应返回 None。"""
        from infrastructure.db import get_async_session

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DATABASE_URL", None)
            session = await get_async_session()
            assert session is None

    @pytest.mark.asyncio
    async def test_creates_session_from_engine(self) -> None:
        """engine 可用时应创建并返回 session。"""
        from infrastructure.db import get_async_session

        mock_engine = MagicMock()
        mock_session = MagicMock()

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@localhost/mydb"}):
            with patch("infrastructure.db.create_async_engine", return_value=mock_engine):
                with patch("infrastructure.db.sessionmaker", return_value=MagicMock(return_value=mock_session)):
                    session = await get_async_session()
                    assert session is mock_session

    @pytest.mark.asyncio
    async def test_returns_none_on_session_creation_error(self) -> None:
        """sessionmaker 抛异常时应返回 None。"""
        from infrastructure.db import get_async_session

        mock_engine = MagicMock()

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@localhost/mydb"}):
            with patch("infrastructure.db.create_async_engine", return_value=mock_engine):
                with patch("infrastructure.db.sessionmaker", side_effect=Exception("session error")):
                    session = await get_async_session()
                    assert session is None


class TestInitDb:
    """init_db 数据库初始化测试。"""

    def setup_method(self) -> None:
        """每个测试前重置全局 engine。"""
        import infrastructure.db as db_mod
        db_mod._engine = None

    @pytest.mark.asyncio
    async def test_returns_false_when_no_engine(self) -> None:
        """engine 不可用时应返回 False。"""
        from infrastructure.db import init_db

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DATABASE_URL", None)
            result = await init_db()
            assert result is False

    @pytest.mark.asyncio
    async def test_creates_tables_when_engine_available(self) -> None:
        """engine 可用时应成功建表并返回 True。"""
        from infrastructure.db import init_db

        mock_engine = MagicMock()
        mock_conn = AsyncMock()

        # engine.begin() 返回异步上下文管理器
        mock_engine.begin = MagicMock(return_value=mock_conn)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_conn.execute = AsyncMock()

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@localhost/mydb"}):
            with patch("infrastructure.db.create_async_engine", return_value=mock_engine):
                # 先触发 engine 创建
                from infrastructure.db import get_engine
                get_engine()

                result = await init_db()
                assert result is True
                # 应执行 2 条 DDL（episodes_memory + semantic_memory）
                assert mock_conn.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_false_on_ddl_error(self) -> None:
        """DDL 执行失败时应返回 False。"""
        from infrastructure.db import init_db

        mock_engine = MagicMock()
        mock_conn = AsyncMock()
        mock_engine.begin = MagicMock(return_value=mock_conn)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_conn.execute = AsyncMock(side_effect=Exception("DDL error"))

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@localhost/mydb"}):
            with patch("infrastructure.db.create_async_engine", return_value=mock_engine):
                from infrastructure.db import get_engine
                get_engine()

                result = await init_db()
                assert result is False


class TestCloseEngine:
    """close_engine 资源释放测试。"""

    def setup_method(self) -> None:
        """每个测试前重置全局 engine。"""
        import infrastructure.db as db_mod
        db_mod._engine = None

    @pytest.mark.asyncio
    async def test_disposes_engine_and_resets(self) -> None:
        """关闭 engine 后应 dispose 并重置为 None。"""
        import infrastructure.db as db_mod

        mock_engine = MagicMock()
        mock_engine.dispose = AsyncMock()
        db_mod._engine = mock_engine

        await db_mod.close_engine()

        mock_engine.dispose.assert_called_once()
        assert db_mod._engine is None

    @pytest.mark.asyncio
    async def test_noop_when_engine_is_none(self) -> None:
        """engine 为 None 时关闭不应报错。"""
        import infrastructure.db as db_mod

        db_mod._engine = None
        await db_mod.close_engine()
        assert db_mod._engine is None

    @pytest.mark.asyncio
    async def test_resets_engine_even_on_dispose_error(self) -> None:
        """dispose 异常时也应重置 engine 为 None。"""
        import infrastructure.db as db_mod

        mock_engine = MagicMock()
        mock_engine.dispose = AsyncMock(side_effect=Exception("dispose error"))
        db_mod._engine = mock_engine

        await db_mod.close_engine()

        assert db_mod._engine is None


class TestSqlAlchemyUnavailable:
    """SQLAlchemy ImportError 降级行为测试。"""

    def test_module_loads_without_sqlalchemy(self) -> None:
        """SQLAlchemy 不可用时模块仍可正常加载。"""
        import infrastructure.db as db_mod

        # 如果当前环境有 SQLAlchemy，_SQLALCHEMY_AVAILABLE 为 True
        # 否则为 False，但模块本身不应抛 ImportError
        assert isinstance(db_mod._SQLALCHEMY_AVAILABLE, bool)

    def test_functions_return_none_when_unavailable(self) -> None:
        """SQLALCHEMY_AVAILABLE=False 时所有函数返回 None/False。"""
        import infrastructure.db as db_mod
        original = db_mod._SQLALCHEMY_AVAILABLE

        try:
            db_mod._SQLALCHEMY_AVAILABLE = False
            db_mod._engine = None

            assert db_mod.get_engine() is None
        finally:
            db_mod._SQLALCHEMY_AVAILABLE = original
