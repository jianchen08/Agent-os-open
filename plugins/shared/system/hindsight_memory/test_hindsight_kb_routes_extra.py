# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""knowledge_base / routes_memory 剩余分支补充覆盖。

主用例见 test_knowledge_base.py / test_routes_memory.py / test_hindsight_server_http.py；
本文件补：
1. knowledge_base：delete_item 源文件删除失败告警、chunk 清理失败告警、
   upload 纯空白内容 400、KBError 重抛、回滚 OSError 兜底、uploads_path 解析失败
   回退插件 data 目录、search 无文本 chunk 跳过（chunks 与 results 双面）、
   search 无 id 条目；
2. routes_memory：list_episodes/list_semantic/search_memories_post 后端缺失降级、
   consolidate 的 int 分支。

mock 仅限外部依赖（hindsight client / 文件系统错误注入）；元数据仓走真实 JSON。

[来源: plugins/shared/system/hindsight_memory/knowledge_base.py、routes_memory.py]
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


def _load_module(name: str, mod_name: str, file: str) -> Any:
    path = _PLUGIN_DIR / file
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None, f"Cannot load {file}"
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


# ═══════════════════════════════════════════════════════════
# knowledge_base 剩余分支
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def kb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = _load_module("kb_extra", "hindsight_kb_extra_test", "knowledge_base.py")
    module.set_data_dir(str(tmp_path / "kb"))
    module.set_client(None)
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    return module


def _seed_item(kb: Any, item_id: str, chunk_ids: list[str] | None = None, source_file: str = "") -> None:
    meta = kb._load_meta()
    meta.setdefault("items", []).append({
        "id": item_id, "name": f"{item_id}.md", "size": 10, "mime_type": "text/markdown",
        "categories": [], "tags": [], "chunk_count": len(chunk_ids or []),
        "chunk_ids": chunk_ids or [], "source_file": source_file,
        "created_at": "t0", "updated_at": "t0",
    })
    kb._save_meta(meta)


class TestKbDeleteItemResilience:
    def test_source_file_delete_failure_warns_but_succeeds(self, kb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """源文件删除抛 OSError → 告警但条目删除仍成功（尽力删除语义）。"""
        source = tmp_path / "locked.md"
        source.write_text("x", encoding="utf-8")
        _seed_item(kb, "i1", [], str(source))

        def _raise_os(*args: Any, **kwargs: Any) -> Any:
            raise OSError("permission denied")

        # 必须 monkeypatch（自动还原）：kb.os 是进程级全局 os 模块，
        # 裸赋值会污染后续所有测试
        monkeypatch.setattr(kb.os, "remove", MagicMock(side_effect=_raise_os))
        with caplog.at_level("WARNING"):
            result = _run(kb.delete_item("i1"))

        assert result["id"] == "i1"
        assert kb.list_items() == []
        assert any("删除源文件失败" in r.getMessage() for r in caplog.records)

    def test_chunk_cleanup_failure_warns_but_succeeds(self, kb: Any, caplog: pytest.LogCaptureFixture) -> None:
        """hindsight 侧 chunk 清理失败 → 告警但条目删除仍成功（尽力而为）。"""
        _seed_item(kb, "i1", ["c1", "c2"])
        client = MagicMock()
        client.documents = MagicMock()
        client.documents.delete_document = AsyncMock(side_effect=RuntimeError("cleanup down"))
        kb.set_client(client)

        with caplog.at_level("WARNING"):
            result = _run(kb.delete_item("i1"))

        assert result["id"] == "i1"
        assert client.documents.delete_document.await_count == 2
        assert any("chunk 清理失败" in r.getMessage() for r in caplog.records)

    def test_delete_without_client_skips_chunk_cleanup(self, kb: Any) -> None:
        """client 未注入时跳过 hindsight 清理（元数据删除仍成功）。"""
        _seed_item(kb, "i1", ["c1"])
        result = _run(kb.delete_item("i1"))
        assert result["id"] == "i1"
        assert kb.list_items() == []


class TestKbUploadEdges:
    def test_upload_whitespace_only_text_400(self, kb: Any) -> None:
        """解码后纯空白文本 → 400（无法提取文本）。"""
        client = MagicMock()
        kb.set_client(client)
        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("a.txt", b"   \n  "))
        assert ei.value.status_code == 400
        client.aretain.assert_not_called()

    def test_upload_kberror_rethrow_keeps_status(self, kb: Any) -> None:
        """入库阶段抛 KBError（如自定义）→ 原样重抛，不改 500。"""
        client = MagicMock()

        def _raise_kberror(**kwargs: Any) -> Any:
            raise kb.KBError(503, "KNB_ING_9001", "服务不可用")

        client.aretain = AsyncMock(side_effect=_raise_kberror)
        client.acreate_bank = AsyncMock(return_value=None)
        kb.set_client(client)

        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("a.txt", b"content"))
        assert ei.value.status_code == 503

    def test_upload_rollback_oserror_passthrough(self, kb: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """入库失败且回滚删文件也失败 → 不叠加崩溃（仍报入库失败 500）。"""
        client = MagicMock()
        client.aretain = AsyncMock(side_effect=RuntimeError("vector down"))
        client.acreate_bank = AsyncMock(return_value=None)
        kb.set_client(client)

        monkeypatch.setattr(kb.os, "remove", MagicMock(side_effect=OSError("locked")))

        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("a.txt", b"content"))
        assert ei.value.status_code == 500
        assert "知识块入库失败" in ei.value.message

    def test_upload_aretain_no_operation_id(self, kb: Any, tmp_path: Path) -> None:
        """aretain 返回无 operation_id → chunk_ids 不收集（条目仍注册）。"""
        client = MagicMock()
        client.aretain = AsyncMock(return_value=MagicMock(operation_id="", accepted=True))
        client.acreate_bank = AsyncMock(return_value=None)
        kb.set_client(client)

        result = _run(kb.upload_document("a.txt", b"hello"))
        assert result["chunks_imported"] == 1
        assert kb.get_item(result["item_id"])["chunk_count"] == 1

    def test_uploads_path_fallback_on_resolve_error(self, kb: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """uploads_path 解析失败 → 回退插件 data/kb/uploads（不炸）。"""
        monkeypatch.setitem(sys.modules, "uploads_path", None)

        resolved = kb._resolve_kb_uploads_dir()

        assert resolved == str(tmp_path / "kb" / "uploads")


class TestKbSearchEdges:
    def test_search_skips_chunk_without_text(self, kb: Any) -> None:
        """chunks 集合含无文本条目 → 跳过（不产出空内容结果）。"""
        _seed_item(kb, "i1", ["c1", "c2"])
        client = MagicMock()
        resp = MagicMock()
        resp.chunks = [
            SimpleNamespace(model_dump=lambda: {"id": "c1", "text": "", "score": 0.9}),
            SimpleNamespace(model_dump=lambda: {"id": "c2", "content": "有内容", "score": 0.8}),
        ]
        resp.results = []
        client.arecall = AsyncMock(return_value=resp)
        kb.set_client(client)

        result = _run(kb.search("q"))

        assert result["total"] == 1
        assert result["results"][0]["content"] == "有内容"

    def test_search_results_fallback_skips_no_text(self, kb: Any) -> None:
        """results 兜底面：无文本条目跳过；dict 条目与 model_dump 条目混用。"""
        _seed_item(kb, "i1", ["c1"])
        client = MagicMock()
        resp = MagicMock()
        resp.chunks = []
        resp.results = [
            {"id": "c1", "text": "事实", "score": 0.5},
            {"id": "c1", "content": "", "score": 0.4},
        ]
        client.arecall = AsyncMock(return_value=resp)
        kb.set_client(client)

        result = _run(kb.search("q"))

        assert result["total"] == 1
        assert result["results"][0]["content"] == "事实"


# ═══════════════════════════════════════════════════════════
# routes_memory 剩余分支
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def routes() -> Any:
    module = _load_module("routes_extra", "hindsight_routes_extra_test", "routes_memory.py")
    module.set_memory_backend(None)
    yield module
    module.set_memory_backend(None)


class TestRoutesDegradeBranches:
    def test_list_episodes_degrades_without_backend(self, routes: Any) -> None:
        result = _run(routes.list_episodes(page=3, page_size=10))
        assert result == {"items": [], "total": 0, "page": 3, "page_size": 10}

    def test_list_semantic_degrades_without_backend(self, routes: Any) -> None:
        result = _run(routes.list_semantic())
        assert result == {"items": [], "total": 0}

    def test_search_post_degrades_without_backend(self, routes: Any) -> None:
        """有 body 但后端缺失 → 空结果（不抛）。"""
        result = _run(routes.search_memories_post({"query": "q"}))
        assert result == {"items": [], "total": 0}

    def test_consolidate_int_result(self, routes: Any) -> None:
        """reflect 返回 int → 直接计数。"""
        backend = MagicMock()
        backend.reflect = AsyncMock(return_value=5)
        routes.set_memory_backend(backend)

        result = _run(routes.consolidate_memory())

        assert result["consolidated_count"] == 5

    def test_consolidate_dict_count_key(self, routes: Any) -> None:
        """reflect 返回 dict 且仅 count 键 → 用 count。"""
        backend = MagicMock()
        backend.reflect = AsyncMock(return_value={"count": 9})
        routes.set_memory_backend(backend)

        result = _run(routes.consolidate_memory())

        assert result["consolidated_count"] == 9
