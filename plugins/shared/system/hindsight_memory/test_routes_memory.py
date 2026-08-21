# @feature: channel_api 退役批次 1 memory 域 | @ci: none-local
"""memory 域端点测试（自持迁移版 routes_memory.py）。

验证内容：
1. 缺省参数与响应形态（list/search/episodes/semantic/stats/consolidate/单条读写删）
   ——与原 channel_api routes_memory.py 响应逐项对齐（前端 memory.ts 消费形态）；
2. IMemoryBackend 注入：set_memory_backend(mock) 后参数透传、未注入时空结果降级；
3. 404 语义：get_episode/get_memory/delete_memory 未命中抛 MemoryAPIError(404)；
4. plugin.json 声明了全部 10 个 memory 域端点（/ext/hindsight_memory_service/memory/**，
   auth=user、timeout 沿用源值）。

测试不依赖真实 hindsight 包——注入 AsyncMock 后端实现。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 routes_memory.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_routes_memory_test"
    path = _PLUGIN_DIR / "routes_memory.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None, "Cannot load routes_memory.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _backend_with(search_results: list[dict[str, Any]], delete_result: bool = True) -> MagicMock:
    """构造 IMemoryBackend mock：search 返回固定列表，delete 返回固定 bool。"""
    backend = MagicMock()
    backend.search = AsyncMock(return_value=search_results)
    backend.delete = AsyncMock(return_value=delete_result)
    return backend


def _m(id_: str, content: str = "c", mtype: str = "semantic", score: float = 1.0, **meta: Any) -> dict[str, Any]:
    """构造后端统一形态记忆条目。"""
    return {"id": id_, "content": content, "score": score, "memory_type": mtype, "metadata": meta}


@pytest.fixture
def mod() -> Any:
    module = _load_module()
    module._memory_backend = None
    return module


# ═══════════════════════════════════════════════════════════
# 列表 / 搜索
# ═══════════════════════════════════════════════════════════


class TestListMemories:
    def test_list_degrades_without_backend(self, mod: Any) -> None:
        """未注入后端 → {items: [], total: 0}（降级不崩溃）。"""
        result = _run(mod.list_memories(memory_type=None, limit=20, offset=0))
        assert result == {"items": [], "total": 0}

    def test_list_passes_params_and_shapes(self, mod: Any) -> None:
        """参数透传 + 响应形态 {items:[...], total}。"""
        backend = _backend_with([
            _m("m1", "hello", "episode", 0.9, tags=["t1"], created_at="2026-08-21T00:00:00Z"),
            _m("m2", "world", "episode", 0.8),
        ])
        mod.set_memory_backend(backend)

        result = _run(mod.list_memories(memory_type="episode", limit=10, offset=0))

        backend.search.assert_awaited_once()
        kwargs = backend.search.call_args.kwargs
        assert kwargs["memory_type"] == "episode"
        assert kwargs["top_k"] == 10
        assert kwargs["user_id"] == "default"
        assert result["total"] == 2
        item = result["items"][0]
        assert item["id"] == "m1"
        assert item["content"] == "hello"
        assert item["memory_type"] == "episode"
        assert item["tags"] == ["t1"]
        assert item["score"] == 0.9
        assert item["created_at"] == "2026-08-21T00:00:00Z"

    def test_list_rejects_bad_pagination(self, mod: Any) -> None:
        """limit 越界 / offset 负数 → MemoryAPIError(400)。"""
        with pytest.raises(mod.MemoryAPIError) as ei:
            _run(mod.list_memories(memory_type=None, limit=0, offset=0))
        assert ei.value.status_code == 400
        with pytest.raises(mod.MemoryAPIError):
            _run(mod.list_memories(memory_type=None, limit=200, offset=0))
        with pytest.raises(mod.MemoryAPIError):
            _run(mod.list_memories(memory_type=None, limit=20, offset=-1))


class TestSearchMemories:
    def test_method_to_memory_type_passthrough(self, mod: Any) -> None:
        """method=episode → memory_type=episode 过滤透传。"""
        backend = _backend_with([])
        mod.set_memory_backend(backend)
        _run(mod.search_memories(query="q", top_k=5, method="episode"))
        assert backend.search.call_args.kwargs["memory_type"] == "episode"

    def test_generic_method_unfiltered(self, mod: Any) -> None:
        """method=keyword/vector/tagwave → memory_type=None（不限类型）。"""
        for method in ("keyword", "vector", "tagwave", ""):
            backend = _backend_with([])
            mod.set_memory_backend(backend)
            _run(mod.search_memories(query="q", top_k=5, method=method))
            assert backend.search.call_args.kwargs["memory_type"] is None, method

    def test_search_degrades_without_backend(self, mod: Any) -> None:
        result = _run(mod.search_memories(query="q"))
        assert result == {"items": [], "total": 0}

    def test_search_post_body(self, mod: Any) -> None:
        """POST 搜索：body {query, top_k} → backend.search 参数。"""
        backend = _backend_with([_m("m1")])
        mod.set_memory_backend(backend)
        result = _run(mod.search_memories_post({"query": "q", "top_k": 3}))
        assert backend.search.call_args.kwargs["query"] == "q"
        assert backend.search.call_args.kwargs["top_k"] == 3
        assert result["total"] == 1

    def test_search_post_none_body(self, mod: Any) -> None:
        backend = _backend_with([_m("m1")])
        mod.set_memory_backend(backend)
        result = _run(mod.search_memories_post(None))
        assert result == {"items": [], "total": 0}


# ═══════════════════════════════════════════════════════════
# 情景记忆
# ═══════════════════════════════════════════════════════════


class TestEpisodes:
    def test_list_episodes_shape(self, mod: Any) -> None:
        backend = _backend_with([
            _m("e1", "intent text", "episode", tags=["a"], created_at="t0"),
        ])
        mod.set_memory_backend(backend)
        result = _run(mod.list_episodes(page=2, page_size=10))
        assert result["page"] == 2
        assert result["page_size"] == 10
        assert result["items"][0]["id"] == "e1"
        assert result["items"][0]["intent_text"] == "intent text"
        assert result["items"][0]["tags"] == ["a"]
        assert result["items"][0]["created_at"] == "t0"
        assert backend.search.call_args.kwargs["memory_type"] == "episode"

    def test_get_episode_found(self, mod: Any) -> None:
        backend = _backend_with([_m("e1", "intent", "episode")])
        mod.set_memory_backend(backend)
        result = _run(mod.get_episode("e1"))
        assert result["id"] == "e1"
        assert result["intent_text"] == "intent"

    def test_get_episode_not_found_404(self, mod: Any) -> None:
        backend = _backend_with([_m("e1")])
        mod.set_memory_backend(backend)
        with pytest.raises(mod.MemoryAPIError) as ei:
            _run(mod.get_episode("nope"))
        assert ei.value.status_code == 404
        assert ei.value.error_code == "MEM_NOTF_5001"

    def test_get_episode_degrades_without_backend_404(self, mod: Any) -> None:
        with pytest.raises(mod.MemoryAPIError) as ei:
            _run(mod.get_episode("e1"))
        assert ei.value.status_code == 404


# ═══════════════════════════════════════════════════════════
# 语义记忆 / 整合 / 统计
# ═══════════════════════════════════════════════════════════


class TestSemanticConsolidateStats:
    def test_list_semantic_shape(self, mod: Any) -> None:
        backend = _backend_with([_m("s1", "semcontent", "semantic", created_at="t0")])
        mod.set_memory_backend(backend)
        result = _run(mod.list_semantic())
        item = result["items"][0]
        assert item == {
            "id": "s1",
            "content": "semcontent",
            "source_type": "memory_backend",
            "extra_data": {},
            "created_at": "t0",
        }

    def test_consolidate_without_reflect_is_stub(self, mod: Any) -> None:
        """后端无 reflect → 空操作（consolidated_count=0）。"""
        backend = _backend_with([])
        mod.set_memory_backend(backend)
        result = _run(mod.consolidate_memory())
        assert result["success"] is True
        assert result["consolidated_count"] == 0

    def test_consolidate_with_reflect(self, mod: Any) -> None:
        backend = MagicMock()
        backend.reflect = AsyncMock(return_value={"consolidated_count": 7})
        mod.set_memory_backend(backend)
        result = _run(mod.consolidate_memory())
        assert result["consolidated_count"] == 7

    def test_consolidate_reflect_error_degrades_to_zero(self, mod: Any) -> None:
        backend = MagicMock()
        backend.reflect = AsyncMock(side_effect=RuntimeError("boom"))
        mod.set_memory_backend(backend)
        result = _run(mod.consolidate_memory())
        assert result["consolidated_count"] == 0

    def test_stats_counts_by_type(self, mod: Any) -> None:
        async def _search(query: str, user_id: str, top_k: int, memory_type: str | None) -> list[dict[str, Any]]:
            counts = {"episode": [{"id": "a"}], "semantic": [{"id": "b"}, {"id": "c"}]}
            return counts.get(memory_type or "", [])

        backend = MagicMock()
        backend.search = AsyncMock(side_effect=_search)
        mod.set_memory_backend(backend)
        result = _run(mod.get_memory_stats())
        assert result == {
            "episode_count": 1,
            "knowledge_count": 2,
            "total_count": 3,
            "last_updated": "",
        }

    def test_stats_degrades_without_backend(self, mod: Any) -> None:
        result = _run(mod.get_memory_stats())
        assert result["episode_count"] == 0
        assert result["total_count"] == 0


# ═══════════════════════════════════════════════════════════
# 单条读写删（动态路径）
# ═══════════════════════════════════════════════════════════


class TestMemoryItem:
    def test_get_memory_found(self, mod: Any) -> None:
        backend = _backend_with([_m("m1", "content", "episode", 0.5, tags=["x"])])
        mod.set_memory_backend(backend)
        result = _run(mod.get_memory("m1"))
        assert result["id"] == "m1"
        assert result["content"] == "content"
        assert result["memory_type"] == "episode"

    def test_get_memory_not_found_404(self, mod: Any) -> None:
        backend = _backend_with([_m("m1")])
        mod.set_memory_backend(backend)
        with pytest.raises(mod.MemoryAPIError) as ei:
            _run(mod.get_memory("nope"))
        assert ei.value.status_code == 404

    def test_delete_memory_success(self, mod: Any) -> None:
        backend = _backend_with([], delete_result=True)
        mod.set_memory_backend(backend)
        result = _run(mod.delete_memory("m1"))
        assert result == {"message": "记忆已删除"}
        assert backend.delete.call_args.kwargs["memory_id"] == "m1"

    def test_delete_memory_not_found_404(self, mod: Any) -> None:
        backend = _backend_with([], delete_result=False)
        mod.set_memory_backend(backend)
        with pytest.raises(mod.MemoryAPIError) as ei:
            _run(mod.delete_memory("nope"))
        assert ei.value.status_code == 404


# ═══════════════════════════════════════════════════════════
# manifest：memory 域 10 端点声明
# ═══════════════════════════════════════════════════════════


class TestManifestMemoryEndpoints:
    def test_manifest_declares_memory_endpoints(self) -> None:
        """10 个 memory 域端点全部声明（auth=user，前缀 hindsight_memory_service）。"""
        data = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        endpoints = {e["path"]: e for e in data["http_endpoints"]}
        expected = [
            ("/ext/hindsight_memory_service/memory", "GET"),
            ("/ext/hindsight_memory_service/memory/search", "GET"),
            ("/ext/hindsight_memory_service/memory/search", "POST"),
            ("/ext/hindsight_memory_service/memory/episodes", "GET"),
            ("/ext/hindsight_memory_service/memory/episodes/{episode_id}", "GET"),
            ("/ext/hindsight_memory_service/memory/semantic", "GET"),
            ("/ext/hindsight_memory_service/memory/consolidate", "POST"),
            ("/ext/hindsight_memory_service/memory/stats", "GET"),
            ("/ext/hindsight_memory_service/memory/{memory_id}", "GET"),
            ("/ext/hindsight_memory_service/memory/{memory_id}", "DELETE"),
        ]
        for path, method in expected:
            matches = [e for e in data["http_endpoints"] if e["path"] == path and e["method"] == method]
            assert matches, f"missing endpoint {method} {path}"
            assert matches[0]["auth"] == "user", f"auth must be user: {path}"
            assert matches[0]["handler_capability"] == "http.handle"
            assert int(matches[0]["timeout_ms"]) > 0

    def test_manifest_granted_tool_executor(self) -> None:
        """memory 域懒注入需要 tool-executor 能力授权。"""
        data = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        assert "tool-executor" in data.get("granted_capabilities", [])