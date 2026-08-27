# @feature: memory 域路由 documents 通路 | @ci: python-coverage
"""memory 列表/统计端点真实链路测试（空查询链路断裂的回归判据）。

真实链路（不走内核；tool-executor 以「直调 sidecar 工具 handler」替身，
仅外部边界 hindsight HTTP 客户端用内存假件替代）：

    routes_memory.list_* / get_memory_stats
      → HindsightBackend.get_documents（documents/list 通路）
      → tool-executor 替身直调 sidecar hindsight.retain / hindsight.get_documents
        handler（真实代码，含 retain 的 type:* 标签注入）
      → 假 hindsight client（内存 bank，list_documents 按 tags any_strict 过滤 /
        get_document 定向取回）

历史缺陷：列表面走 backend.search(query="")——sidecar recall 对空 query
必拒（返回 error 签名），HindsightBackend.search 对 error 签名诚实上抛
RuntimeError → http.handle 兜底 500。本文件断言修复后的不变量：
列表/统计经 documents 通路不依赖 query、空库不报错、计数与落库一致、
能力故障诚实传播不吞。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

pytestmark = pytest.mark.unit


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeDocumentError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class FakeDocumentsAPI:
    """内存 documents API：实现 list_documents/get_document 的服务端契约。"""

    def __init__(self, banks: dict[str, dict[str, dict[str, Any]]]) -> None:
        self._banks = banks

    async def list_documents(
        self,
        *,
        bank_id: str,
        limit: int,
        tags: list[str] | None = None,
        tags_match: str | None = None,
        q: str | None = None,
    ) -> Any:
        docs = list(self._banks.get(bank_id, {}).values())
        if q:
            docs = [d for d in docs if q.lower() in str(d.get("id", "")).lower()]
        if tags:
            match = (tags_match or "any").lower()
            if match == "any_strict":
                # OR 且排除无 tag 文档（hindsight 服务端 any_strict 语义）
                docs = [
                    d for d in docs
                    if d.get("tags") and set(tags) & set(d["tags"])
                ]
            else:
                docs = [
                    d for d in docs
                    if d.get("tags") and set(tags) & set(d["tags"])
                ]
        return SimpleNamespace(items=docs[: max(1, limit)])

    async def get_document(self, *, bank_id: str, document_id: str) -> dict[str, Any]:
        doc = self._banks.get(bank_id, {}).get(document_id)
        if doc is None:
            raise FakeDocumentError(404, f"document {document_id} not found")
        return dict(doc)


class FakeHindsightClient:
    """内存 hindsight 客户端：aretain 落库 + documents 面读回。"""

    def __init__(self) -> None:
        self.banks: dict[str, dict[str, dict[str, Any]]] = {}
        self.documents = FakeDocumentsAPI(self.banks)

    async def aretain(self, **kwargs: Any) -> Any:
        bank = kwargs.get("bank_id", "")
        doc_id = kwargs.get("document_id") or f"auto-{len(self.banks.get(bank, {})) + 1}"
        self.banks.setdefault(bank, {})[doc_id] = {
            "id": doc_id,
            "original_text": kwargs.get("content", ""),
            "tags": [str(t) for t in (kwargs.get("tags") or [])],
            "document_metadata": dict(kwargs.get("metadata") or {}),
            "created_at": "2026-08-27T00:00:00Z",
        }
        return SimpleNamespace(success=True, operation_id=None)


def _call_tool(module: Any, tool_name: str, **kwargs: Any) -> Any:
    result = module.plugin._tools[tool_name].handler(**kwargs)
    return _run(result)


@pytest.fixture()
def stack(monkeypatch: pytest.MonkeyPatch) -> Any:
    """装载真实 sidecar 模块 + 真实 routes 模块，经 HindsightBackend 直连。"""
    hmod = _load_module(_PLUGIN_DIR / "server.py", "hindsight_chain_sidecar")
    fake_client = FakeHindsightClient()
    hmod._client = fake_client
    monkeypatch.setattr(hmod, "_DEFAULT_BANK_ID", "default")

    async def caller(method: str, params: dict[str, Any]) -> Any:
        assert method == "tool-executor.invoke", method
        result = hmod.plugin._tools[params["tool_name"]].handler(**params["args"])
        if asyncio.iscoroutine(result):
            result = await result
        return result

    from memory_backend import HindsightBackend

    rmod = _load_module(_PLUGIN_DIR / "routes_memory.py", "hindsight_chain_routes")
    rmod.set_memory_backend(HindsightBackend(caller))
    return {"hindsight": hmod, "routes": rmod, "client": fake_client}


def _seed(stack: Any, doc_id: str, mtype: str, text: str, tags: list[str]) -> None:
    """经真实 retain handler 落一条记忆（type:* 标签注入走生产同款代码）。"""
    _call_tool(
        stack["hindsight"],
        "hindsight.retain",
        bank_id="default",
        content=text,
        memory_type=mtype,
        document_id=doc_id,
        metadata={"tags": tags},
    )


class TestListingRealChain:
    def test_list_memories_maps_retained_documents(self, stack: Any) -> None:
        """retain 写入的两类记忆经 documents 通路原样列出（type:* 内部标签剔除）。"""
        _seed(stack, "ep-1", "episode", "登录失败三次被锁定", ["auth"])
        _seed(stack, "se-1", "semantic", "用户偏好深色主题", [])
        _seed(stack, "se-2", "semantic", "部署窗口为周日凌晨", [])

        listing = _run(stack["routes"].list_memories(limit=20))

        assert listing["total"] == 3
        by_id = {item["id"]: item for item in listing["items"]}
        assert set(by_id) == {"ep-1", "se-1", "se-2"}
        ep = by_id["ep-1"]
        assert ep["content"] == "登录失败三次被锁定"
        assert ep["memory_type"] == "episode"
        assert ep["tags"] == ["auth"]
        for item in listing["items"]:
            assert item["score"] == 0.0

    def test_type_filter_only_returns_matching_type(self, stack: Any) -> None:
        """memory_type=episode 只命中 episode（filtered ⊆ unfiltered 性质）。"""
        _seed(stack, "ep-1", "episode", "情景A", [])
        _seed(stack, "se-1", "semantic", "语义B", [])

        episodes = _run(stack["routes"].list_memories(memory_type="episode", limit=20))
        everything = _run(stack["routes"].list_memories(limit=20))

        assert [i["id"] for i in episodes["items"]] == ["ep-1"]
        assert len(everything["items"]) == 2
        assert {i["id"] for i in episodes["items"]} <= {
            i["id"] for i in everything["items"]
        }

    def test_empty_bank_lists_and_stats_without_error(self, stack: Any) -> None:
        """空 bank：列表/统计全走真链路也不得抛错（历史缺陷在此 500）。"""
        listing = _run(stack["routes"].list_memories(limit=20))
        episodes = _run(stack["routes"].list_episodes(page=1, page_size=20))
        semantic = _run(stack["routes"].list_semantic())
        stats = _run(stack["routes"].get_memory_stats())

        assert listing == {"items": [], "total": 0}
        assert episodes["items"] == [] and episodes["page_size"] == 20
        assert semantic == {"items": [], "total": 0}
        assert stats["total_count"] == 0 and stats["episode_count"] == 0

    def test_stats_counts_match_bank_contents(self, stack: Any) -> None:
        """统计计数与落库构成逐项一致。"""
        _seed(stack, "ep-1", "episode", "e1", [])
        _seed(stack, "se-1", "semantic", "s1", [])
        _seed(stack, "se-2", "semantic", "s2", [])

        stats = _run(stack["routes"].get_memory_stats())

        assert stats == {
            "episode_count": 1,
            "knowledge_count": 2,
            "total_count": 3,
            "last_updated": "",
        }

    def test_capability_outage_propagates_fail_closed(self, stack: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """后端能力故障 → 诚实抛错（不得伪装成空列表假成功）。"""

        async def broken_caller(method: str, params: dict[str, Any]) -> Any:
            raise RuntimeError("sidecar down")

        from memory_backend import HindsightBackend

        monkeypatch.setattr(
            stack["routes"], "_memory_backend", HindsightBackend(broken_caller)
        )
        with pytest.raises(RuntimeError, match="sidecar down"):
            _run(stack["routes"].list_memories(limit=10))
        with pytest.raises(RuntimeError, match="sidecar down"):
            _run(stack["routes"].get_memory_stats())

    def test_episodes_surface_reads_original_text(self, stack: Any) -> None:
        """episodes 列表内容源自文档原文（intent_text ← original_text）。"""
        _seed(stack, "ep-1", "episode", "重试任务 id=42 成功", ["retry"])

        result = _run(stack["routes"].list_episodes(page=2, page_size=10))

        assert result["page"] == 2
        assert result["total"] == 1
        assert result["items"][0]["id"] == "ep-1"
        assert result["items"][0]["intent_text"] == "重试任务 id=42 成功"
        assert result["items"][0]["tags"] == ["retry"]

    def test_semantic_surface_shape_matches_frontend_contract(self, stack: Any) -> None:
        """semantic 列表键面前端契约冻结：id/content/source_type/extra_data/created_at。"""
        _seed(stack, "se-1", "semantic", "知识条目正文", [])

        result = _run(stack["routes"].list_semantic())

        assert result["items"][0] == {
            "id": "se-1",
            "content": "知识条目正文",
            "source_type": "memory_backend",
            "extra_data": {},
            "created_at": "2026-08-27T00:00:00Z",
        }
