# @feature: FP-MIGR 0.1→0.2迁移（0.1 遗留测试） | @ci: python-plugins-test
"""记忆管理路由测试（Step 7 重建：数据源切到 IMemoryBackend）。

覆盖 /api/v1/memory/* 端点（routes_memory）：
- 无后端注入时所有列表/统计端点空结果降级（等价旧 memory_store 空 dict 行为）
- 注入后端后 list/search/episodes/semantic/stats/delete/import 正确透传参数

测试通过模块级 `set_memory_backend()` 注入 AsyncMock 后端，不做任何真实能力调用。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

from tests.channels.conftest import use_channel

use_channel("api")
import routes_memory  # noqa: E402
from deps import APIError  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_backend():
    """每个测试前清空注入的后端，保证用例相互独立。"""
    routes_memory.set_memory_backend(None)
    yield


def _backend_item(mem_id: str, content: str = "内容", mem_type: str = "semantic") -> dict:
    """构造统一形态后端结果 {id, content, score, memory_type, metadata}。"""
    return {
        "id": mem_id,
        "content": content,
        "score": 0.8,
        "memory_type": mem_type,
        "metadata": {"tags": ["t1"], "created_at": "2026-08-01T00:00:00Z"},
    }


# ── 1. 无后端降级 ─────────────────────────────────────────────


class TestNoBackendFallback:
    """未注入后端时保持旧行为（memory_store 空 dict）：空结果，不崩溃。"""

    async def test_list_without_backend_empty(self):
        result = await routes_memory.list_memories(
            memory_type=None, limit=20, offset=0, _user={"sub": "test_user"}
        )
        assert result.items == []
        assert result.total == 0

    async def test_stats_zero_without_backend(self):
        result = await routes_memory.get_memory_stats(_user={"sub": "test_user"})
        assert result == {
            "episode_count": 0,
            "knowledge_count": 0,
            "total_count": 0,
            "last_updated": "",
        }


# ── 2. 后端注入后的端点行为 ────────────────────────────────────


class TestListWithBackend:
    """GET / 列表 → backend.search(query="", ...)。"""

    async def test_list_calls_backend_search(self):
        backend = AsyncMock()
        backend.search.return_value = [_backend_item("m1", mem_type="episode")]
        routes_memory.set_memory_backend(backend)

        result = await routes_memory.list_memories(
            memory_type="episode", limit=10, offset=0, _user={"sub": "u1"}
        )
        backend.search.assert_awaited_once_with(
            query="", user_id="u1", top_k=10, memory_type="episode"
        )
        assert result.total == 1
        assert result.items[0].id == "m1"
        assert result.items[0].content == "内容"


class TestSearch:
    """GET/POST /search → backend.search(query, top_k)。"""

    async def test_search_memories(self):
        backend = AsyncMock()
        backend.search.return_value = [_backend_item("s1")]
        routes_memory.set_memory_backend(backend)

        result = await routes_memory.search_memories(
            query="关键词", top_k=5, method="keyword", _user={"sub": "u1"}
        )
        backend.search.assert_awaited_once_with(
            query="关键词", user_id="u1", top_k=5, memory_type=None
        )
        assert result.total == 1
        assert result.items[0].id == "s1"

    async def test_search_memories_post(self):
        backend = AsyncMock()
        backend.search.return_value = [_backend_item("s2")]
        routes_memory.set_memory_backend(backend)

        result = await routes_memory.search_memories_post(
            {"query": "q", "top_k": 3}, _user={"sub": "u1"}
        )
        backend.search.assert_awaited_once_with(
            query="q", user_id="u1", top_k=3, memory_type=None
        )
        assert result.total == 1


class TestEpisodes:
    """GET /episodes → backend.search(memory_type="episode")。"""

    async def test_episodes_filter(self):
        backend = AsyncMock()
        backend.search.return_value = [_backend_item("e1", mem_type="episode")]
        routes_memory.set_memory_backend(backend)

        result = await routes_memory.list_episodes(
            page=1, page_size=20, _user={"sub": "u1"}
        )
        backend.search.assert_awaited_once_with(
            query="", user_id="u1", top_k=20, memory_type="episode"
        )
        assert result["total"] == 1
        assert result["items"][0]["id"] == "e1"
        assert result["items"][0]["intent_text"] == "内容"

    async def test_get_episode_filters_by_id(self):
        backend = AsyncMock()
        backend.search.return_value = [_backend_item("e1", mem_type="episode")]
        routes_memory.set_memory_backend(backend)

        result = await routes_memory.get_episode("e1", _user={"sub": "u1"})
        assert result["id"] == "e1"

    async def test_get_episode_404_when_missing(self):
        backend = AsyncMock()
        backend.search.return_value = []
        routes_memory.set_memory_backend(backend)

        with pytest.raises(APIError) as exc:
            await routes_memory.get_episode("nope", _user={"sub": "u1"})
        assert exc.value.status_code == 404


class TestImportDocument:
    """POST /import → backend.import_document。"""

    async def test_import_document_calls_backend(self):
        backend = AsyncMock()
        backend.import_document.return_value = {
            "chunks_imported": 3,
            "name": "知识库",
            "total_chunks": 3,
        }
        routes_memory.set_memory_backend(backend)

        result = await routes_memory.import_document(
            text="文档内容", name="知识库", _user={"sub": "u1"}
        )
        backend.import_document.assert_awaited_once_with(
            user_id="u1", text="文档内容", file_path=None, name="知识库"
        )
        assert result == {"imported": 3, "name": "知识库"}


class TestStats:
    """GET /stats → 按类型聚合计数。"""

    async def test_stats_with_backend_counts(self):
        backend = AsyncMock()
        backend.search.side_effect = [
            [_backend_item("e1", mem_type="episode"), _backend_item("e2", mem_type="episode")],
            [_backend_item("s1", mem_type="semantic")],
        ]
        routes_memory.set_memory_backend(backend)

        result = await routes_memory.get_memory_stats(_user={"sub": "u1"})
        assert result["episode_count"] == 2
        assert result["knowledge_count"] == 1
        assert result["total_count"] == 3


class TestDelete:
    """DELETE /{memory_id} → backend.delete。"""

    async def test_delete_memory(self):
        backend = AsyncMock()
        backend.delete.return_value = True
        routes_memory.set_memory_backend(backend)

        result = await routes_memory.delete_memory("mem-1", _user={"sub": "u1"})
        assert result == {"message": "记忆已删除"}
        backend.delete.assert_awaited_once_with(user_id="u1", memory_id="mem-1")

    async def test_delete_memory_404_when_not_deleted(self):
        backend = AsyncMock()
        backend.delete.return_value = False
        routes_memory.set_memory_backend(backend)

        with pytest.raises(APIError) as exc:
            await routes_memory.delete_memory("mem-1", _user={"sub": "u1"})
        assert exc.value.status_code == 404
