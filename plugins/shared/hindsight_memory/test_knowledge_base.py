# @feature: knowledge-base 域路由 | @ci: none-local
"""知识库（knowledge-base 域）测试——bank 唯一存储契约（终版拍板 2026-10-15）。

机制：
- 上传=切块→aretain 逐块写记忆 bank（kb bank=唯一存储，无目录/无副本/无对账）；
  中途失败如实 500 并尽力清理已写 chunk；
- 列表/详情/统计/标签从 bank documents 面派生（document_metadata.kb_* 聚合）；
- 删除=按 type:knowledge+kb_item:{id} 标签删 bank（主路径，定位 0 文档=404，
  删失败如实 500）；
- 检索=arecall 原文 chunks，chunk_id=document_id 逐条取归属上下文；
- 零自持真源点：kb_meta / 源文件落盘 / set_data_dir 全无（防复活机械闸）。

mock 仅限外部依赖（hindsight sidecar documents/retain/recall 面，_FakeBank 对齐
真实语义：list 分页+tags 过滤、get 404、delete 移除）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# X10-F1 身份头契约：kb 域按调用方租户分 bank（缺身份头 fail-closed 空集）。
# 存量 bank "kb" 即 default 租户 bank——本文件全部用例以 default 身份驱动，
# 既有 bank/tag 断言语义不变。
_HEADERS = {"x-agentos-tenant": "default", "x-agentos-user": "admin"}


def _load_module() -> Any:
    """动态加载 knowledge_base.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_knowledge_base_test"
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "knowledge_base.py")
    assert spec is not None, "Cannot load knowledge_base.py"
    assert spec.loader is not None, "Cannot load knowledge_base.py"
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


class _FakeBank:
    """hindsight documents 面替身（外部依赖）：内存文档表 + 真实语义（list 分页
    with tags any_strict 过滤 / get 404 / delete 移除）。"""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def add(self, doc: dict[str, Any]) -> None:
        self.docs[doc["id"]] = doc

    def _filtered(self, tags: list[str] | None, tags_match: str = "any_strict") -> list[dict[str, Any]]:
        docs = list(self.docs.values())
        if tags:
            if tags_match == "all":
                docs = [d for d in docs if set(tags) <= set(d.get("tags") or [])]
            else:  # any_strict：任一命中（无 tag 文档排除）
                docs = [d for d in docs if set(tags) & set(d.get("tags") or [])]
        return docs

    async def list_documents(self, bank_id: str = "", tags: list[str] | None = None,
                             tags_match: str = "any_strict", limit: int = 100,
                             offset: int = 0) -> Any:
        docs = self._filtered(tags, tags_match)
        page = docs[offset : offset + limit]
        items = [SimpleNamespace(model_dump=lambda d=d: dict(d)) for d in page]
        return SimpleNamespace(items=items, total=len(docs))

    async def get_document(self, bank_id: str = "", document_id: str = "") -> Any:
        doc = self.docs.get(document_id)
        if doc is None:
            err = RuntimeError("not found")
            err.status = 404  # type: ignore[attr-defined]
            raise err
        return SimpleNamespace(model_dump=lambda: dict(doc))

    async def delete_document(self, bank_id: str = "", document_id: str = "") -> None:
        self.docs.pop(document_id, None)


def _bank_doc(
    doc_id: str,
    kb_id: str,
    kb_name: str = "doc.md",
    created_at: str = "2026-10-15T00:00:00+00:00",
    chunk_total: str = "1",
    extra_tags: list[str] | None = None,
) -> dict[str, Any]:
    """bank 文档形态（DocumentResponse 关键字段；document_metadata=aretain metadata）。"""
    return {
        "id": doc_id,
        "bank_id": "kb",
        "original_text": f"chunk-{doc_id}",
        "created_at": created_at,
        "updated_at": created_at,
        "tags": ["type:knowledge", f"kb_item:{kb_id}", *(extra_tags or [])],
        "document_metadata": {
            "kb_id": kb_id,
            "kb_name": kb_name,
            "kb_size": "120",
            "kb_mime": "text/markdown",
            "kb_chunk_total": chunk_total,
            "source": "knowledge_base",
            "created_at": created_at,
            "memory_type": "semantic",
        },
    }


def _make_client(bank: _FakeBank | None = None) -> tuple[MagicMock, _FakeBank]:
    bank = bank if bank is not None else _FakeBank()
    client = MagicMock()
    client.documents = bank
    client.aretain = AsyncMock(side_effect=lambda **kw: MagicMock(
        operation_id=f"c{kw['metadata']['kb_chunk_index']}", accepted=True))
    client.acreate_bank = AsyncMock(return_value=None)
    client.arecall = AsyncMock(return_value=SimpleNamespace(chunks=[], results=[]))
    return client, bank


@pytest.fixture
def kb() -> Any:
    module = _load_module()
    module.set_client(None)
    return module


@pytest.fixture
def client(kb: Any) -> MagicMock:
    client, _bank = _make_client()
    kb.set_client(client)
    return client


# ═══════════════════════════════════════════════════════════
# 零自持真源点（机械闸：自持面不得复活）
# ═══════════════════════════════════════════════════════════


class TestNoLocalTruth:
    def test_no_self_held_truth_surface(self, kb: Any) -> None:
        """kb_meta 读写 / 数据根覆盖 / uploads 解析 / 分类登记全无——bank 是唯一存储。"""
        for banned in ("_load_meta", "_save_meta", "set_data_dir",
                       "_resolve_kb_uploads_dir", "create_category", "list_categories"):
            assert not hasattr(kb, banned), f"自持面复活: {banned}"


# ═══════════════════════════════════════════════════════════
# 上传（bank 唯一写入面）
# ═══════════════════════════════════════════════════════════


class TestUpload:
    def test_upload_retains_chunks_with_item_metadata(self, kb: Any, client: MagicMock) -> None:
        """切块→aretain 逐块入 bank：tags 隔离（type:knowledge+kb_item+filetype）、
        metadata 带聚合面（kb_id/kb_name/kb_size/kb_mime/kb_chunk_total）。"""
        content = ("知识库内容。" * 800).encode("utf-8")  # > 2000 字符 → 多块
        result = _run(kb.upload_document("guide.md", content, "text/markdown", headers=_HEADERS))

        assert result["chunks_imported"] > 1
        assert result["message"] == "文件上传成功"
        assert result["size"] == len(content)
        assert client.acreate_bank.call_args.kwargs["bank_id"] == "kb"
        assert client.aretain.call_count == result["chunks_imported"]

        first = client.aretain.call_args_list[0].kwargs
        assert first["bank_id"] == "kb"
        assert first["tags"] == ["type:knowledge", f"kb_item:{result['item_id']}", "filetype:md"]
        meta = first["metadata"]
        assert meta["kb_id"] == result["item_id"]
        assert meta["kb_name"] == "guide.md"
        assert meta["kb_size"] == str(len(content))
        assert meta["kb_mime"] == "text/markdown"
        assert meta["kb_chunk_total"] == str(result["chunks_imported"])
        assert meta["source"] == "knowledge_base"
        assert meta["created_at"]

    def test_upload_fail_midway_reports_and_cleans_written_chunks(self, kb: Any) -> None:
        """中途失败：上传如实 500（唯一存储无兜底），已写 chunk 尽力清理不留半成品。"""
        client, bank = _make_client()
        real_aretain = client.aretain.side_effect

        async def flaky(**kw: Any) -> Any:
            if kw["metadata"]["kb_chunk_index"] == "2":
                raise RuntimeError("vector api down")
            return await real_aretain(**kw)  # type: ignore[misc]

        client.aretain = AsyncMock(side_effect=flaky)
        kb.set_client(client)

        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("guide.md", ("字" * 5000).encode(), "text/markdown", headers=_HEADERS))
        assert ei.value.status_code == 500
        assert ei.value.error_code == "KNB_ING_8001"
        # 已成功的 c0/c1 被清理（bank 回到无本条目状态）
        assert bank.docs == {}, f"残留 chunk: {list(bank.docs)}"

    def test_upload_fail_first_chunk_no_cleanup_calls(self, kb: Any) -> None:
        """首块即失败：无已写 chunk，不触发清理面。"""
        client, bank = _make_client()
        client.aretain = AsyncMock(side_effect=RuntimeError("down"))
        kb.set_client(client)
        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("a.md", "正文".encode(), "text/markdown", headers=_HEADERS))
        assert ei.value.status_code == 500
        assert not bank.docs

    def test_upload_without_client_fails_honestly(self, kb: Any) -> None:
        """sidecar 缺席：唯一存储不可达 = 上传如实失败（503，无兜底）。"""
        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("a.md", b"content", headers=_HEADERS))
        assert ei.value.status_code == 503
        assert "KNB_INIT" in ei.value.error_code

    def test_upload_rejections_touch_no_bank(self, kb: Any, client: MagicMock) -> None:
        """校验拒绝（扩展名/空/纯空白/超大）→ 400 且不触碰 bank。"""
        cases = [("secret.pdf", b"data"), ("a.txt", b""), ("b.txt", b"  \n "),
                 ("big.md", b"x" * (kb._KB_MAX_FILE_BYTES + 1))]
        for filename, payload in cases:
            with pytest.raises(kb.KBError) as ei:
                _run(kb.upload_document(filename, payload, headers=_HEADERS))
            assert ei.value.status_code == 400
        client.aretain.assert_not_called()


# ═══════════════════════════════════════════════════════════
# 列表 / 详情 / 统计 / 标签（bank documents 面聚合派生）
# ═══════════════════════════════════════════════════════════


class TestListFromBank:
    def test_list_aggregates_items_by_kb_id(self, kb: Any, client: MagicMock) -> None:
        """多条目×多 chunk 按 kb_id 聚合；按创建时间倒序；形态与旧契约对齐
        （categories 键保留恒空）。"""
        _, bank = _make_client()
        client.documents = bank
        bank.add(_bank_doc("c1", "item-a", kb_name="a.md", created_at="2026-10-14T00:00:00+00:00", chunk_total="2"))
        bank.add(_bank_doc("c2", "item-a", created_at="2026-10-14T00:00:00+00:00", chunk_total="2"))
        bank.add(_bank_doc("c3", "item-b", kb_name="b.md", created_at="2026-10-15T00:00:00+00:00"))

        items = _run(kb.list_items(headers=_HEADERS))

        assert [it["id"] for it in items] == ["item-b", "item-a"]
        first = items[0]
        assert set(first) == {"id", "name", "size", "mime_type", "categories",
                              "tags", "chunk_count", "created_at", "updated_at"}
        assert first["name"] == "b.md"
        assert first["categories"] == []
        second = items[1]
        assert second["chunk_count"] == 2
        assert second["size"] == 120
        assert second["mime_type"] == "text/markdown"

    def test_list_paginates_through_all_documents(self, kb: Any, client: MagicMock,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        """分页拉全：单页 limit 不足时以 offset 续拉（聚合不受页界切割影响）。"""
        _, bank = _make_client()
        client.documents = bank
        for i in range(3):
            bank.add(_bank_doc(f"c{i}", f"item-{i}", kb_name=f"{i}.md"))
        monkeypatch.setattr(kb, "_KB_LIST_PAGE_SIZE", 2)

        items = _run(kb.list_items(headers=_HEADERS))

        assert {it["id"] for it in items} == {"item-0", "item-1", "item-2"}

    def test_list_degrades_empty_without_client(self, kb: Any) -> None:
        """读面降级先例：client 缺席 → 空列表（check 如实报告后端不可用）。"""
        assert _run(kb.list_items(headers=_HEADERS)) == []
        stats = _run(kb.get_stats(headers=_HEADERS))
        assert stats == {"total": 0, "categories_count": 0, "tags_count": 0,
                         "total_chunks": 0, "total_size": 0}

    def test_get_item_found_and_not_found(self, kb: Any, client: MagicMock) -> None:
        _, bank = _make_client()
        client.documents = bank
        bank.add(_bank_doc("c1", "item-a", kb_name="a.md"))
        item = _run(kb.get_item("item-a", headers=_HEADERS))
        assert item["id"] == "item-a"
        assert "content" not in item
        assert "original_text" not in item
        with pytest.raises(kb.KBError) as ei:
            _run(kb.get_item("nope", headers=_HEADERS))
        assert ei.value.status_code == 404

    def test_list_tags_from_item_tags(self, kb: Any, client: MagicMock) -> None:
        """标签=条目 tags 去重排序（内部前缀 type:/kb_item: 不外泄）。"""
        _, bank = _make_client()
        client.documents = bank
        bank.add(_bank_doc("c1", "item-a", extra_tags=["filetype:md", "beta"]))
        bank.add(_bank_doc("c2", "item-b", kb_name="b.md", extra_tags=["filetype:txt"]))
        assert _run(kb.list_tags(headers=_HEADERS)) == ["beta", "filetype:md", "filetype:txt"]

    def test_stats_aggregates_from_bank(self, kb: Any, client: MagicMock) -> None:
        _, bank = _make_client()
        client.documents = bank
        bank.add(_bank_doc("c1", "item-a", extra_tags=["filetype:md"], chunk_total="2"))
        bank.add(_bank_doc("c2", "item-a", chunk_total="2", extra_tags=["filetype:md"]))
        stats = _run(kb.get_stats(headers=_HEADERS))
        assert stats["total"] == 1
        assert stats["total_chunks"] == 2
        assert stats["total_size"] == 120
        assert stats["tags_count"] == 1


# ═══════════════════════════════════════════════════════════
# 删除（按标签删 bank = 主路径）
# ═══════════════════════════════════════════════════════════


class TestDelete:
    def test_delete_removes_all_item_documents(self, kb: Any, client: MagicMock) -> None:
        """按 type:knowledge+kb_item 标签定位并逐文档删除（主路径，非尽力而为）。"""
        _, bank = _make_client()
        client.documents = bank
        bank.add(_bank_doc("c1", "item-a"))
        bank.add(_bank_doc("c2", "item-a"))
        bank.add(_bank_doc("c9", "item-b", kb_name="b.md"))

        result = _run(kb.delete_item("item-a", headers=_HEADERS))

        assert result == {"message": "条目已删除", "id": "item-a"}
        assert "c1" not in bank.docs
        assert "c2" not in bank.docs
        assert "c9" in bank.docs, "未定位条目不得误删"
        with pytest.raises(kb.KBError):
            _run(kb.get_item("item-a", headers=_HEADERS))

    def test_delete_unknown_404(self, kb: Any, client: MagicMock) -> None:
        """定位 0 文档=条目不存在（404，不再有账本幽灵态）。"""
        with pytest.raises(kb.KBError) as ei:
            _run(kb.delete_item("nope", headers=_HEADERS))
        assert ei.value.status_code == 404

    def test_delete_bank_failure_reports_honestly(self, kb: Any) -> None:
        """删除中途 bank 故障：如实 500（已删的不回，重试幂等）。"""
        client, bank = _make_client()
        bank.add(_bank_doc("c1", "item-a"))
        bank.add(_bank_doc("c2", "item-a"))
        real_delete = bank.delete_document

        async def flaky(bank_id: str = "", document_id: str = "") -> None:
            if document_id == "c2":
                raise RuntimeError("delete down")
            await real_delete(bank_id=bank_id, document_id=document_id)

        client.documents.delete_document = AsyncMock(side_effect=flaky)
        kb.set_client(client)

        with pytest.raises(kb.KBError) as ei:
            _run(kb.delete_item("item-a", headers=_HEADERS))
        assert ei.value.status_code == 500
        assert list(bank.docs) == ["c2"]

    def test_delete_without_client_fails_honestly(self, kb: Any) -> None:
        with pytest.raises(kb.KBError) as ei:
            _run(kb.delete_item("nope", headers=_HEADERS))
        assert ei.value.status_code == 503


# ═══════════════════════════════════════════════════════════
# 检索（原文 chunks + 归属上下文解析）
# ═══════════════════════════════════════════════════════════


class TestSearch:
    def test_search_returns_chunks_with_item_context(self, kb: Any, client: MagicMock) -> None:
        """原文 chunk 回显；chunk_id=document_id 解析归属（item/name/tags）。"""
        _, bank = _make_client()
        client.documents = bank
        bank.add(_bank_doc("c1", "item-a", kb_name="a.md", extra_tags=["filetype:md"]))
        resp = SimpleNamespace(
            chunks=[SimpleNamespace(model_dump=lambda: {"id": "c1", "text": "分块一", "score": 0.9})],
            results=[],
        )
        client.arecall = AsyncMock(return_value=resp)

        result = _run(kb.search("架构", headers=_HEADERS))

        call_kwargs = client.arecall.call_args.kwargs
        assert call_kwargs["bank_id"] == "kb"
        assert call_kwargs["include_chunks"] is True
        assert call_kwargs["tags"] == ["type:knowledge"]
        assert result["total"] == 1
        first = result["results"][0]
        assert first["item_id"] == "item-a"
        assert first["name"] == "a.md"
        assert first["content"] == "分块一"
        assert first["categories"] == []
        assert first["tags"] == ["filetype:md"]

    def test_search_attribution_failure_degrades_context_only(self, kb: Any, client: MagicMock) -> None:
        """归属解析失败：内容照回显，上下文字段留空（不炸不吞内容）。"""
        resp = SimpleNamespace(
            chunks=[SimpleNamespace(model_dump=lambda: {"id": "ghost", "text": "孤儿", "score": 0.7})],
            results=[],
        )
        client.arecall = AsyncMock(return_value=resp)

        result = _run(kb.search("q", headers=_HEADERS))

        assert result["total"] == 1
        first = result["results"][0]
        assert first["content"] == "孤儿"
        assert first["item_id"] == ""
        assert first["name"] == ""

    def test_search_results_fallback_uses_result_metadata(self, kb: Any, client: MagicMock) -> None:
        """无 chunks 时用 results 事实兜底（自带 document_id/metadata/tags）。"""
        resp = SimpleNamespace(
            chunks=[],
            results=[SimpleNamespace(model_dump=lambda: {
                "id": "f1", "text": "事实文本", "document_id": "c1",
                "metadata": {"kb_id": "item-a", "kb_name": "a.md"},
                "tags": ["filetype:md"], "score": 0.6,
            })],
        )
        client.arecall = AsyncMock(return_value=resp)

        result = _run(kb.search("q", headers=_HEADERS))

        first = result["results"][0]
        assert first["content"] == "事实文本"
        assert first["item_id"] == "item-a"

    def test_search_empty_query_400(self, kb: Any, client: MagicMock) -> None:
        with pytest.raises(kb.KBError) as ei:
            _run(kb.search("   ", headers=_HEADERS))
        assert ei.value.status_code == 400

    def test_search_recall_error_degrades(self, kb: Any, client: MagicMock) -> None:
        client.arecall = AsyncMock(side_effect=RuntimeError("recall down"))
        result = _run(kb.search("q", headers=_HEADERS))
        assert result == {"results": [], "total": 0, "error": "recall down"}

    def test_search_without_client_degrades(self, kb: Any) -> None:
        result = _run(kb.search("q", headers=_HEADERS))
        assert result["results"] == []
        assert result["total"] == 0
        assert result["error"]

    def test_search_top_k_clamped(self, kb: Any, client: MagicMock) -> None:
        _run(kb.search("q", top_k=9999, headers=_HEADERS))
        assert client.arecall.call_args.kwargs["query"] == "q"


# ═══════════════════════════════════════════════════════════
# check（检索面可用性）
# ═══════════════════════════════════════════════════════════


class TestCheck:
    def test_check_reports_unavailable_without_client(self, kb: Any) -> None:
        result = _run(kb.check_available(headers=_HEADERS))
        assert result["available"] is False
        assert "未初始化" in result["message"]

    def test_check_reports_available_with_client(self, kb: Any, client: MagicMock) -> None:
        result = _run(kb.check_available(headers=_HEADERS))
        assert result["available"] is True
        assert result["backend"] == "hindsight"
        assert result["bank"] == "kb"

    def test_check_reports_unavailable_on_bank_error(self, kb: Any, client: MagicMock) -> None:
        client.acreate_bank = AsyncMock(side_effect=RuntimeError("pg0 down"))
        result = _run(kb.check_available(headers=_HEADERS))
        assert result["available"] is False


# ═══════════════════════════════════════════════════════════
# manifest：knowledge-base 端点声明（categories 随目录删除）
# ═══════════════════════════════════════════════════════════


class TestManifestKbEndpoints:
    def test_manifest_declares_knowledge_base_endpoints(self) -> None:
        """8 个知识库端点全部声明（auth=user，前缀 hindsight_memory_service）；
        categories 端点不得复活。"""
        data = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        expected = [
            ("/ext/hindsight_memory_service/knowledge-base", "GET"),
            ("/ext/hindsight_memory_service/knowledge-base/stats", "GET"),
            ("/ext/hindsight_memory_service/knowledge-base/upload", "POST"),
            ("/ext/hindsight_memory_service/knowledge-base/check", "GET"),
            ("/ext/hindsight_memory_service/knowledge-base/tags", "GET"),
            ("/ext/hindsight_memory_service/knowledge-base/search", "GET"),
            ("/ext/hindsight_memory_service/knowledge-base/{item_id}", "GET"),
            ("/ext/hindsight_memory_service/knowledge-base/{item_id}", "DELETE"),
        ]
        declared = {(e["path"], e["method"]) for e in data["http_endpoints"]}
        for path, method in expected:
            assert (path, method) in declared, f"missing endpoint {method} {path}"
            matches = [e for e in data["http_endpoints"] if e["path"] == path and e["method"] == method]
            assert matches[0]["auth"] == "user", f"auth must be user: {path}"
            assert matches[0]["handler_capability"] == "http.handle"
        for path, _method in declared:
            assert "categories" not in path, "categories 端点已随目录删除，不得复活"
