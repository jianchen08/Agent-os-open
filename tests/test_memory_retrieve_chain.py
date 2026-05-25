"""Memory Retrieve 检索链路测试。

验证 JsonMemoryStore.retrieve() 方法及 MemoryService 三种检索方法
（vector fallback keyword / keyword / tagwave fallback keyword）的完整链路。

根因：JsonMemoryStore 缺少 IRetriever.retrieve() 方法，
导致 MemoryService._retrieve_by_method() 调用时 AttributeError 被吞掉返回 []。
本测试验证修复后各链路能正确返回数据。
"""
import pytest

from memory.service import MemoryService
from memory.storage.json_store import JsonMemoryStore
from memory.types import Episode, Knowledge, MemoryType, SearchResult


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def json_store(tmp_path):
    """创建使用临时目录的 JsonMemoryStore 实例。"""
    return JsonMemoryStore(data_dir=str(tmp_path / "memory"))


@pytest.fixture
def memory_service(json_store):
    """创建注入了 JsonMemoryStore 的 MemoryService 实例。

    - vector_search 禁用（模拟 MVP 默认配置）
    - fallback_to_keyword 开启
    - keyword 检索器指向 json_store
    """
    return MemoryService(
        episode_storage=json_store,
        semantic_storage=json_store,
        retrievers={"keyword": json_store},
        config={"vector_search": {"enabled": False, "fallback_to_keyword": True}},
    )


async def _store_knowledge(json_store: JsonMemoryStore, content: str, tags: list[str] | None = None) -> str:
    """辅助：存储一条 Knowledge 并返回 ID。"""
    knowledge = Knowledge(
        user_id="test_user",
        content=content,
        source_type="manual",
        extra_data={"tags": tags or []},
    )
    return await json_store.save(knowledge, "semantic")


async def _store_episode(json_store: JsonMemoryStore, intent: str, summary: str, tags: list[str] | None = None) -> str:
    """辅助：存储一条 Episode 并返回 ID。"""
    episode = Episode(
        user_id="test_user",
        intent_text=intent,
        execution_summary=summary,
        tags=tags or [],
    )
    return await json_store.save(episode, "episode")


# ============================================================
# 1. JsonMemoryStore.retrieve() 单元测试
# ============================================================


class TestJsonMemoryStoreRetrieve:
    """验证 JsonMemoryStore 实现了 IRetriever.retrieve() 方法。"""

    @pytest.mark.asyncio
    async def test_retrieve_returns_stored_knowledge(self, json_store):
        """store 后 retrieve 能按关键词匹配返回数据。"""
        await _store_knowledge(json_store, "Python 编码规范要求使用 type hints", ["python"])

        results = await json_store.retrieve(
            query="Python 编码规范",
            user_id="test_user",
            top_k=5,
            memory_type="semantic",
        )

        assert len(results) >= 1, "retrieve 应至少返回 1 条匹配结果"
        assert "Python" in results[0].content
        assert results[0].memory_type == MemoryType.SEMANTIC

    @pytest.mark.asyncio
    async def test_retrieve_respects_top_k(self, json_store):
        """retrieve 应尊重 top_k 参数限制返回数量。"""
        for i in range(5):
            await _store_knowledge(json_store, f"测试知识条目编号 {i}", [])

        results = await json_store.retrieve(
            query="测试知识",
            user_id="test_user",
            top_k=2,
            memory_type="semantic",
        )

        assert len(results) <= 2

    @pytest.mark.asyncio
    async def test_retrieve_empty_query_returns_empty(self, json_store):
        """空查询应返回空结果。"""
        await _store_knowledge(json_store, "一些知识内容", [])

        results = await json_store.retrieve(
            query="",
            user_id="test_user",
            top_k=5,
            memory_type="semantic",
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_retrieve_no_match_returns_empty(self, json_store):
        """不匹配的查询应返回空结果。"""
        await _store_knowledge(json_store, "Python 编码规范", [])

        results = await json_store.retrieve(
            query="完全不相关的查询 XXXYYY",
            user_id="test_user",
            top_k=5,
            memory_type="semantic",
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_retrieve_memory_type_episode(self, json_store):
        """retrieve 按 memory_type=episode 过滤只返回情景记忆。"""
        await _store_episode(json_store, "重构代码", "完成了代码重构", ["refactor"])
        await _store_knowledge(json_store, "重构代码的最佳实践", [])

        results = await json_store.retrieve(
            query="重构代码",
            user_id="test_user",
            top_k=5,
            memory_type="episode",
        )

        assert len(results) >= 1
        for r in results:
            assert r.memory_type == MemoryType.EPISODE

    @pytest.mark.asyncio
    async def test_retrieve_inherits_IRetriever(self, json_store):
        """JsonMemoryStore 应是 IRetriever 的子类。"""
        from memory.ports import IRetriever

        assert isinstance(json_store, IRetriever), (
            "JsonMemoryStore 必须实现 IRetriever 接口"
        )

    @pytest.mark.asyncio
    async def test_retrieve_has_retrieve_method(self, json_store):
        """JsonMemoryStore 实例必须有 retrieve 方法。"""
        assert hasattr(json_store, "retrieve"), (
            "JsonMemoryStore 必须有 retrieve 方法"
        )
        assert callable(json_store.retrieve)


# ============================================================
# 2. MemoryService 检索链路集成测试
# ============================================================


class TestMemoryServiceRetrieveChain:
    """验证 MemoryService 三种 retrieval_method 的完整链路。"""

    @pytest.mark.asyncio
    async def test_keyword_method_returns_results(self, memory_service, json_store):
        """keyword 检索方法能通过 MemoryService.retrieve 返回结果。"""
        await _store_knowledge(json_store, "Python 类型注解规范", ["python"])

        results = await memory_service.retrieve(
            user_id="test_user",
            filter={"memory_type": "semantic"},
            inject_type="retrieval",
            retrieval_method="keyword",
            query="Python 类型注解",
            top_k=5,
        )

        assert len(results) >= 1, "keyword 方法应返回匹配结果"
        assert "Python" in results[0].content

    @pytest.mark.asyncio
    async def test_vector_fallback_keyword_returns_results(self, memory_service, json_store):
        """vector 检索在禁用时应 fallback 到 keyword 并返回结果。"""
        await _store_knowledge(json_store, "FastAPI 路由设计模式", ["fastapi"])

        results = await memory_service.retrieve(
            user_id="test_user",
            filter={"memory_type": "semantic"},
            inject_type="retrieval",
            retrieval_method="vector",
            query="FastAPI 路由",
            top_k=5,
        )

        assert len(results) >= 1, "vector fallback 到 keyword 应返回匹配结果"

    @pytest.mark.asyncio
    async def test_tagwave_fallback_keyword_returns_results(self, memory_service, json_store):
        """tagwave 检索器未注册时应 fallback 到 keyword 并返回结果。"""
        await _store_knowledge(json_store, "Docker 容器编排策略", ["docker"])

        results = await memory_service.retrieve(
            user_id="test_user",
            filter={"memory_type": "semantic"},
            inject_type="retrieval",
            retrieval_method="tagwave",
            query="Docker 容器",
            top_k=5,
        )

        assert len(results) >= 1, "tagwave fallback 到 keyword 应返回匹配结果"

    @pytest.mark.asyncio
    async def test_store_then_retrieve_full_chain(self, memory_service, json_store):
        """完整链路：通过 MemoryService store → 通过 retrieve keyword 找回。"""
        knowledge = Knowledge(
            user_id="test_user",
            content="缓存失效策略包括 LRU、LFU 和 FIFO",
            source_type="manual",
            extra_data={"tags": ["cache", "strategy"]},
        )
        await memory_service.store_knowledge(knowledge)

        results = await memory_service.retrieve(
            user_id="test_user",
            filter={"memory_type": "semantic"},
            inject_type="retrieval",
            retrieval_method="keyword",
            query="缓存失效策略",
            top_k=5,
        )

        assert len(results) >= 1, "store 后 retrieve 应能找回数据"
        assert any("缓存" in r.content for r in results)
