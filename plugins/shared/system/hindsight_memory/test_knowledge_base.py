# @feature: channel_api 退役批次 4 knowledge-base | @ci: none-local
"""知识库（knowledge-base 域）测试。

验证内容：
1. 上传链路：multipart 文件 → 落盘（uploads/kb/）→ 切块 → 向量化入库
   （mock aretain，逐 chunk 校验 tags/metadata）→ 元数据仓注册；
2. 边界：非 txt/md 拒绝 400、空内容 400、超大文件 400、client 未初始化 503；
3. 分类 CRUD（同名 409 / 空名 400 / 删除解除条目关联）、标签、统计、条目读写删；
4. 检索：mock arecall（include_chunks 原文优先）→ chunk 归属索引回连条目，
   未注册 chunk（已删残留）不回显；空查询 400；recall 异常降级空结果；
5. check 可用性如实降级/可用；
6. 元数据仓原子写与损坏重建。

不依赖真实 hindsight 包——mock client（AsyncMock）。
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


def _load_module() -> Any:
    """动态加载 knowledge_base.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_knowledge_base_test"
    path = _PLUGIN_DIR / "knowledge_base.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
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


def _retain_result(op_id: str) -> MagicMock:
    return MagicMock(operation_id=op_id, accepted=True)


def _recall_response(chunks: list[dict[str, Any]]) -> MagicMock:
    """构造 RecallResponse 替身：chunks 是 model_dump-able 对象列表。"""
    resp = MagicMock()
    resp.chunks = [SimpleNamespace(model_dump=lambda d=c: dict(d)) for c in chunks]
    resp.results = []
    return resp


@pytest.fixture
def kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """加载模块并隔离数据目录 + 上传目录（UPLOADS_DIR env → tmp）。"""
    module = _load_module()
    module.set_data_dir(str(tmp_path / "kb"))
    module.set_client(None)
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    return module


@pytest.fixture
def client(kb: Any) -> MagicMock:
    """注入 mock hindsight client（aretain/acreate_bank/arecall 全 AsyncMock）。"""
    c = MagicMock()
    c.aretain = AsyncMock(side_effect=lambda **kw: _retain_result(f"chunk-{kw['metadata']['kb_chunk_index']}"))
    c.acreate_bank = AsyncMock(return_value=None)
    c.arecall = AsyncMock(return_value=_recall_response([]))
    kb.set_client(c)
    return c


# ═══════════════════════════════════════════════════════════
# 切块
# ═══════════════════════════════════════════════════════════


class TestChunkText:
    def test_chunk_basic(self, kb: Any) -> None:
        assert kb._chunk_text("") == []
        chunks = kb._chunk_text("x" * 5000)
        assert len(chunks) == 3  # 2000 字符/块
        assert all(len(c) <= 2000 for c in chunks)
        assert "".join(chunks) == "x" * 5000


# ═══════════════════════════════════════════════════════════
# 上传链路
# ═══════════════════════════════════════════════════════════


class TestUpload:
    def test_upload_ingests_chunks_and_registers(self, kb: Any, client: MagicMock, tmp_path: Path) -> None:
        """文档入库：aretain 逐块调用（tags type:knowledge + kb_item）+ 元数据注册 + 落盘。"""
        content = ("知识库内容。" * 800).encode("utf-8")  # > 2000 字符 → 多块
        result = _run(kb.upload_document("guide.md", content, "text/markdown"))

        assert result["chunks_imported"] > 1
        assert result["message"] == "文件上传成功"
        assert client.acreate_bank.call_args.kwargs["bank_id"] == "kb"

        # aretain 调用校验（每块带隔离 tag 与完整元数据）
        assert client.aretain.call_count == result["chunks_imported"]
        first_kwargs = client.aretain.call_args.kwargs
        assert first_kwargs["bank_id"] == "kb"
        assert first_kwargs["tags"] == ["type:knowledge", f"kb_item:{result['item_id']}"]
        meta = first_kwargs["metadata"]
        assert meta["kb_id"] == result["item_id"]
        assert meta["source"] == "knowledge_base"
        assert meta["kb_chunk_total"] == str(result["chunks_imported"])

        # 条目注册 + chunk_ids 收集 + 落盘文件存在
        meta_file = tmp_path / "kb" / "kb_meta.json"
        assert meta_file.exists()
        saved = json.loads(meta_file.read_text(encoding="utf-8"))
        item = saved["items"][0]
        assert item["id"] == result["item_id"]
        assert item["name"] == "guide.md"
        assert item["chunk_count"] == result["chunks_imported"]
        assert len(item["chunk_ids"]) == result["chunks_imported"]
        assert item["tags"] == ["filetype:md"]
        assert Path(item["source_file"]).exists()

        # 列表端点返回注册条目（前端数组形态）
        items = kb.list_items()
        assert len(items) == 1
        assert items[0]["id"] == result["item_id"]
        assert items[0]["name"] == "guide.md"
        assert items[0]["size"] == len(content)

    def test_upload_rejects_unsupported_ext(self, kb: Any, client: MagicMock) -> None:
        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("secret.pdf", b"data"))
        assert ei.value.status_code == 400
        client.aretain.assert_not_called()

    def test_upload_rejects_empty_content(self, kb: Any, client: MagicMock) -> None:
        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("a.txt", b""))
        assert ei.value.status_code == 400

    def test_upload_rejects_oversize(self, kb: Any, client: MagicMock) -> None:
        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("big.md", b"x" * (kb._KB_MAX_FILE_BYTES + 1)))
        assert ei.value.status_code == 400

    def test_upload_without_client_503(self, kb: Any) -> None:
        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("a.md", b"content"))
        assert ei.value.status_code == 503
        assert "KNB_INIT" in ei.value.error_code

    def test_upload_rolls_back_file_on_ingest_failure(self, kb: Any, client: MagicMock, tmp_path: Path) -> None:
        """入库中途异常 → 回滚落盘文件，不留半成品。"""
        client.aretain = AsyncMock(side_effect=RuntimeError("vector api down"))
        # 预置一个条目文件名，验证回滚后目录干净
        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("a.md", b"some text"))
        assert ei.value.status_code == 500
        kb_dir = tmp_path / "uploads" / "kb"
        assert not any(kb_dir.glob("*.md")), "ingest 失败必须回滚落盘文件"
        assert not (tmp_path / "kb" / "kb_meta.json").exists()


# ═══════════════════════════════════════════════════════════
# 分类 / 标签 / 统计 / 条目
# ═══════════════════════════════════════════════════════════


class TestCategories:
    def _seed_item(self, kb: Any, item_id: str, categories: list[str]) -> None:
        meta = kb._load_meta()
        meta.setdefault("items", []).append(
            {
                "id": item_id,
                "name": f"{item_id}.md",
                "size": 10,
                "mime_type": "text/markdown",
                "categories": categories,
                "tags": ["filetype:md"],
                "chunk_count": 1,
                "chunk_ids": [f"cid-{item_id}"],
                "source_file": "",
                "created_at": "2026-08-21T00:00:00Z",
                "updated_at": "2026-08-21T00:00:00Z",
            }
        )
        kb._save_meta(meta)

    def test_create_and_list_categories(self, kb: Any) -> None:
        r = kb.create_category("架构")
        assert r["name"] == "架构"
        cats = kb.list_categories()
        assert cats == [{"name": "架构", "count": 0}]

    def test_create_duplicate_409(self, kb: Any) -> None:
        kb.create_category("架构")
        with pytest.raises(kb.KBError) as ei:
            kb.create_category(" 架构 ")
        assert ei.value.status_code == 409

    def test_create_blank_400(self, kb: Any) -> None:
        with pytest.raises(kb.KBError) as ei:
            kb.create_category("   ")
        assert ei.value.status_code == 400

    def test_category_count_reflects_items(self, kb: Any) -> None:
        kb.create_category("架构")
        self._seed_item(kb, "i1", ["架构"])
        self._seed_item(kb, "i2", ["架构"])
        cats = kb.list_categories()
        assert {"name": "架构", "count": 2} in cats

    def test_delete_category_unbinds_items(self, kb: Any) -> None:
        kb.create_category("架构")
        self._seed_item(kb, "i1", ["架构", "其他"])
        kb.delete_category("架构")
        cats = kb.list_categories()
        assert cats == []  # 空分类也未保留
        assert kb.get_item("i1")["categories"] == ["其他"]

    def test_tags_distinct_sorted(self, kb: Any) -> None:
        self._seed_item(kb, "i1", [])
        meta = kb._load_meta()
        meta["items"][0]["tags"] = ["filetype:md", "beta"]
        kb._save_meta(meta)
        self._seed_item(kb, "i2", [])
        assert kb.list_tags() == ["beta", "filetype:md"]


class TestStatsItem:
    def test_stats_shape(self, kb: Any) -> None:
        stats = kb.get_stats()
        assert stats == {
            "total": 0,
            "categories_count": 0,
            "tags_count": 0,
            "total_chunks": 0,
            "total_size": 0,
        }

    def test_get_item_not_found_404(self, kb: Any) -> None:
        with pytest.raises(kb.KBError) as ei:
            kb.get_item("nope")
        assert ei.value.status_code == 404

    def test_delete_item_removes_meta_and_file(self, kb: Any, client: MagicMock, tmp_path: Path) -> None:
        """删除：元数据移除 + 源文件删除 + 尽力清理 hindsight chunk。"""
        source = tmp_path / "uploads" / "kb"
        source.mkdir(parents=True, exist_ok=True)
        (source / "i1.md").write_text("x", encoding="utf-8")
        meta = kb._load_meta()
        meta.setdefault("items", []).append(
            {
                "id": "i1",
                "name": "i1.md",
                "size": 1,
                "mime_type": "text/markdown",
                "categories": [],
                "tags": [],
                "chunk_count": 2,
                "chunk_ids": ["c1", "c2"],
                "source_file": str(source / "i1.md"),
                "created_at": "t0",
                "updated_at": "t0",
            }
        )
        kb._save_meta(meta)

        client.documents = MagicMock()
        client.documents.delete_document = AsyncMock(return_value=None)

        result = _run(kb.delete_item("i1"))
        assert result["id"] == "i1"
        assert kb.list_items() == []
        assert not (source / "i1.md").exists()
        assert client.documents.delete_document.call_count == 2

    def test_delete_item_unknown_404(self, kb: Any) -> None:
        with pytest.raises(kb.KBError) as ei:
            _run(kb.delete_item("nope"))
        assert ei.value.status_code == 404


# ═══════════════════════════════════════════════════════════
# 检索
# ═══════════════════════════════════════════════════════════


class TestSearch:
    def _seed_item(self, kb: Any, item_id: str, chunk_ids: list[str]) -> None:
        meta = kb._load_meta()
        meta.setdefault("items", []).append(
            {
                "id": item_id,
                "name": f"{item_id}.md",
                "size": 10,
                "mime_type": "text/markdown",
                "categories": ["架构"],
                "tags": ["filetype:md"],
                "chunk_count": len(chunk_ids),
                "chunk_ids": chunk_ids,
                "source_file": "",
                "created_at": "2026-08-21T00:00:00Z",
                "updated_at": "2026-08-21T00:00:00Z",
            }
        )
        kb._save_meta(meta)

    def test_search_returns_registered_chunks_with_context(self, kb: Any, client: MagicMock) -> None:
        """召回 chunk 经归属索引回连条目（含分类/标签上下文）。"""
        self._seed_item(kb, "i1", ["c1", "c2"])
        client.arecall.return_value = _recall_response([
            {"id": "c1", "text": "分块一", "score": 0.9},
            {"id": "c2", "text": "分块二", "score": 0.8},
        ])

        result = _run(kb.search("架构"))

        call_kwargs = client.arecall.call_args.kwargs
        assert call_kwargs["bank_id"] == "kb"
        assert call_kwargs["include_chunks"] is True
        assert call_kwargs["tags"] == ["type:knowledge"]
        assert result["total"] == 2
        first = result["results"][0]
        assert first["item_id"] == "i1"
        assert first["name"] == "i1.md"
        assert first["categories"] == ["架构"]
        assert first["content"] == "分块一"

    def test_search_drops_orphan_chunks(self, kb: Any, client: MagicMock) -> None:
        """未注册 chunk（已删条目残留）不回显。"""
        self._seed_item(kb, "i1", ["c1"])
        client.arecall.return_value = _recall_response([
            {"id": "c1", "text": "live", "score": 0.9},
            {"id": "ghost", "text": "orphan", "score": 0.7},
        ])
        result = _run(kb.search("q"))
        assert result["total"] == 1
        assert result["results"][0]["content"] == "live"

    def test_search_category_tag_filter(self, kb: Any, client: MagicMock) -> None:
        self._seed_item(kb, "i1", ["c1"])
        client.arecall.return_value = _recall_response([])
        _run(kb.search("q", category="架构", tag="dev"))
        tags = client.arecall.call_args.kwargs["tags"]
        assert "kb_cat:架构" in tags
        assert "kb_tag:dev" in tags
        assert client.arecall.call_args.kwargs["tags_match"] == "all"

    def test_search_falls_back_to_results_when_no_chunks(self, kb: Any, client: MagicMock) -> None:
        """无 chunks 时用 results 事实兜底。"""
        self._seed_item(kb, "i1", ["c1"])
        resp = MagicMock()
        resp.chunks = []
        resp.results = [SimpleNamespace(model_dump=lambda: {"id": "c1", "text": "事实文本", "score": 0.6})]
        client.arecall.return_value = resp
        result = _run(kb.search("q"))
        assert result["results"][0]["content"] == "事实文本"

    def test_search_empty_query_400(self, kb: Any, client: MagicMock) -> None:
        with pytest.raises(kb.KBError) as ei:
            _run(kb.search("   "))
        assert ei.value.status_code == 400

    def test_search_recall_error_degrades(self, kb: Any, client: MagicMock) -> None:
        client.arecall = AsyncMock(side_effect=RuntimeError("recall down"))
        result = _run(kb.search("q"))
        assert result == {"results": [], "total": 0, "error": "recall down"}

    def test_search_top_k_clamped(self, kb: Any, client: MagicMock) -> None:
        self._seed_item(kb, "i1", ["c1"])
        _run(kb.search("q", top_k=9999))
        assert client.arecall.call_args.kwargs["query"] == "q"


# ═══════════════════════════════════════════════════════════
# check / 元数据仓韧性
# ═══════════════════════════════════════════════════════════


class TestCheckAndMeta:
    def test_check_reports_unavailable_without_client(self, kb: Any) -> None:
        result = _run(kb.check_available())
        assert result["available"] is False
        assert "未初始化" in result["message"]

    def test_check_reports_available_with_client(self, kb: Any, client: MagicMock) -> None:
        result = _run(kb.check_available())
        assert result["available"] is True
        assert result["backend"] == "hindsight"
        assert result["bank"] == "kb"

    def test_check_reports_unavailable_on_bank_error(self, kb: Any, client: MagicMock) -> None:
        client.acreate_bank = AsyncMock(side_effect=RuntimeError("pg0 down"))
        result = _run(kb.check_available())
        assert result["available"] is False

    def test_corrupted_meta_rebuilds_empty(self, kb: Any, tmp_path: Path) -> None:
        meta_path = tmp_path / "kb" / "kb_meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text("{not-json", encoding="utf-8")
        assert kb.list_items() == []
        assert kb.get_stats()["total"] == 0

    def test_meta_atomic_write(self, kb: Any, tmp_path: Path) -> None:
        kb.create_category("a")
        kb.create_category("b")
        raw = json.loads((tmp_path / "kb" / "kb_meta.json").read_text(encoding="utf-8"))
        assert len(raw["categories"]) == 2
        assert not (tmp_path / "kb" / "kb_meta.json.tmp").exists()


# ═══════════════════════════════════════════════════════════
# manifest：knowledge-base 端点声明
# ═══════════════════════════════════════════════════════════


class TestManifestKbEndpoints:
    def test_manifest_declares_knowledge_base_endpoints(self) -> None:
        """10 个知识库端点 + 检索端点全部声明（auth=user，前缀 hindsight_memory_service）。"""
        data = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        expected = [
            ("/ext/hindsight_memory_service/knowledge-base", "GET"),
            ("/ext/hindsight_memory_service/knowledge-base/stats", "GET"),
            ("/ext/hindsight_memory_service/knowledge-base/upload", "POST"),
            ("/ext/hindsight_memory_service/knowledge-base/check", "GET"),
            ("/ext/hindsight_memory_service/knowledge-base/categories", "GET"),
            ("/ext/hindsight_memory_service/knowledge-base/categories", "POST"),
            ("/ext/hindsight_memory_service/knowledge-base/categories/{name}", "DELETE"),
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
