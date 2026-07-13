"""PgVectorStore 向量存储（含自动降级）测试。

覆盖场景：
- 默认关闭，所有操作委托给 JsonMemoryStore
- 配置启用但 pgvector 不可用 → 自动降级
- 配置启用且 pgvector 可用 → search 使用向量检索
- store/delete 始终不依赖 pgvector
- search 降级到关键词搜索
- 降级过程有日志提示
"""

from __future__ import annotations

import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.skip(
    reason="memory.pgvector_store 模块已不存在，"
           "PgVectorStore/PgVectorConfig 已被替换为 memory.storage.pgvector_retriever.PgVectorRetriever（API完全不同）"
)


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def tmp_data_dir(tmp_path):
    """创建临时数据目录。"""
    data_dir = tmp_path / "memory"
    data_dir.mkdir()
    return str(data_dir)


@pytest.fixture
def disabled_config(tmp_data_dir):
    """默认关闭的配置。"""
    return PgVectorConfig(enabled=False, data_dir=tmp_data_dir)


@pytest.fixture
def enabled_config(tmp_data_dir):
    """启用了但没有连接字符串的配置。"""
    return PgVectorConfig(enabled=True, data_dir=tmp_data_dir)


@pytest.fixture
def full_config(tmp_data_dir):
    """完整启用的配置（含模拟连接字符串和嵌入服务）。"""
    embedding_service = AsyncMock()
    embedding_service.embed_text = AsyncMock(return_value=[0.1] * 8)
    return PgVectorConfig(
        enabled=True,
        connection_string="postgresql+asyncpg://test:test@localhost/test",
        data_dir=tmp_data_dir,
        embedding_service=embedding_service,
    )


@pytest.fixture
def sample_episode():
    """示例情景记忆。"""
    return Episode(
        id="ep-001",
        user_id="user-001",
        intent_text="实现用户登录功能",
        execution_summary="完成了 JWT 认证模块",
        tags=["认证", "JWT"],
    )


@pytest.fixture
def sample_knowledge():
    """示例知识。"""
    return Knowledge(
        id="kn-001",
        user_id="user-001",
        source_type="episode",
        content="JWT token 有效期为 24 小时",
    )


# ============================================================
# 1. 默认关闭：所有操作委托给 JsonMemoryStore
# ============================================================


class TestDisabledByDefault:
    """PgVector 默认关闭时的行为。"""

    @pytest.mark.asyncio
    async def test_save_delegates_to_fallback(self, disabled_config, sample_episode):
        store = PgVectorStore(disabled_config)
        entry_id = await store.save(sample_episode)
        assert entry_id == "ep-001"

    @pytest.mark.asyncio
    async def test_load_delegates_to_fallback(self, disabled_config, sample_episode):
        store = PgVectorStore(disabled_config)
        await store.save(sample_episode)
        loaded = await store.load("ep-001", "episode")
        assert loaded is not None
        assert loaded.id == "ep-001"

    @pytest.mark.asyncio
    async def test_delete_delegates_to_fallback(self, disabled_config, sample_episode):
        store = PgVectorStore(disabled_config)
        await store.save(sample_episode)
        result = await store.delete("ep-001")
        assert result is True
        loaded = await store.load("ep-001", "episode")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_search_delegates_to_fallback(self, disabled_config, sample_episode):
        store = PgVectorStore(disabled_config)
        await store.save(sample_episode)
        results = await store.search("登录")
        assert len(results) >= 1
        assert results[0].content != ""

    @pytest.mark.asyncio
    async def test_pg_not_available_when_disabled(self, disabled_config):
        store = PgVectorStore(disabled_config)
        assert store.pg_available is False

    @pytest.mark.asyncio
    async def test_default_config_disabled(self, tmp_data_dir):
        """PgVectorConfig 默认 enabled=False。"""
        config = PgVectorConfig(data_dir=tmp_data_dir)
        assert config.enabled is False


# ============================================================
# 2. 配置启用但 pgvector 不可用 → 自动降级
# ============================================================


class TestAutoDegradation:
    """pgvector 不可用时的自动降级行为。"""

    @pytest.mark.asyncio
    async def test_enabled_but_no_connection_string_degrades(
        self, enabled_config, sample_episode, caplog,
    ):
        """启用但没有连接字符串 → 降级到 JSON，有日志。"""
        store = PgVectorStore(enabled_config)
        with caplog.at_level(logging.INFO, logger="memory.pgvector_store"):
            await store.search("test")

        # store 应该正常工作（通过 fallback）
        entry_id = await store.save(sample_episode)
        assert entry_id == "ep-001"
        assert store.pg_available is False

    @pytest.mark.asyncio
    async def test_connection_failure_degrades(
        self, full_config, sample_episode, caplog,
    ):
        """连接失败 → 降级到 JSON，有日志。"""
        store = PgVectorStore(full_config)

        # 直接 mock _create_pg_engine 使其抛异常
        async def _fail_initialize():
            raise Exception("Connection refused")

        store._create_pg_engine = _fail_initialize

        with caplog.at_level(logging.WARNING, logger="memory.pgvector_store"):
            results = await store.search("test")

        assert store.pg_available is False
        # 即使降级，save/delete 仍然正常工作
        await store.save(sample_episode)
        loaded = await store.load("ep-001", "episode")
        assert loaded is not None

    @pytest.mark.asyncio
    async def test_missing_dependency_degrades(self, full_config, caplog):
        """缺少 sqlalchemy 依赖 → 降级到 JSON，有日志。"""
        store = PgVectorStore(full_config)

        async def _import_error():
            raise ImportError("No module named 'sqlalchemy'")

        store._create_pg_engine = _import_error

        with caplog.at_level(logging.WARNING, logger="memory.pgvector_store"):
            results = await store.search("test")

        assert store.pg_available is False

    @pytest.mark.asyncio
    async def test_pgvector_extension_not_installed(self, full_config, caplog):
        """pgvector 扩展未安装 → 降级，有日志。"""
        store = PgVectorStore(full_config)

        async def _no_extension():
            raise RuntimeError("PostgreSQL 中未安装 pgvector 扩展")

        store._create_pg_engine = _no_extension

        with caplog.at_level(logging.WARNING, logger="memory.pgvector_store"):
            await store.search("test")

        assert store.pg_available is False


# ============================================================
# 3. store/delete 始终不依赖 pgvector
# ============================================================


class TestStoreDeleteIndependent:
    """store 和 delete 不依赖 pgvector。"""

    @pytest.mark.asyncio
    async def test_save_works_without_pgvector(
        self, disabled_config, sample_episode, sample_knowledge,
    ):
        """没有 pgvector 时 save 仍然正常工作。"""
        store = PgVectorStore(disabled_config)

        ep_id = await store.save(sample_episode)
        assert ep_id == "ep-001"

        kn_id = await store.save(sample_knowledge, "semantic")
        assert kn_id == "kn-001"

    @pytest.mark.asyncio
    async def test_delete_works_without_pgvector(
        self, disabled_config, sample_episode,
    ):
        """没有 pgvector 时 delete 仍然正常工作。"""
        store = PgVectorStore(disabled_config)
        await store.save(sample_episode)
        result = await store.delete("ep-001")
        assert result is True

    @pytest.mark.asyncio
    async def test_load_works_without_pgvector(
        self, disabled_config, sample_knowledge,
    ):
        """没有 pgvector 时 load 仍然正常工作。"""
        store = PgVectorStore(disabled_config)
        await store.save(sample_knowledge, "semantic")
        loaded = await store.load("kn-001", "semantic")
        assert loaded is not None
        assert loaded.id == "kn-001"

    @pytest.mark.asyncio
    async def test_save_returns_correct_id_for_episode(
        self, disabled_config, sample_episode,
    ):
        """save Episode 返回正确的 ID。"""
        store = PgVectorStore(disabled_config)
        result = await store.save(sample_episode)
        assert result == sample_episode.id

    @pytest.mark.asyncio
    async def test_save_returns_correct_id_for_knowledge(
        self, disabled_config, sample_knowledge,
    ):
        """save Knowledge 返回正确的 ID。"""
        store = PgVectorStore(disabled_config)
        result = await store.save(sample_knowledge, "semantic")
        assert result == sample_knowledge.id

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, disabled_config):
        """删除不存在的条目返回 False。"""
        store = PgVectorStore(disabled_config)
        result = await store.delete("nonexistent-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_load_nonexistent_returns_none(self, disabled_config):
        """加载不存在的条目返回 None。"""
        store = PgVectorStore(disabled_config)
        result = await store.load("nonexistent-id")
        assert result is None


# ============================================================
# 4. pgvector 可用时的 search 行为
# ============================================================


class TestSearchWithPgVector:
    """pgvector 可用时的搜索行为。"""

    @pytest.mark.asyncio
    async def test_search_uses_pgvector_when_available(
        self, full_config, sample_episode,
    ):
        """pgvector 可用时，search 使用向量检索。"""
        store = PgVectorStore(full_config)

        # 先保存数据到 fallback
        await store.save(sample_episode)

        # 模拟 pgvector 可用状态
        store._pg_available = True
        store._initialized = True

        # 模拟向量检索返回结果
        mock_session = AsyncMock()
        mock_search_result = MagicMock()
        mock_row = MagicMock()
        mock_row.id = "ep-001"
        mock_row.intent_text = "实现用户登录"
        mock_row.execution_summary = "完成了 JWT 认证"
        mock_row.similarity = 0.95
        mock_search_result.fetchall.return_value = [mock_row]
        mock_session.execute = AsyncMock(return_value=mock_search_result)
        mock_session.flush = AsyncMock()
        store._session = mock_session

        results = await store.search("登录")

        assert store.pg_available is True
        # 向量检索返回了结果
        assert len(results) >= 1
        assert results[0].score == 0.95

    @pytest.mark.asyncio
    async def test_search_falls_back_on_pgvector_error(
        self, full_config, sample_episode, caplog,
    ):
        """pgvector 检索失败时，降级到关键词搜索。"""
        store = PgVectorStore(full_config)

        # 先保存一条数据到 fallback
        await store.save(sample_episode)

        # 模拟 pgvector 可用但 search 抛异常
        store._pg_available = True
        store._initialized = True
        store._session = AsyncMock()
        store._session.execute = AsyncMock(side_effect=Exception("Vector search failed"))

        with caplog.at_level(logging.WARNING, logger="memory.pgvector_store"):
            results = await store.search("登录")

        # 应该降级到关键词搜索，仍然能找到结果
        assert len(results) >= 1


# ============================================================
# 5. 配置验证
# ============================================================


class TestConfig:
    """配置相关测试。"""

    def test_default_config_disabled(self):
        """默认配置应该是关闭的。"""
        config = PgVectorConfig()
        assert config.enabled is False

    def test_default_config_values(self):
        """默认配置值合理。"""
        config = PgVectorConfig()
        assert config.data_dir == "data/memory"
        assert config.connection_string == ""
        assert config.embedding_service is None
        assert config.vector_dimension == 1536

    def test_custom_config(self):
        """自定义配置生效。"""
        config = PgVectorConfig(
            enabled=True,
            connection_string="postgresql://localhost/test",
            data_dir="/tmp/memory",
        )
        assert config.enabled is True
        assert config.connection_string == "postgresql://localhost/test"
        assert config.data_dir == "/tmp/memory"

    def test_none_config_uses_default(self):
        """传 None 配置时使用默认值。"""
        store = PgVectorStore(None)
        assert store.pg_available is False


# ============================================================
# 6. 接口实现验证
# ============================================================


class TestInterfaceCompliance:
    """验证 PgVectorStore 实现了 IMemoryStore 接口。"""

    def test_implements_imemory_store(self):
        """PgVectorStore 应该实现 IMemoryStore 接口。"""
        from memory.ports import IMemoryStore
        assert issubclass(PgVectorStore, IMemoryStore)

    def test_has_required_methods(self):
        """PgVectorStore 应该有 IMemoryStore 要求的所有方法。"""
        store = PgVectorStore(None)
        assert hasattr(store, "save")
        assert hasattr(store, "load")
        assert hasattr(store, "delete")
        assert hasattr(store, "search")
        assert callable(store.save)
        assert callable(store.load)
        assert callable(store.delete)
        assert callable(store.search)


# ============================================================
# 7. 日志验证
# ============================================================


class TestLogging:
    """降级日志验证。"""

    @pytest.mark.asyncio
    async def test_disabled_logs_info(self, disabled_config, caplog):
        """未启用时记录 info 日志。"""
        store = PgVectorStore(disabled_config)
        with caplog.at_level(logging.INFO, logger="memory.pgvector_store"):
            await store.search("test")

        # 应该有日志说明未启用
        assert any(
            "未启用" in record.message or "JSON" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_connection_failure_logs_warning(self, full_config, caplog):
        """连接失败时记录 warning 日志。"""
        store = PgVectorStore(full_config)

        async def _fail_initialize():
            raise Exception("Connection refused")

        store._create_pg_engine = _fail_initialize

        with caplog.at_level(logging.WARNING, logger="memory.pgvector_store"):
            await store.search("test")

        assert any(
            "降级" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_search_fallback_logs_warning(self, full_config, caplog):
        """search 向量检索失败时记录 warning 日志。"""
        store = PgVectorStore(full_config)
        store._pg_available = True
        store._session = AsyncMock()
        store._session.execute = AsyncMock(side_effect=Exception("Search error"))

        with caplog.at_level(logging.WARNING, logger="memory.pgvector_store"):
            await store.search("test")

        assert any(
            "向量检索失败" in record.message or "降级" in record.message
            for record in caplog.records
        )


# ============================================================
# 8. 边界条件
# ============================================================


class TestEdgeCases:
    """边界条件测试。"""

    @pytest.mark.asyncio
    async def test_search_empty_query(self, disabled_config):
        """空查询不抛异常。"""
        store = PgVectorStore(disabled_config)
        results = await store.search("")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_no_results(self, disabled_config):
        """没有匹配结果时返回空列表。"""
        store = PgVectorStore(disabled_config)
        results = await store.search("不存在的查询xyz123")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_double_initialization(self, full_config):
        """多次调用 _try_initialize 不会重复初始化。"""
        store = PgVectorStore(full_config)

        async def _fail():
            raise Exception("fail")

        store._create_pg_engine = _fail

        await store._try_initialize()
        await store._try_initialize()  # 第二次调用

        # _initialized 应该为 True，不会重复尝试
        assert store._initialized is True

    @pytest.mark.asyncio
    async def test_save_with_none_embedding(
        self, disabled_config,
    ):
        """保存没有向量的条目也正常工作。"""
        store = PgVectorStore(disabled_config)
        ep = Episode(id="ep-no-vec", user_id="u1", intent_text="test")
        assert ep.intent_vector is None
        entry_id = await store.save(ep)
        assert entry_id == "ep-no-vec"

    @pytest.mark.asyncio
    async def test_pgvector_sync_failure_does_not_affect_save(
        self, full_config, sample_episode, caplog,
    ):
        """pgvector 同步失败不影响主存储的 save。"""
        store = PgVectorStore(full_config)
        store._pg_available = True
        store._session = AsyncMock()
        store._session.execute = AsyncMock(side_effect=Exception("PG error"))
        store._session.flush = AsyncMock(side_effect=Exception("PG error"))

        with caplog.at_level(logging.DEBUG, logger="memory.pgvector_store"):
            entry_id = await store.save(sample_episode)

        # save 仍然成功（fallback 保存成功）
        assert entry_id == "ep-001"
        loaded = await store.load("ep-001")
        assert loaded is not None

    @pytest.mark.asyncio
    async def test_pgvector_delete_failure_does_not_affect_delete(
        self, full_config, sample_episode, caplog,
    ):
        """pgvector 删除失败不影响主存储的 delete。"""
        store = PgVectorStore(full_config)
        await store.save(sample_episode)

        store._pg_available = True
        store._session = AsyncMock()
        store._session.execute = AsyncMock(side_effect=Exception("PG error"))
        store._session.flush = AsyncMock(side_effect=Exception("PG error"))

        with caplog.at_level(logging.DEBUG, logger="memory.pgvector_store"):
            result = await store.delete("ep-001")

        # delete 仍然成功
        assert result is True
