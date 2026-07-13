"""MemoryService 记忆服务门面测试。

测试 MemoryService 的情景记忆、语义记忆、检索、搜索、
压缩块操作以及无存储降级行为。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from memory.service import MemoryService
from memory.types import (
    ChunkData,
    Episode,
    Knowledge,
    MemoryType,
    SearchResult,
)


# ============================================================
# 辅助：构造带 mock 后端的 MemoryService
# ============================================================


def _make_episode_storage() -> AsyncMock:
    """创建 mock IEpisodeStorage。"""
    storage = AsyncMock()
    storage.save = AsyncMock(return_value="ep-1")
    storage.get = AsyncMock(return_value=None)
    storage.find_by_user = AsyncMock(return_value=[])
    storage.update = AsyncMock(return_value=True)
    storage.delete = AsyncMock(return_value=True)
    storage.count_by_user = AsyncMock(return_value=0)
    return storage


def _make_semantic_storage() -> AsyncMock:
    """创建 mock ISemanticStorage。"""
    storage = AsyncMock()
    storage.save = AsyncMock(return_value="kn-1")
    storage.get = AsyncMock(return_value=None)
    storage.find_by_user = AsyncMock(return_value=[])
    storage.update_embedding = AsyncMock(return_value=True)
    storage.delete = AsyncMock(return_value=True)
    return storage


def _make_retriever() -> AsyncMock:
    """创建 mock IRetriever。"""
    retriever = AsyncMock()
    retriever.retrieve = AsyncMock(return_value=[])
    return retriever


def _make_service(**overrides: Any) -> MemoryService:
    """构造 MemoryService 实例，默认注入 mock 后端。"""
    ep = overrides.pop("episode_storage", _make_episode_storage())
    se = overrides.pop("semantic_storage", _make_semantic_storage())
    retrievers = overrides.pop("retrievers", None)
    return MemoryService(
        episode_storage=ep,
        semantic_storage=se,
        retrievers=retrievers,
        **overrides,
    )


# ============================================================
# 1. 构造函数测试
# ============================================================


class TestMemoryServiceInit:
    """测试 MemoryService 初始化。"""

    def test_无存储后端时降级到内存(self) -> None:
        """不传任何后端时应成功创建，内部使用内存存储。"""
        svc = MemoryService()
        assert svc._episode_service is not None
        assert svc._knowledge_service is not None
        assert svc._retrievers == {}

    def test_注入存储后端(self) -> None:
        """传入 mock 后端时应正确注入。"""
        ep = _make_episode_storage()
        se = _make_semantic_storage()
        svc = MemoryService(episode_storage=ep, semantic_storage=se)
        assert svc._episode_service._storage is ep
        assert svc._knowledge_service._storage is se

    def test_注入检索器字典(self) -> None:
        """传入检索器字典应正确保存。"""
        r = _make_retriever()
        svc = MemoryService(retrievers={"vector": r})
        assert "vector" in svc._retrievers

    def test_注入可选服务(self) -> None:
        """embedding_service / vector_retriever / chunk_service / tag_service 可选注入。"""
        embedding = AsyncMock()
        vec_ret = AsyncMock()
        chunk = AsyncMock()
        tag = AsyncMock()
        svc = MemoryService(
            embedding_service=embedding,
            vector_retriever=vec_ret,
            chunk_service=chunk,
            tag_service=tag,
        )
        assert svc._embedding_service is embedding
        assert svc._vector_retriever is vec_ret
        assert svc._chunk_service is chunk
        assert svc._tag_service is tag


# ============================================================
# 2. 情景记忆操作
# ============================================================


class TestEpisodeOperations:
    """测试情景记忆 CRUD 操作。"""

    @pytest.mark.asyncio
    async def test_store_episode_无向量检索器(self) -> None:
        """存储情景记忆，无 vector_retriever 时直接返回 ID。"""
        svc = _make_service()
        ep = Episode(user_id="u1", intent_text="测试")
        eid = await svc.store_episode(ep)
        assert eid == "ep-1"

    @pytest.mark.asyncio
    async def test_store_episode_有向量检索器且无向量(self) -> None:
        """有 vector_retriever 但 episode 无向量时不调用 save_index。"""
        vr = AsyncMock()
        svc = _make_service(vector_retriever=vr)
        ep = Episode(user_id="u1", intent_text="测试", intent_vector=None)
        eid = await svc.store_episode(ep)
        vr.save_index.assert_not_called()
        assert eid == "ep-1"

    @pytest.mark.asyncio
    async def test_store_episode_有向量检索器且有向量(self) -> None:
        """有 vector_retriever 且 episode 有向量时调用 save_index。"""
        vr = AsyncMock()
        vr.save_index = AsyncMock()
        svc = _make_service(vector_retriever=vr)
        ep = Episode(user_id="u1", intent_text="测试", intent_vector=[0.1, 0.2])
        eid = await svc.store_episode(ep)
        vr.save_index.assert_called_once()
        assert eid == "ep-1"

    @pytest.mark.asyncio
    async def test_store_episode_向量索引写入失败不抛异常(self) -> None:
        """向量索引写入失败时记录日志但不抛异常。"""
        vr = AsyncMock()
        vr.save_index = AsyncMock(side_effect=Exception("PG 连接失败"))
        svc = _make_service(vector_retriever=vr)
        ep = Episode(user_id="u1", intent_text="测试", intent_vector=[0.1])
        eid = await svc.store_episode(ep)
        assert eid == "ep-1"

    @pytest.mark.asyncio
    async def test_create_episode(self) -> None:
        """创建情景记忆应返回字典。"""
        svc = _make_service()
        result = await svc.create_episode(
            user_id="u1", intent_text="意图", tags=["t1"],
        )
        assert isinstance(result, dict)
        assert result["user_id"] == "u1"
        assert result["intent_text"] == "意图"
        assert result["tags"] == ["t1"]

    @pytest.mark.asyncio
    async def test_get_episode_存在时返回字典(self) -> None:
        """获取存在的情景记忆应返回字典。"""
        ep = Episode(id="ep-1", user_id="u1", intent_text="测试")
        storage = _make_episode_storage()
        storage.get = AsyncMock(return_value=ep)
        svc = _make_service(episode_storage=storage)
        result = await svc.get_episode("ep-1", "u1")
        assert result is not None
        assert result["id"] == "ep-1"

    @pytest.mark.asyncio
    async def test_get_episode_不存在时返回None(self) -> None:
        """获取不存在的情景记忆应返回 None。"""
        svc = _make_service()
        result = await svc.get_episode("nonexistent", "u1")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_episodes(self) -> None:
        """列出情景记忆应返回分页字典。"""
        svc = _make_service()
        result = await svc.list_episodes("u1", page=1, page_size=10)
        assert "items" in result
        assert "total" in result
        assert "page" in result

    @pytest.mark.asyncio
    async def test_delete_episode_无向量检索器(self) -> None:
        """删除情景记忆，无 vector_retriever 时直接删除。"""
        storage = _make_episode_storage()
        storage.get = AsyncMock(return_value=Episode(id="ep-1", user_id="u1"))
        svc = _make_service(episode_storage=storage)
        success = await svc.delete_episode("ep-1", "u1")
        assert success is True

    @pytest.mark.asyncio
    async def test_delete_episode_有向量检索器(self) -> None:
        """删除情景记忆，有 vector_retriever 时同步删除向量索引。"""
        storage = _make_episode_storage()
        storage.get = AsyncMock(return_value=Episode(id="ep-1", user_id="u1"))
        vr = AsyncMock()
        vr.delete_index = AsyncMock()
        svc = _make_service(episode_storage=storage, vector_retriever=vr)
        success = await svc.delete_episode("ep-1", "u1")
        vr.delete_index.assert_called_once()
        assert success is True

    @pytest.mark.asyncio
    async def test_delete_episode_删除失败时不调用向量索引删除(self) -> None:
        """底层删除失败时不调用向量索引删除。"""
        vr = AsyncMock()
        storage = _make_episode_storage()
        storage.delete = AsyncMock(return_value=False)
        svc = _make_service(episode_storage=storage, vector_retriever=vr)
        success = await svc.delete_episode("ep-1", "u1")
        vr.delete_index.assert_not_called()
        assert success is False


# ============================================================
# 3. 知识记忆操作
# ============================================================


class TestKnowledgeOperations:
    """测试知识记忆 CRUD 操作。"""

    @pytest.mark.asyncio
    async def test_store_knowledge_无向量检索器(self) -> None:
        """存储知识，无 vector_retriever 时直接返回 ID。"""
        svc = _make_service()
        kn = Knowledge(user_id="u1", content="知识内容")
        kid = await svc.store_knowledge(kn)
        assert kid == "kn-1"

    @pytest.mark.asyncio
    async def test_store_knowledge_有向量检索器且有嵌入(self) -> None:
        """有 vector_retriever 且知识有嵌入时调用 save_index。"""
        vr = AsyncMock()
        vr.save_index = AsyncMock()
        svc = _make_service(vector_retriever=vr)
        kn = Knowledge(user_id="u1", content="知识", embedding=[0.1, 0.2])
        kid = await svc.store_knowledge(kn)
        vr.save_index.assert_called_once()
        assert kid == "kn-1"

    @pytest.mark.asyncio
    async def test_create_knowledge(self) -> None:
        """创建知识应返回字典。"""
        svc = _make_service()
        result = await svc.create_knowledge(
            user_id="u1", content="新知识", source_type="manual",
        )
        assert isinstance(result, dict)
        assert result["user_id"] == "u1"
        assert result["content"] == "新知识"

    @pytest.mark.asyncio
    async def test_list_semantic_memory(self) -> None:
        """列出语义记忆应返回字典。"""
        svc = _make_service()
        result = await svc.list_semantic_memory("u1")
        assert "items" in result
        assert "total" in result

    @pytest.mark.asyncio
    async def test_delete_knowledge(self) -> None:
        """删除知识应调用底层删除。"""
        storage = _make_semantic_storage()
        storage.get = AsyncMock(return_value=Knowledge(id="kn-1", user_id="u1"))
        svc = _make_service(semantic_storage=storage)
        success = await svc.delete_knowledge("kn-1", "u1")
        assert success is True

    @pytest.mark.asyncio
    async def test_delete_knowledge_有向量检索器(self) -> None:
        """删除知识，有 vector_retriever 时同步删除向量索引。"""
        storage = _make_semantic_storage()
        storage.get = AsyncMock(return_value=Knowledge(id="kn-1", user_id="u1"))
        vr = AsyncMock()
        vr.delete_index = AsyncMock()
        svc = _make_service(semantic_storage=storage, vector_retriever=vr)
        success = await svc.delete_knowledge("kn-1", "u1")
        vr.delete_index.assert_called_once()
        assert success is True


# ============================================================
# 4. 三层决策检索
# ============================================================


class TestRetrieve:
    """测试统一检索接口（三层决策模型）。"""

    @pytest.mark.asyncio
    async def test_full注入_有vector检索器(self) -> None:
        """full 注入方式应调用 vector 检索器。"""
        r = _make_retriever()
        r.retrieve = AsyncMock(return_value=[
            SearchResult(id="1", content="c", score=0.9),
        ])
        svc = _make_service(retrievers={"vector": r})
        results = await svc.retrieve(
            user_id="u1", inject_type="full", filter={"memory_type": "semantic"},
        )
        assert len(results) == 1
        r.retrieve.assert_called_once()

    @pytest.mark.asyncio
    async def test_full注入_无vector检索器(self) -> None:
        """full 注入方式无 vector 检索器时返回空列表。"""
        svc = _make_service()
        results = await svc.retrieve(user_id="u1", inject_type="full")
        assert results == []

    @pytest.mark.asyncio
    async def test_summary注入(self) -> None:
        """summary 注入方式应先 vector 检索再返回结果。"""
        r = _make_retriever()
        r.retrieve = AsyncMock(return_value=[
            SearchResult(id="1", content="c", score=0.8),
        ])
        svc = _make_service(retrievers={"vector": r})
        results = await svc.retrieve(user_id="u1", inject_type="summary", query="测试")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_retrieval注入_vector方法(self) -> None:
        """retrieval + vector 应调用 vector 检索器。"""
        r = _make_retriever()
        r.retrieve = AsyncMock(return_value=[
            SearchResult(id="1", content="c", score=0.7),
        ])
        svc = _make_service(retrievers={"vector": r})
        results = await svc.retrieve(
            user_id="u1", inject_type="retrieval",
            retrieval_method="vector", query="测试",
        )
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_retrieval注入_keyword方法(self) -> None:
        """retrieval + keyword 应调用 keyword 检索器。"""
        r = _make_retriever()
        r.retrieve = AsyncMock(return_value=[
            SearchResult(id="1", content="c", score=0.6),
        ])
        svc = _make_service(retrievers={"keyword": r})
        results = await svc.retrieve(
            user_id="u1", inject_type="retrieval",
            retrieval_method="keyword", query="测试",
        )
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_retrieval注入_tagwave方法(self) -> None:
        """retrieval + tagwave 应调用 tagwave 检索器。"""
        r = _make_retriever()
        r.retrieve = AsyncMock(return_value=[])
        svc = _make_service(retrievers={"tagwave": r})
        await svc.retrieve(
            user_id="u1", inject_type="retrieval",
            retrieval_method="tagwave", query="测试",
        )
        r.retrieve.assert_called_once()

    @pytest.mark.asyncio
    async def test_retrieval_无query返回空(self) -> None:
        """retrieval 方式无 query 时返回空列表。"""
        r = _make_retriever()
        svc = _make_service(retrievers={"vector": r})
        results = await svc.retrieve(user_id="u1", inject_type="retrieval", query=None)
        assert results == []

    @pytest.mark.asyncio
    async def test_retrieval_无对应检索器返回空(self) -> None:
        """retrieval 方式无对应检索器时返回空列表。"""
        svc = _make_service(retrievers={"vector": _make_retriever()})
        results = await svc.retrieve(
            user_id="u1", inject_type="retrieval",
            retrieval_method="keyword", query="测试",
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_retrieval_检索失败返回空(self) -> None:
        """检索器抛异常时返回空列表。"""
        r = _make_retriever()
        r.retrieve = AsyncMock(side_effect=Exception("检索错误"))
        svc = _make_service(retrievers={"vector": r})
        results = await svc.retrieve(
            user_id="u1", inject_type="retrieval",
            retrieval_method="vector", query="测试",
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_无效注入类型抛ValueError(self) -> None:
        """传入无效的 inject_type 应抛 ValueError。"""
        svc = _make_service()
        with pytest.raises(ValueError):
            await svc.retrieve(user_id="u1", inject_type="invalid_type")


# ============================================================
# 5. 搜索
# ============================================================


class TestSearch:
    """测试搜索功能。"""

    @pytest.mark.asyncio
    async def test_搜索默认类型(self) -> None:
        """默认搜索 episode + semantic 两种类型。"""
        r = _make_retriever()
        r.retrieve = AsyncMock(return_value=[
            SearchResult(id="1", content="c", score=0.9, memory_type=MemoryType.SEMANTIC),
        ])
        svc = _make_service(retrievers={"vector": r})
        result = await svc.search(user_id="u1", query="测试")
        assert "items" in result
        assert "total" in result
        assert result["query"] == "测试"

    @pytest.mark.asyncio
    async def test_搜索指定类型(self) -> None:
        """指定 memory_types 只搜索对应类型。"""
        r = _make_retriever()
        r.retrieve = AsyncMock(return_value=[
            SearchResult(id="1", content="c", score=0.9),
        ])
        svc = _make_service(retrievers={"vector": r})
        await svc.search(user_id="u1", query="测试", memory_types=["episode"])
        assert r.retrieve.call_count == 1

    @pytest.mark.asyncio
    async def test_搜索分值过滤(self) -> None:
        """min_score 应过滤低分结果。"""
        r = _make_retriever()
        r.retrieve = AsyncMock(return_value=[
            SearchResult(id="1", content="c", score=0.3),
            SearchResult(id="2", content="c2", score=0.8),
        ])
        svc = _make_service(retrievers={"vector": r})
        result = await svc.search(user_id="u1", query="测试", min_score=0.5)
        # 只有 score >= 0.5 的结果
        assert all(item["score"] >= 0.5 for item in result["items"])

    @pytest.mark.asyncio
    async def test_搜索结果按分数降序(self) -> None:
        """搜索结果应按分数降序排列。"""
        r = _make_retriever()
        r.retrieve = AsyncMock(side_effect=[
            [SearchResult(id="1", content="a", score=0.5)],
            [SearchResult(id="2", content="b", score=0.9)],
        ])
        svc = _make_service(retrievers={"vector": r})
        result = await svc.search(user_id="u1", query="测试")
        scores = [item["score"] for item in result["items"]]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_搜索top_k限制(self) -> None:
        """top_k 应限制返回数量。"""
        r = _make_retriever()
        r.retrieve = AsyncMock(return_value=[
            SearchResult(id=str(i), content=f"c{i}", score=0.9)
            for i in range(20)
        ])
        svc = _make_service(retrievers={"vector": r})
        result = await svc.search(user_id="u1", query="测试", top_k=3)
        assert len(result["items"]) <= 3


# ============================================================
# 6. get_stats
# ============================================================


class TestGetStats:
    """测试统计信息。"""

    @pytest.mark.asyncio
    async def test_get_stats返回正确结构(self) -> None:
        """get_stats 应返回包含各计数的字典。"""
        svc = _make_service()
        stats = await svc.get_stats("u1")
        assert "episode_count" in stats
        assert "knowledge_count" in stats
        assert "total_count" in stats
        assert "last_updated" in stats

    @pytest.mark.asyncio
    async def test_get_stats计数正确(self) -> None:
        """get_stats 应正确汇总情景记忆和知识数量。"""
        storage = _make_episode_storage()
        storage.find_by_user = AsyncMock(return_value=[])  # 0 episodes
        semantic = _make_semantic_storage()
        semantic.find_by_user = AsyncMock(return_value=[
            Knowledge(user_id="u1", content="k1"),
            Knowledge(user_id="u1", content="k2"),
        ])
        svc = _make_service(episode_storage=storage, semantic_storage=semantic)
        stats = await svc.get_stats("u1")
        assert stats["knowledge_count"] == 2
        assert stats["total_count"] == 2


# ============================================================
# 7. store（通用存储）
# ============================================================


class TestStore:
    """测试通用存储方法。"""

    @pytest.mark.asyncio
    async def test_store创建episode(self) -> None:
        """通用存储应创建情景记忆并返回 ID。"""
        svc = _make_service()
        eid = await svc.store(
            user_id="u1", session_id="s1",
            category="chat", content="这是一段对话内容",
        )
        assert eid == "ep-1"

    @pytest.mark.asyncio
    async def test_store_带metadata(self) -> None:
        """通用存储应支持 metadata 中的 tags。"""
        svc = _make_service()
        eid = await svc.store(
            user_id="u1", session_id="s1",
            category="chat", content="内容",
            metadata={"tags": ["custom_tag"]},
        )
        assert eid == "ep-1"

    @pytest.mark.asyncio
    async def test_store_无metadata时用category作tag(self) -> None:
        """无 metadata 时应使用 category 作为 tag。"""
        svc = _make_service()
        eid = await svc.store(
            user_id="u1", session_id="s1",
            category="chat", content="内容",
        )
        assert eid == "ep-1"


# ============================================================
# 8. 压缩块操作
# ============================================================


class TestChunkOperations:
    """测试压缩块 CRUD 操作。"""

    @pytest.mark.asyncio
    async def test_store_chunk_有chunk服务(self) -> None:
        """有 chunk_service 时应委托存储。"""
        chunk_svc = AsyncMock()
        chunk_svc.save = AsyncMock(return_value="chunk-1")
        svc = _make_service(chunk_service=chunk_svc)
        chunk = ChunkData(id="chunk-1", content="测试块")
        cid = await svc.store_chunk(chunk)
        assert cid == "chunk-1"
        chunk_svc.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_chunk_无chunk服务(self) -> None:
        """无 chunk_service 时应返回 chunk_data.id。"""
        svc = _make_service()
        chunk = ChunkData(id="chunk-x", content="测试块")
        cid = await svc.store_chunk(chunk)
        assert cid == "chunk-x"

    @pytest.mark.asyncio
    async def test_get_chunk_有chunk服务且存在(self) -> None:
        """有 chunk_service 且存在时应返回字典。"""
        chunk_svc = AsyncMock()
        chunk_data = ChunkData(id="chunk-1", content="内容")
        chunk_svc.load = AsyncMock(return_value=chunk_data)
        svc = _make_service(chunk_service=chunk_svc)
        result = await svc.get_chunk("chunk-1")
        assert result is not None
        assert result["id"] == "chunk-1"

    @pytest.mark.asyncio
    async def test_get_chunk_有chunk服务但不存在(self) -> None:
        """有 chunk_service 但不存在时应返回 None。"""
        chunk_svc = AsyncMock()
        chunk_svc.load = AsyncMock(return_value=None)
        svc = _make_service(chunk_service=chunk_svc)
        result = await svc.get_chunk("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_chunk_无chunk服务(self) -> None:
        """无 chunk_service 时应返回 None。"""
        svc = _make_service()
        result = await svc.get_chunk("any-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_chunk_有chunk服务(self) -> None:
        """有 chunk_service 时应委托删除。"""
        chunk_svc = AsyncMock()
        chunk_svc.delete = AsyncMock(return_value=True)
        svc = _make_service(chunk_service=chunk_svc)
        success = await svc.delete_chunk("chunk-1")
        assert success is True

    @pytest.mark.asyncio
    async def test_delete_chunk_无chunk服务(self) -> None:
        """无 chunk_service 时应返回 False。"""
        svc = _make_service()
        success = await svc.delete_chunk("chunk-1")
        assert success is False


# ============================================================
# 9. register_retriever
# ============================================================


class TestRegisterRetriever:
    """测试检索器注册。"""

    def test_注册新检索器(self) -> None:
        """注册新检索器应添加到 _retrievers。"""
        svc = _make_service()
        r = _make_retriever()
        svc.register_retriever("keyword", r)
        assert "keyword" in svc._retrievers

    def test_覆盖已有检索器(self) -> None:
        """注册同名检索器应覆盖。"""
        svc = _make_service()
        r1 = _make_retriever()
        r2 = _make_retriever()
        svc.register_retriever("vector", r1)
        svc.register_retriever("vector", r2)
        assert svc._retrievers["vector"] is r2


# ============================================================
# 10. 无存储时内存降级行为
# ============================================================


class TestInMemoryFallback:
    """测试无存储后端时的内存降级。"""

    @pytest.mark.asyncio
    async def test_无后端时store_episode(self) -> None:
        """无后端时存储情景记忆应使用内存存储。"""
        svc = MemoryService()
        ep = Episode(user_id="u1", intent_text="测试")
        eid = await svc.store_episode(ep)
        assert eid == ep.id

    @pytest.mark.asyncio
    async def test_无后端时create_and_get_episode(self) -> None:
        """无后端时创建并获取情景记忆。"""
        svc = MemoryService()
        created = await svc.create_episode(user_id="u1", intent_text="意图")
        result = await svc.get_episode(created["id"], "u1")
        assert result is not None
        assert result["intent_text"] == "意图"

    @pytest.mark.asyncio
    async def test_无后端时store_knowledge(self) -> None:
        """无后端时存储知识应使用内存存储。"""
        svc = MemoryService()
        kn = Knowledge(user_id="u1", content="知识")
        kid = await svc.store_knowledge(kn)
        assert kid == kn.id

    @pytest.mark.asyncio
    async def test_无后端时list_episodes(self) -> None:
        """无后端时列出情景记忆。"""
        svc = MemoryService()
        await svc.create_episode(user_id="u1", intent_text="意图1")
        await svc.create_episode(user_id="u1", intent_text="意图2")
        result = await svc.list_episodes("u1")
        assert result["total"] == 2

    @pytest.mark.asyncio
    async def test_无后端时delete_episode(self) -> None:
        """无后端时删除情景记忆。"""
        svc = MemoryService()
        created = await svc.create_episode(user_id="u1", intent_text="意图")
        success = await svc.delete_episode(created["id"], "u1")
        assert success is True
        result = await svc.get_episode(created["id"], "u1")
        assert result is None

    @pytest.mark.asyncio
    async def test_无后端时get_embedding返回None(self) -> None:
        """无 embedding_service 时 get_embedding 返回 None。"""
        svc = MemoryService()
        result = await svc.get_embedding("测试文本")
        assert result is None

    @pytest.mark.asyncio
    async def test_有embedding_service时调用embed_text(self) -> None:
        """有 embedding_service 且有 embed_text 方法时调用。"""
        es = AsyncMock()
        es.embed_text = AsyncMock(return_value=[0.1, 0.2])
        svc = MemoryService(embedding_service=es)
        result = await svc.get_embedding("测试")
        assert result == [0.1, 0.2]

    @pytest.mark.asyncio
    async def test_有embedding_service时调用embed(self) -> None:
        """有 embedding_service 且无 embed_text 但有 embed 时调用。"""
        es = AsyncMock(spec=[])
        es.embed = AsyncMock(return_value=[0.3])
        svc = MemoryService(embedding_service=es)
        result = await svc.get_embedding("测试")
        assert result == [0.3]
