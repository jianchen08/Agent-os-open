"""JSON 文件存储测试。

测试 JsonMemoryStore 的 CRUD 操作和搜索功能。
使用临时目录避免污染实际数据。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from memory.storage.json_store import JsonMemoryStore
from memory.types import Episode, Knowledge


@pytest.fixture
def temp_dir() -> str:
    """创建临时目录用于测试。"""
    d = tempfile.mkdtemp(prefix="test_memory_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(temp_dir: str) -> JsonMemoryStore:
    """创建 JsonMemoryStore 实例。"""
    return JsonMemoryStore(data_dir=temp_dir)


class TestJsonMemoryStoreEpisodes:
    """情景记忆 CRUD 测试。"""

    @pytest.mark.asyncio
    async def test_save_and_load(self, store: JsonMemoryStore) -> None:
        """测试保存和加载。"""
        episode = Episode(
            user_id="user-1",
            session_id="session-1",
            intent_text="测试意图",
            execution_summary="执行摘要",
            tags=["test"],
        )

        # 保存
        entry_id = await store.save(episode, "episode")
        assert entry_id == episode.id

        # 加载
        loaded = await store.load(episode.id, "episode")
        assert loaded is not None
        assert loaded.user_id == "user-1"
        assert loaded.intent_text == "测试意图"

    @pytest.mark.asyncio
    async def test_delete(self, store: JsonMemoryStore) -> None:
        """测试删除。"""
        episode = Episode(user_id="user-1", intent_text="待删除")
        await store.save(episode, "episode")

        # 删除
        result = await store.delete(episode.id, "episode")
        assert result is True

        # 确认已删除
        loaded = await store.load(episode.id, "episode")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, store: JsonMemoryStore) -> None:
        """测试删除不存在的条目。"""
        result = await store.delete("nonexistent-id", "episode")
        assert result is False

    @pytest.mark.asyncio
    async def test_find_by_user(self, store: JsonMemoryStore) -> None:
        """测试按用户查找。"""
        for i in range(5):
            ep = Episode(user_id="user-1", intent_text=f"意图{i}")
            await store.save(ep, "episode")

        # 另一个用户
        ep2 = Episode(user_id="user-2", intent_text="其他用户")
        await store.save(ep2, "episode")

        # 查找 user-1
        results = await store.find_by_user("user-1")
        assert len(results) == 5

        # 查找 user-2
        results = await store.find_by_user("user-2")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_count_by_user(self, store: JsonMemoryStore) -> None:
        """测试统计用户记忆数。"""
        for i in range(3):
            ep = Episode(user_id="user-1", intent_text=f"意图{i}")
            await store.save(ep, "episode")

        count = await store.count_by_user("user-1")
        assert count == 3

        count = await store.count_by_user("user-999")
        assert count == 0

    @pytest.mark.asyncio
    async def test_update(self, store: JsonMemoryStore) -> None:
        """测试更新。"""
        episode = Episode(user_id="user-1", intent_text="原始意图")
        await store.save(episode, "episode")

        # 更新
        result = await store.update(episode.id, execution_summary="新摘要")
        assert result is True

        # 验证
        loaded = await store.load(episode.id, "episode")
        assert loaded is not None
        assert loaded.execution_summary == "新摘要"

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, store: JsonMemoryStore) -> None:
        """测试更新不存在的条目。"""
        result = await store.update("nonexistent-id", execution_summary="无意义")
        assert result is False


class TestJsonMemoryStoreKnowledge:
    """知识 CRUD 测试。"""

    @pytest.mark.asyncio
    async def test_save_and_load(self, store: JsonMemoryStore) -> None:
        """测试保存和加载知识。"""
        knowledge = Knowledge(
            user_id="user-1",
            source_type="file",
            content="知识内容",
            extra_data={"key": "value"},
        )

        entry_id = await store.save(knowledge, "semantic")
        assert entry_id == knowledge.id

        loaded = await store.load(knowledge.id, "semantic")
        assert loaded is not None
        assert loaded.content == "知识内容"
        assert loaded.extra_data == {"key": "value"}

    @pytest.mark.asyncio
    async def test_delete_knowledge(self, store: JsonMemoryStore) -> None:
        """测试删除知识。"""
        knowledge = Knowledge(user_id="user-1", content="待删除知识")
        await store.save(knowledge, "semantic")

        result = await store.delete(knowledge.id, "semantic")
        assert result is True

        loaded = await store.load(knowledge.id, "semantic")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_find_knowledge_by_user(self, store: JsonMemoryStore) -> None:
        """测试按用户查找知识。"""
        for i in range(3):
            kn = Knowledge(user_id="user-1", content=f"知识{i}", source_type="test")
            await store.save(kn, "semantic")

        results = await store.find_knowledge_by_user("user-1")
        assert len(results) == 3


class TestJsonMemoryStoreSearch:
    """搜索测试。"""

    @pytest.mark.asyncio
    async def test_search_episodes(self, store: JsonMemoryStore) -> None:
        """测试搜索情景记忆。"""
        ep1 = Episode(user_id="user-1", intent_text="Python 编程", execution_summary="Python开发", tags=["python"])
        ep2 = Episode(user_id="user-1", intent_text="Java 编程", execution_summary="Java开发", tags=["java"])
        ep3 = Episode(user_id="user-1", intent_text="做饭菜谱")

        await store.save(ep1, "episode")
        await store.save(ep2, "episode")
        await store.save(ep3, "episode")

        results = await store.search("编程", user_id="user-1")
        assert len(results) >= 2  # Python 和 Java 都包含"编程"

    @pytest.mark.asyncio
    async def test_search_knowledge(self, store: JsonMemoryStore) -> None:
        """测试搜索知识。"""
        kn1 = Knowledge(user_id="user-1", content="Python 是一种编程语言", source_type="wiki")
        kn2 = Knowledge(user_id="user-1", content="今天天气不错", source_type="chat")

        await store.save(kn1, "semantic")
        await store.save(kn2, "semantic")

        results = await store.search("Python", user_id="user-1", filters={"memory_type": "semantic"})
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_empty_query(self, store: JsonMemoryStore) -> None:
        """测试空查询。"""
        results = await store.search("", user_id="user-1")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_no_results(self, store: JsonMemoryStore) -> None:
        """测试无结果的搜索。"""
        results = await store.search("量子物理", user_id="user-1")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_limit(self, store: JsonMemoryStore) -> None:
        """测试搜索结果限制。"""
        for i in range(10):
            kn = Knowledge(user_id="user-1", content=f"Python知识{i}", source_type="test")
            await store.save(kn, "semantic")

        results = await store.search("Python", user_id="user-1", limit=3)
        assert len(results) <= 3


class TestJsonMemoryStorePersistence:
    """持久化测试。"""

    @pytest.mark.asyncio
    async def test_persist_and_reload(self, temp_dir: str) -> None:
        """测试数据持久化和重新加载。"""
        # 创建存储并写入数据
        store1 = JsonMemoryStore(data_dir=temp_dir)
        ep = Episode(user_id="user-1", intent_text="持久化测试")
        await store1.save(ep, "episode")

        # 重新创建存储实例（模拟重启）
        store2 = JsonMemoryStore(data_dir=temp_dir)
        loaded = await store2.load(ep.id, "episode")
        assert loaded is not None
        assert loaded.intent_text == "持久化测试"

    @pytest.mark.asyncio
    async def test_file_structure(self, temp_dir: str) -> None:
        """测试文件目录结构。"""
        store = JsonMemoryStore(data_dir=temp_dir)

        ep = Episode(user_id="user-1", intent_text="测试")
        await store.save(ep, "episode")

        kn = Knowledge(user_id="user-1", content="知识", source_type="test")
        await store.save(kn, "semantic")

        # 检查目录结构
        assert (Path(temp_dir) / "episodes").exists()
        assert (Path(temp_dir) / "knowledge").exists()
        assert (Path(temp_dir) / "episodes" / f"{ep.id}.json").exists()
        assert (Path(temp_dir) / "knowledge" / f"{kn.id}.json").exists()
