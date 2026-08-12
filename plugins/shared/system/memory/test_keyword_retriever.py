# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: python-plugins-test
"""KeywordRetriever 关键词检索器单元测试（阶段 4.3：低覆盖系统插件补测）。

memory 关键词检索此前 0 测试（traceability 表 B FP-0.2.六 标"未测"）。
覆盖朴素子串匹配 + TF 比例打分、排序、top_k 截断、memory_type=all 双类检索、
空查询与无匹配的降级。不依赖真实存储——用假 store 注入。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

# 插件目录加入 sys.path（与 server.py 自身的 sys.path 注入对齐，平铺 import）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from keyword_retriever import KeywordRetriever  # noqa: E402
from models import MemoryType  # noqa: E402


def _mem(mid: str, content: str, metadata: dict | None = None) -> dict:
    """构造一条 store 记忆条目。"""
    return {"id": mid, "content": content, "metadata": metadata or {}}


def _store(memories_by_type: dict[str, list[dict]]) -> MagicMock:
    """构造假 store：list_memories(type) 返回该 type 对应的记忆列表。"""

    def _list(memory_type: str, user_id=None, limit=1000):
        return memories_by_type.get(memory_type, [])[:limit]

    store = MagicMock()
    store.list_memories.side_effect = _list
    return store


@pytest.mark.asyncio
async def test_empty_query_returns_empty() -> None:
    retriever = KeywordRetriever(_store({}))
    assert await retriever.retrieve("") == []


@pytest.mark.asyncio
async def test_substring_match_and_tf_scoring() -> None:
    # "alpha" 在两条里都出现，但 m2 占比更高 → 排前
    store = _store(
        {"semantic": [_mem("m1", "alpha beta gamma"), _mem("m2", "alpha alpha")]}
    )
    retriever = KeywordRetriever(store)
    results = await retriever.retrieve("alpha", top_k=5)
    assert [r.id for r in results] == ["m2", "m1"]
    assert results[0].score > results[1].score
    assert all(r.score > 0 for r in results)


@pytest.mark.asyncio
async def test_top_k_truncation() -> None:
    store = _store(
        {"semantic": [_mem(f"m{i}", f"key key key{i}") for i in range(10)]}
    )
    retriever = KeywordRetriever(store)
    results = await retriever.retrieve("key", top_k=3)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_non_matching_excluded() -> None:
    store = _store({"semantic": [_mem("m1", "alpha"), _mem("m2", "totally unrelated")]})
    retriever = KeywordRetriever(store)
    results = await retriever.retrieve("alpha")
    assert [r.id for r in results] == ["m1"]


@pytest.mark.asyncio
async def test_case_insensitive_match() -> None:
    store = _store({"semantic": [_mem("m1", "Hello WORLD")]})
    retriever = KeywordRetriever(store)
    results = await retriever.retrieve("WORLD")
    assert len(results) == 1
    assert results[0].id == "m1"


@pytest.mark.asyncio
async def test_memory_type_all_searches_both_types() -> None:
    store = _store(
        {
            "episode": [_mem("e1", "shared keyword here")],
            "semantic": [_mem("s1", "keyword semantic one")],
        }
    )
    retriever = KeywordRetriever(store)
    results = await retriever.retrieve("keyword", memory_type="all")
    ids = {r.id for r in results}
    assert ids == {"e1", "s1"}
    # episode 命中应标记为 EPISODE，semantic 命中标记为 SEMANTIC
    types = {r.id: r.memory_type for r in results}
    assert types["e1"] == MemoryType.EPISODE
    assert types["s1"] == MemoryType.SEMANTIC


@pytest.mark.asyncio
async def test_results_sorted_descending_by_score() -> None:
    # 三条命中，构造明显不同的 TF 比例
    store = _store(
        {
            "semantic": [
                _mem("low", "x foo a b c d e f"),  # foo 占比低
                _mem("high", "foo foo foo"),  # foo 占比最高
                _mem("mid", "foo foo zzz"),
            ]
        }
    )
    retriever = KeywordRetriever(store)
    results = await retriever.retrieve("foo", top_k=10)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    assert results[0].id == "high"


@pytest.mark.asyncio
async def test_user_id_passed_to_store() -> None:
    store = _store({"semantic": [_mem("m1", "alpha")]})
    retriever = KeywordRetriever(store)
    await retriever.retrieve("alpha", user_id="u-123")
    store.list_memories.assert_called()
    # list_memories 的 user_id 参数应为传入值
    assert any(call.kwargs.get("user_id") == "u-123" for call in store.list_memories.call_args_list)
