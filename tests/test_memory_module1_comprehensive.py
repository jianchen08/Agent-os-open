"""记忆系统升级模块一：全面功能验证测试。

以用户真实使用场景为核心，模拟用户操作验证功能真正可用。
覆盖模块：
1. PgVectorStore 向量存储（含自动降级）
2. TagWave 检索算法（透镜-拓展-聚焦三阶段）
3. 记忆维护机制（TTL 过期清理、容量限制淘汰、重要性衰减）
4. 记忆监控指标（延迟/命中率/存储容量）
5. 递归压缩策略回归验证
"""

from __future__ import annotations

import math
import os
import time
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(
    reason="引用的模块已不存在或重构：memory.pgvector_store(PgVectorStore) -> memory.storage.pgvector_retriever(PgVectorRetriever)，"
           "memory.tagwave_retriever(TagWaveRetriever) -> memory.wave_retriever(WaveRetriever)，"
           "memory.memory_metrics 已移除，MaintenanceConfig 已重构，"
           "compressor 子模块 reader/writer/store/store_manager/structured 已移除"
)


# ============================================================
# 模块1：PgVectorStore 向量存储 — 真实用户场景
# ============================================================


class TestPgVectorStoreRealUserScenario:
    """模拟用户真实使用 PgVectorStore 的全流程。

    用户场景：
    1. 创建 store（默认关闭）
    2. 存入多条记忆
    3. 搜索记忆
    4. 删除记忆
    5. 在整个过程中不依赖 pgvector
    """

    @pytest.fixture
    def tmp_data_dir(self, tmp_path):
        data_dir = tmp_path / "memory"
        data_dir.mkdir()
        return str(data_dir)

    @pytest.fixture
    def store(self, tmp_data_dir):
        from memory.pgvector_store import PgVectorConfig, PgVectorStore
        config = PgVectorConfig(enabled=False, data_dir=tmp_data_dir)
        return PgVectorStore(config)

    @pytest.mark.asyncio
    async def test_full_lifecycle_store_search_delete(self, store):
        """用户真实场景：存入记忆→搜索→验证结果→删除→确认删除。

        验证：store/search/delete 接口在无 pgvector 时正常工作。
        """
        from memory.types import Episode, Knowledge

        # 步骤1: 存入情景记忆
        ep1 = Episode(
            id="lifecycle-ep1",
            user_id="user-test",
            intent_text="实现用户认证模块",
            execution_summary="完成了JWT认证，包括token生成和验证",
            tags=["认证", "JWT", "安全"],
        )
        ep2 = Episode(
            id="lifecycle-ep2",
            user_id="user-test",
            intent_text="数据库迁移脚本",
            execution_summary="完成了MySQL到PostgreSQL的迁移",
            tags=["数据库", "迁移"],
        )
        await store.save(ep1)
        await store.save(ep2)

        # 步骤2: 存入知识
        kn1 = Knowledge(
            id="lifecycle-kn1",
            user_id="user-test",
            source_type="episode",
            content="JWT token 有效期为24小时，需要定期刷新",
        )
        kn_id = await store.save(kn1, "semantic")
        assert kn_id == "lifecycle-kn1"

        # 步骤3: 搜索记忆（关键词搜索）
        results = await store.search("JWT认证")
        assert isinstance(results, list)
        # 应该能搜索到包含JWT相关内容的记忆
        assert len(results) >= 1
        # 搜索结果内容不为空
        for r in results:
            assert r.content != ""

        # 步骤4: 搜索数据库相关
        db_results = await store.search("数据库")
        assert isinstance(db_results, list)

        # 步骤5: 删除记忆
        deleted = await store.delete("lifecycle-ep1")
        assert deleted is True

        # 步骤6: 确认删除成功
        loaded = await store.load("lifecycle-ep1", "episode")
        assert loaded is None

        # 另一条应该还在
        loaded2 = await store.load("lifecycle-ep2", "episode")
        assert loaded2 is not None
        assert loaded2.id == "lifecycle-ep2"

        # 步骤7: pgvector 始终不可用
        assert store.pg_available is False

    @pytest.mark.asyncio
    async def test_config_controls_enable_disable(self, tmp_data_dir):
        """用户场景：通过配置控制是否启用 pgvector。

        验证：配置项可控制 pgvector 启用/禁用。
        """
        from memory.pgvector_store import PgVectorConfig, PgVectorStore

        # 默认配置 → 关闭
        default_store = PgVectorStore(PgVectorConfig(data_dir=tmp_data_dir))
        assert default_store.pg_available is False
        # 即使执行搜索也不会尝试连接
        await default_store.search("test")
        assert default_store.pg_available is False

        # 启用但无连接 → 降级
        enabled_store = PgVectorStore(
            PgVectorConfig(enabled=True, data_dir=tmp_data_dir)
        )
        assert enabled_store.pg_available is False
        await enabled_store.search("test")
        # 没有连接字符串，仍然是降级状态
        assert enabled_store.pg_available is False

    @pytest.mark.asyncio
    async def test_vector_search_only_triggered_on_search(self, tmp_data_dir):
        """用户场景：向量检索仅在 search 时触发。

        验证：store/delete 不会触发 pgvector 初始化。
        """
        from memory.pgvector_store import PgVectorConfig, PgVectorStore
        from memory.types import Episode

        config = PgVectorConfig(
            enabled=True,
            connection_string="postgresql+asyncpg://test:test@localhost/test",
            data_dir=tmp_data_dir,
        )
        store = PgVectorStore(config)

        ep = Episode(id="trigger-test", user_id="u1", intent_text="test")
        # save 不会触发初始化
        await store.save(ep)
        assert store._initialized is False
        assert store.pg_available is False

        # load 不会触发初始化
        await store.load("trigger-test")
        assert store._initialized is False

        # delete 不会触发初始化
        await store.delete("trigger-test")
        assert store._initialized is False

        # 只有 search 才触发初始化
        await store.search("test")
        assert store._initialized is True

    @pytest.mark.asyncio
    async def test_degradation_no_exception(self, tmp_data_dir):
        """用户场景：没有 pgvector 时完全不抛异常。

        验证：所有操作在无 pgvector 环境中正常完成。
        """
        from memory.pgvector_store import PgVectorStore
        from memory.types import Episode, Knowledge

        store = PgVectorStore(None)  # 使用默认配置（pgvector 关闭）

        # 所有操作都不应抛异常
        ep = Episode(id="safe-ep", user_id="u1", intent_text="安全测试")
        await store.save(ep)

        loaded = await store.load("safe-ep")
        assert loaded is not None

        results = await store.search("安全")
        assert isinstance(results, list)

        deleted = await store.delete("safe-ep")
        assert deleted is True

        assert store.pg_available is False


class TestPgVectorStoreSearchDegradation:
    """验证 pgvector 不可用时 search 正确降级到关键词搜索。"""

    @pytest.fixture
    def tmp_data_dir(self, tmp_path):
        data_dir = tmp_path / "memory"
        data_dir.mkdir()
        return str(data_dir)

    @pytest.mark.asyncio
    async def test_search_degrades_to_keyword_when_pg_fails(self, tmp_data_dir):
        """用户场景：pgvector 连接失败，搜索仍然返回关键词结果。"""
        from memory.pgvector_store import PgVectorConfig, PgVectorStore
        from memory.types import Episode

        embedding_service = AsyncMock()
        embedding_service.embed_text = AsyncMock(return_value=[0.1] * 8)
        config = PgVectorConfig(
            enabled=True,
            connection_string="postgresql+asyncpg://test:test@localhost/test",
            data_dir=tmp_data_dir,
            embedding_service=embedding_service,
        )
        store = PgVectorStore(config)

        # 存入数据
        ep = Episode(
            id="degrade-ep",
            user_id="u1",
            intent_text="测试降级搜索功能",
            execution_summary="降级到关键词搜索后仍能找到结果",
            tags=["降级", "搜索"],
        )
        await store.save(ep)

        # 模拟连接失败
        async def _fail():
            raise Exception("Connection refused")

        store._create_pg_engine = _fail

        # 搜索应该降级但不抛异常
        results = await store.search("降级")
        assert isinstance(results, list)
        # 关键词搜索应该能找到结果
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_with_pgvector_sync_then_query(self, tmp_data_dir):
        """用户场景：pgvector 可用时，先同步再搜索。"""
        from memory.pgvector_store import PgVectorConfig, PgVectorStore
        from memory.types import Episode

        embedding_service = AsyncMock()
        embedding_service.embed_text = AsyncMock(return_value=[0.1] * 8)
        config = PgVectorConfig(
            enabled=True,
            connection_string="postgresql+asyncpg://test:test@localhost/test",
            data_dir=tmp_data_dir,
            embedding_service=embedding_service,
        )
        store = PgVectorStore(config)

        ep = Episode(
            id="sync-ep",
            user_id="u1",
            intent_text="同步测试",
            execution_summary="测试pgvector同步",
        )
        # 先保存到 fallback
        await store.save(ep)

        # 模拟 pgvector 可用
        store._pg_available = True
        store._initialized = True

        # 模拟向量检索成功
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_row = MagicMock()
        mock_row.id = "sync-ep"
        mock_row.intent_text = "同步测试"
        mock_row.execution_summary = "测试pgvector同步"
        mock_row.similarity = 0.92
        mock_result.fetchall.return_value = [mock_row]
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.flush = AsyncMock()
        store._session = mock_session

        results = await store.search("同步")
        assert len(results) >= 1
        assert results[0].score == 0.92


# ============================================================
# 模块2：TagWave 检索算法 — 真实用户场景
# ============================================================


@dataclass
class _MemItem:
    """测试用记忆条目。"""
    id: str
    content: str
    tags: list[str]
    vector: list[float] = field(default_factory=list)
    timestamp: float = 0.0
    importance: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)


def _norm(vec: list[float]) -> list[float]:
    mag = math.sqrt(sum(v * v for v in vec))
    if mag < 1e-9:
        return vec
    return [v / mag for v in vec]


class TestTagWaveRealUserScenario:
    """模拟用户真实使用 TagWave 检索的场景。"""

    @pytest.fixture
    def retriever_with_data(self):
        from memory.tagwave_retriever import TagWaveRetriever
        r = TagWaveRetriever()

        cooccurrence = [
            ("Python", "异步", 15), ("Python", "asyncio", 12),
            ("Python", "编程", 20), ("Python", "并发", 10),
            ("异步", "asyncio", 18), ("异步", "并发", 14),
            ("数据库", "MySQL", 12), ("数据库", "PostgreSQL", 10),
            ("数据库", "Redis", 8), ("MySQL", "优化", 13),
            ("Redis", "缓存", 15), ("机器学习", "神经网络", 12),
            ("深度学习", "Transformer", 14), ("Python", "机器学习", 4),
        ]
        frequency = {
            "Python": 50, "异步": 25, "编程": 40, "asyncio": 20,
            "数据库": 45, "MySQL": 20, "Redis": 15,
            "机器学习": 35, "神经网络": 18,
        }
        r.build_index(cooccurrence, frequency)
        return r

    @pytest.fixture
    def memories(self):
        now = time.time()
        return [
            _MemItem(id="m1", content="Python异步编程最佳实践",
                     tags=["Python", "异步", "编程"],
                     vector=_norm([0.9, 0.1, 0.0]),
                     timestamp=now - 100, importance=0.8),
            _MemItem(id="m2", content="asyncio事件循环详解",
                     tags=["Python", "asyncio", "异步"],
                     vector=_norm([0.85, 0.15, 0.0]),
                     timestamp=now - 200, importance=0.7),
            _MemItem(id="m3", content="MySQL查询优化技巧",
                     tags=["MySQL", "优化", "数据库"],
                     vector=_norm([0.0, 0.1, 0.9]),
                     timestamp=now - 150, importance=0.8),
            _MemItem(id="m4", content="Redis缓存架构设计",
                     tags=["Redis", "缓存", "数据库"],
                     vector=_norm([0.0, 0.2, 0.8]),
                     timestamp=now - 30, importance=0.9),
            _MemItem(id="m5", content="红烧肉的做法",
                     tags=["烹饪", "肉类"],
                     vector=_norm([0.0, 0.0, 0.1]),
                     timestamp=now - 1000, importance=0.3),
        ]

    def test_lens_phase_filters_by_keywords(self, retriever_with_data, memories):
        """用户场景：通过关键词/标签快速过滤不相关记忆。

        查询 Python 相关内容 → 烹饪类记忆不应出现。
        """
        results = retriever_with_data.lens_phase(["Python", "异步"], memories)
        result_ids = {r.id for r in results}

        # Python 相关记忆应保留
        assert "m1" in result_ids
        assert "m2" in result_ids
        # 烹饪不应出现
        assert "m5" not in result_ids

    def test_expand_phase_discovers_related(self, retriever_with_data, memories):
        """用户场景：通过标签关联发现间接相关的记忆。

        查询 asyncio → 拓展发现 Python 和 异步 关联的记忆。
        """
        lens_results = retriever_with_data.lens_phase(["asyncio"], memories)
        expanded = retriever_with_data.expand_phase(
            ["asyncio"], lens_results, memories,
        )
        expanded_ids = {r.id for r in expanded}

        # asyncio 直接匹配 m2
        assert "m2" in expanded_ids
        # Python+异步 间接关联 m1
        assert "m1" in expanded_ids

    def test_focus_phase_reranks_by_comprehensive_score(
        self, retriever_with_data, memories,
    ):
        """用户场景：综合相关度、时效性、重要性精排。

        查询 Python 异步 → 最新且重要的 Python 记忆应排在前面。
        """
        query_tags = ["Python", "异步"]
        query_vector = _norm([0.9, 0.1, 0.0])

        lens_results = retriever_with_data.lens_phase(query_tags, memories)
        expanded = retriever_with_data.expand_phase(
            query_tags, lens_results, memories,
        )
        focused = retriever_with_data.focus_phase(
            expanded, query_vector, top_k=3,
        )

        # 应返回不超过 3 个结果
        assert len(focused) <= 3
        # 所有结果都有 final_score
        for item in focused:
            assert hasattr(item, "final_score")
            assert item.final_score > 0.0
        # 结果按分数降序
        scores = [item.final_score for item in focused]
        assert scores == sorted(scores, reverse=True)

    def test_retrieval_accuracy_above_80_percent(self, retriever_with_data):
        """用户场景：检索准确率必须 ≥ 80%。

        多次查询不同领域，验证 Precision@5 平均 ≥ 0.80。
        """
        now = time.time()

        # 构建更大的测试数据集
        items = []
        # Python 域（10条）
        for i in range(10):
            items.append(_MemItem(
                id=f"py-{i}",
                content=f"Python编程技巧{i}",
                tags=["Python", "编程", f"技巧{i % 3}"],
                vector=_norm([0.9, 0.1, 0.0]),
                timestamp=now - i * 100,
                importance=0.7 + 0.03 * i,
            ))
        # 数据库域（10条）
        for i in range(10):
            items.append(_MemItem(
                id=f"db-{i}",
                content=f"数据库优化方案{i}",
                tags=["数据库", "优化", f"db{i % 3}"],
                vector=_norm([0.0, 0.1, 0.9]),
                timestamp=now - i * 150,
                importance=0.6 + 0.04 * i,
            ))
        # 不相关噪音（5条）
        for i in range(5):
            items.append(_MemItem(
                id=f"noise-{i}",
                content=f"噪音内容{i}",
                tags=["烹饪", "旅行"],
                vector=_norm([0.0, 0.0, 0.1]),
                timestamp=now - i * 500,
                importance=0.2,
            ))

        # 查询及期望结果
        queries = [
            {
                "tags": ["Python", "编程"],
                "vector": _norm([0.9, 0.1, 0.0]),
                "relevant": {f"py-{i}" for i in range(10)},
            },
            {
                "tags": ["数据库", "优化"],
                "vector": _norm([0.0, 0.1, 0.9]),
                "relevant": {f"db-{i}" for i in range(10)},
            },
        ]

        total_precision = 0.0
        for q in queries:
            result = retriever_with_data.retrieve(
                query_tags=q["tags"],
                query_vector=q["vector"],
                candidates=items,
                top_k=5,
            )
            retrieved_ids = {r.id for r in result.results}
            hits = len(retrieved_ids & q["relevant"])
            precision = hits / max(len(retrieved_ids), 1)
            total_precision += precision

        avg_precision = total_precision / len(queries)
        assert avg_precision >= 0.80, (
            f"检索准确率 {avg_precision:.2%} < 80%"
        )

    def test_empty_input_returns_graceful_result(self, retriever_with_data):
        """边界场景：空输入不崩溃。"""
        result = retriever_with_data.retrieve(
            query_tags=[],
            query_vector=None,
            candidates=[],
            top_k=5,
        )
        assert isinstance(result.results, list)
        assert result.elapsed_ms >= 0


# ============================================================
# 模块3：记忆维护机制 — 真实用户场景
# ============================================================


def _make_episode(
    id: str = "test-ep",
    intent_text: str = "test",
    created_at: datetime | None = None,
    extra_data: dict[str, Any] | None = None,
):
    from memory.types import Episode
    ep = Episode(
        id=id,
        intent_text=intent_text,
        created_at=created_at or datetime.now(UTC),
    )
    if extra_data is not None:
        ep.extra_data = extra_data
    return ep


def _make_knowledge(
    id: str = "test-kn",
    content: str = "test content",
    created_at: datetime | None = None,
    extra_data: dict[str, Any] | None = None,
):
    from memory.types import Knowledge
    kn = Knowledge(
        id=id,
        content=content,
        created_at=created_at or datetime.now(UTC),
    )
    if extra_data is not None:
        kn.extra_data = extra_data
    return kn


def _make_mock_services():
    """创建 Mock 记忆服务。"""
    ep_service = MagicMock()
    ep_service._storage = None
    ep_service._in_memory = {}

    kn_service = MagicMock()
    kn_service._storage = None
    kn_service._in_memory = {}

    memory_service = MagicMock()
    memory_service._episode_service = ep_service
    memory_service._knowledge_service = kn_service
    memory_service._embedding_service = None
    memory_service._vector_retriever = None
    memory_service._tag_service = None

    return memory_service, ep_service, kn_service


class TestMaintenanceRealUserScenario:
    """模拟用户真实的维护场景。"""

    def test_ttl_cleanup_removes_expired_memories(self):
        """用户场景：按 TTL 自动清理过期记忆。

        场景：3条记忆，1条TTL=1小时已过期，1条TTL=1小时未过期，1条永不过期。
        期望：只有过期的被清理。
        """
        from memory.maintenance import MaintenanceConfig, MemoryMaintenanceService

        memory_service, ep_service, kn_service = _make_mock_services()
        config = MaintenanceConfig(ttl_enabled=True, default_ttl_seconds=86400)
        service = MemoryMaintenanceService(
            memory_service=memory_service, config=config,
        )

        now = datetime.now(UTC)

        # 过期记忆（TTL=1小时，创建于2小时前）
        expired_ep = _make_episode(
            id="expired",
            created_at=now - timedelta(hours=2),
            extra_data={"ttl_seconds": 3600},
        )
        # 未过期记忆（TTL=1小时，创建于10分钟前）
        recent_ep = _make_episode(
            id="recent",
            created_at=now - timedelta(minutes=10),
            extra_data={"ttl_seconds": 3600},
        )
        # 永不过期记忆（TTL=0）
        permanent_ep = _make_episode(
            id="permanent",
            created_at=now - timedelta(days=365),
            extra_data={"ttl_seconds": 0},
        )

        ep_service._in_memory = {
            "expired": expired_ep,
            "recent": recent_ep,
            "permanent": permanent_ep,
        }

        result = service.cleanup_ttl_expired(now=now)

        assert "expired" not in ep_service._in_memory
        assert "recent" in ep_service._in_memory
        assert "permanent" in ep_service._in_memory
        assert result["cleaned_count"] >= 1
        assert result["status"] == "success"

    def test_capacity_eviction_by_lru_and_importance(self):
        """用户场景：超限时按 LRU + 重要性淘汰。

        场景：容量限制为3，当前有5条记忆。
        期望：淘汰2条评分最低的记忆（久未访问+低重要性）。
        """
        from memory.maintenance import MaintenanceConfig, MemoryMaintenanceService

        memory_service, ep_service, kn_service = _make_mock_services()
        config = MaintenanceConfig(
            capacity_limit=3,
            lru_weight=0.5,
            importance_weight=0.5,
        )
        service = MemoryMaintenanceService(
            memory_service=memory_service, config=config,
        )

        now = datetime.now(UTC)

        # 5条记忆，从低价值到高价值
        for i in range(5):
            ep = _make_episode(
                id=f"evict-{i}",
                intent_text=f"记忆{i}",
                created_at=now - timedelta(hours=i),
                extra_data={
                    "importance": 0.1 * i,  # 0.0, 0.1, 0.2, 0.3, 0.4
                    "last_accessed_at": (
                        now - timedelta(hours=(4 - i) * 10)
                    ).isoformat(),
                },
            )
            ep_service._in_memory[f"evict-{i}"] = ep

        result = service.evict_by_capacity(now=now)

        assert result["evicted_count"] == 2
        assert result["status"] == "success"
        # 剩余3条
        assert len(ep_service._in_memory) == 3
        # 低价值的（evict-0 最可能被淘汰）
        # 高价值的（evict-3, evict-4 应保留）
        assert "evict-4" in ep_service._in_memory

    def test_importance_decay_over_time(self):
        """用户场景：重要性随时间降低。

        场景：一条记忆 importance=1.0，创建于2个半衰期之前。
        期望：衰减后 importance ≈ 0.25。
        """
        from memory.maintenance import MaintenanceConfig, MemoryMaintenanceService

        memory_service, ep_service, kn_service = _make_mock_services()
        config = MaintenanceConfig(
            decay_enabled=True,
            decay_type="exponential",
            decay_half_life_seconds=86400,  # 1天
        )
        service = MemoryMaintenanceService(
            memory_service=memory_service, config=config,
        )

        now = datetime.now(UTC)
        # 创建于2天前
        old_ep = _make_episode(
            id="decay-test",
            created_at=now - timedelta(days=2),
            extra_data={"importance": 1.0},
        )
        ep_service._in_memory = {"decay-test": old_ep}

        result = service.decay_importance(now=now)

        assert result["status"] == "success"
        assert result["decayed_count"] >= 1

        # 验证衰减结果
        decayed_importance = old_ep.extra_data["importance"]
        # 2个半衰期: 1.0 * 0.5^2 = 0.25
        assert abs(decayed_importance - 0.25) < 0.05, (
            f"衰减后重要性 {decayed_importance} 不接近 0.25"
        )

    def test_decay_disabled_skips(self):
        """用户场景：禁用衰减时不影响记忆。"""
        from memory.maintenance import MaintenanceConfig, MemoryMaintenanceService

        memory_service, ep_service, kn_service = _make_mock_services()
        config = MaintenanceConfig(decay_enabled=False)
        service = MemoryMaintenanceService(
            memory_service=memory_service, config=config,
        )

        now = datetime.now(UTC)
        ep = _make_episode(
            id="no-decay",
            created_at=now - timedelta(days=100),
            extra_data={"importance": 1.0},
        )
        ep_service._in_memory = {"no-decay": ep}

        result = service.decay_importance(now=now)

        assert result["status"] == "skipped"
        # 重要性不应改变
        assert ep.extra_data["importance"] == 1.0

    def test_linear_decay_reduces_proportionally(self):
        """用户场景：线性衰减按比例降低重要性。"""
        from memory.maintenance import MaintenanceConfig, MemoryMaintenanceService

        memory_service, ep_service, kn_service = _make_mock_services()
        config = MaintenanceConfig(
            decay_enabled=True,
            decay_type="linear",
            decay_rate=0.001,  # 每秒降低 0.001
        )
        service = MemoryMaintenanceService(
            memory_service=memory_service, config=config,
        )

        now = datetime.now(UTC)
        ep = _make_episode(
            id="linear-decay",
            created_at=now - timedelta(seconds=500),
            extra_data={"importance": 1.0},
        )
        ep_service._in_memory = {"linear-decay": ep}

        service.decay_importance(now=now)

        expected = max(0.0, 1.0 - 0.001 * 500)  # = 0.5
        actual = ep.extra_data["importance"]
        assert abs(actual - expected) < 0.01, (
            f"线性衰减结果 {actual} 不接近 {expected}"
        )


# ============================================================
# 模块4：记忆监控指标 — 真实用户场景
# ============================================================


class TestMemoryMetricsRealUserScenario:
    """模拟用户真实使用监控的场景。"""

    def test_track_retrieval_latency_p50_p95_p99(self):
        """用户场景：查看检索延迟分位数。

        场景：执行100次检索，延迟从1ms到100ms均匀分布。
        期望：P50≈50ms, P95≈95ms, P99≈99ms。
        """
        from memory.memory_metrics import MemoryMetrics

        metrics = MemoryMetrics()

        # 模拟100次检索，延迟从1ms到100ms
        for i in range(1, 101):
            metrics.record_retrieval(
                latency_seconds=i / 1000.0,
                hit=(i <= 80),  # 80% 命中率
            )

        snapshot = metrics.get_metrics()

        # 验证延迟分位数
        lat = snapshot["retrieval_latency"]
        assert lat["count"] == 100
        assert lat["p50"] is not None
        assert 0.045 <= lat["p50"] <= 0.055, f"P50={lat['p50']}"
        assert 0.090 <= lat["p95"] <= 0.096, f"P95={lat['p95']}"
        assert 0.098 <= lat["p99"] <= 0.100, f"P99={lat['p99']}"
        assert lat["min"] == pytest.approx(0.001, abs=0.001)
        assert lat["max"] == pytest.approx(0.100, abs=0.001)

        # 验证命中率
        hr = snapshot["hit_rate"]
        assert hr["rate"] == 80.0
        assert hr["hits"] == 80
        assert hr["total"] == 100

    def test_track_hit_rate_percentage(self):
        """用户场景：查看命中率统计。

        场景：10次检索，7次命中，3次未命中。
        期望：命中率=70%。
        """
        from memory.memory_metrics import MemoryMetrics

        metrics = MemoryMetrics()

        hits = [True, True, False, True, True, False, True, False, True, True]
        for hit in hits:
            metrics.record_retrieval(latency_seconds=0.01, hit=hit)

        hr = metrics.get_metrics()["hit_rate"]
        assert hr["rate"] == 70.0
        assert hr["hits"] == 7
        assert hr["total"] == 10

    def test_track_storage_capacity(self):
        """用户场景：追踪存储容量变化。

        场景：添加10条×500字节，删除3条×200字节，再添加5条×300字节。
        期望：最终12条，4900字节。
        """
        from memory.memory_metrics import MemoryMetrics

        metrics = MemoryMetrics()

        # 添加
        metrics.record_storage_change(delta_entries=10, delta_bytes=5000)
        # 删除
        metrics.record_storage_change(delta_entries=-3, delta_bytes=-200)
        # 再添加
        metrics.record_storage_change(delta_entries=5, delta_bytes=300)

        storage = metrics.get_metrics()["storage"]
        assert storage["entry_count"] == 12  # 10-3+5
        assert storage["total_bytes"] == 5100  # 5000-200+300

    def test_reset_and_continue(self):
        """用户场景：重置指标后继续使用。"""
        from memory.memory_metrics import MemoryMetrics

        metrics = MemoryMetrics()
        metrics.record_retrieval(latency_seconds=0.5, hit=True)
        metrics.record_storage_change(delta_entries=10, delta_bytes=1000)

        # 重置
        metrics.reset()

        snapshot = metrics.get_metrics()
        assert snapshot["retrieval_latency"]["count"] == 0
        assert snapshot["hit_rate"]["rate"] == 0.0
        assert snapshot["storage"]["entry_count"] == 0

        # 继续使用
        metrics.record_retrieval(latency_seconds=0.1, hit=True)
        new_snapshot = metrics.get_metrics()
        assert new_snapshot["retrieval_latency"]["count"] == 1
        assert new_snapshot["hit_rate"]["rate"] == 100.0

    def test_concurrent_usage_thread_safe(self):
        """用户场景：多线程并发记录指标。"""
        from memory.memory_metrics import MemoryMetrics

        metrics = MemoryMetrics()
        num_threads = 10
        records_per_thread = 100
        barrier = threading.Barrier(num_threads)

        def worker():
            barrier.wait()
            for _ in range(records_per_thread):
                metrics.record_retrieval(latency_seconds=0.001, hit=True)
                metrics.record_storage_change(delta_entries=1, delta_bytes=10)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snapshot = metrics.get_metrics()
        assert snapshot["retrieval_latency"]["count"] == num_threads * records_per_thread
        assert snapshot["hit_rate"]["hits"] == num_threads * records_per_thread
        assert snapshot["storage"]["entry_count"] == num_threads * records_per_thread


# ============================================================
# 模块5：递归压缩策略回归验证
# ============================================================


class TestCompressorRegression:
    """验证递归压缩策略代码未被改动。"""

    def test_compressor_files_exist_and_importable(self):
        """所有压缩模块文件应存在且可导入。"""
        import importlib

        modules = [
            "memory.compressor",
            "memory.compressor.config",
            "memory.compressor.core",
            "memory.compressor.models",
            "memory.compressor.reader",
            "memory.compressor.writer",
            "memory.compressor.store",
            "memory.compressor.store_manager",
            "memory.compressor.structured",
        ]
        for mod_name in modules:
            mod = importlib.import_module(mod_name)
            assert mod is not None, f"{mod_name} 应可导入"

    def test_compressor_public_api_unchanged(self):
        """压缩模块的公共 API 应保持不变。"""
        from memory.compressor.config import CompressionConfig
        from memory.compressor.core import ContextCompressor
        from memory.compressor.models import CompressionResult

        # 验证核心类存在
        assert CompressionConfig is not None
        assert ContextCompressor is not None
        assert CompressionResult is not None

        # 验证核心方法存在
        assert hasattr(ContextCompressor, "compress")
        assert callable(ContextCompressor.compress)

    def test_compressor_timestamp_consistent(self):
        """所有压缩模块文件时间戳应一致（未被单独修改）。

        验证方式：所有 .py 文件的修改时间应在同一秒内（容差 ≤ 1秒）。
        """
        import importlib

        mod = importlib.import_module("memory.compressor.config")
        config_path = mod.__file__
        config_dir = os.path.dirname(config_path)

        timestamps = []
        for fname in os.listdir(config_dir):
            if fname.endswith(".py"):
                fpath = os.path.join(config_dir, fname)
                stat = os.stat(fpath)
                timestamps.append(stat.st_mtime)

        # 所有文件时间戳应在同一秒内
        assert len(timestamps) > 0, "应至少有一个 .py 文件"
        max_diff = max(timestamps) - min(timestamps)
        assert max_diff <= 1.0, (
            f"压缩模块文件被单独修改，时间差 {max_diff:.3f}s > 1s"
        )

    def test_compressor_basic_functionality(self):
        """压缩模块基本功能应正常工作。

        验证核心类可导入、API 面存在。
        注意：CompressionConfig.__post_init__ 需要外部配置文件，
        因此只验证类存在和方法签名，不直接实例化。
        """
        from memory.compressor.config import CompressionConfig, ContextBudget
        from memory.compressor.core import ContextCompressor

        # 验证核心类存在
        assert CompressionConfig is not None
        assert ContextCompressor is not None
        assert ContextBudget is not None

        # 验证核心方法存在
        assert hasattr(ContextCompressor, "compress")
        assert callable(ContextCompressor.compress)

        # 验证 ContextBudget 可直接实例化
        budget = ContextBudget()
        assert budget.total() == 0
        budget.system_prompt_tokens = 100
        budget.l1_tokens = 50
        assert budget.total() == 150


# ============================================================
# 跨模块集成测试
# ============================================================


class TestCrossModuleIntegration:
    """验证模块之间的集成协作。"""

    def test_metrics_tracks_maintenance_operations(self):
        """用户场景：维护操作后，指标应反映变化。

        场景：执行维护操作后，存储容量应减少。
        """
        from memory.memory_metrics import MemoryMetrics
        from memory.maintenance import MaintenanceConfig, MemoryMaintenanceService

        metrics = MemoryMetrics()
        memory_service, ep_service, kn_service = _make_mock_services()
        config = MaintenanceConfig(
            ttl_enabled=True,
            capacity_limit=10000,
            decay_enabled=True,
        )
        service = MemoryMaintenanceService(
            memory_service=memory_service, config=config,
        )

        now = datetime.now(UTC)

        # 添加一些记忆
        for i in range(5):
            ep = _make_episode(
                id=f"int-ep-{i}",
                created_at=now - timedelta(minutes=i),
                extra_data={"ttl_seconds": 3600},
            )
            ep_service._in_memory[f"int-ep-{i}"] = ep
            metrics.record_storage_change(delta_entries=1, delta_bytes=100)

        # 验证指标
        assert metrics.get_metrics()["storage"]["entry_count"] == 5

        # 执行 TTL 清理（无过期）
        result = service.cleanup_ttl_expired(now=now)
        assert result["status"] == "success"

    def test_tagwave_and_pgvector_independent(self):
        """用户场景：TagWave 和 PgVector 独立工作，互不影响。"""
        from memory.tagwave_retriever import TagWaveRetriever, TagWaveConfig
        from memory.pgvector_store import PgVectorConfig, PgVectorStore

        # TagWave 不依赖 PgVector
        retriever = TagWaveRetriever()
        assert retriever is not None

        # PgVector 不依赖 TagWave
        store = PgVectorStore(PgVectorConfig(enabled=False))
        assert store is not None

        # 两者可同时使用
        items = [
            _MemItem(id="t1", content="测试", tags=["测试"],
                     vector=_norm([1.0, 0.0])),
        ]
        result = retriever.retrieve(
            query_tags=["测试"],
            query_vector=_norm([1.0, 0.0]),
            candidates=items,
            top_k=5,
        )
        assert len(result.results) == 1

    def test_maintenance_and_metrics_coexist(self):
        """用户场景：维护和监控模块可同时使用。"""
        from memory.memory_metrics import MemoryMetrics
        from memory.maintenance import MaintenanceConfig, MemoryMaintenanceService

        metrics = MemoryMetrics()
        memory_service, _, _ = _make_mock_services()
        config = MaintenanceConfig()
        service = MemoryMaintenanceService(
            memory_service=memory_service, config=config,
        )

        # 同时使用不冲突
        metrics.record_retrieval(latency_seconds=0.05, hit=True)
        stats = service.get_stats()
        assert isinstance(stats, dict)

        snapshot = metrics.get_metrics()
        assert snapshot["retrieval_latency"]["count"] == 1
