# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""knowledge_base / routes_memory 剩余分支补充覆盖。

主用例见 test_knowledge_base.py / test_routes_memory.py / test_hindsight_server_http.py；
本文件补：
1. knowledge_base：upload 纯空白内容 400、bank 异常（含 KBError）如实失败、
   verbatim 失败降级告警（禁静默）、成功路径零清理调用、search 无文本 chunk
   跳过（chunks 与 results 双面，双 bank 扇出）；
2. routes_memory：list_episodes/list_semantic/search_memories_post 后端缺失降级、
   consolidate 的 int 分支。

mock 仅限外部依赖（hindsight sidecar 面）；无本地真源（bank 唯一存储）。

[来源: plugins/shared/hindsight_memory/knowledge_base.py、routes_memory.py]
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

# X10-F1 身份头契约：kb 域按调用方租户分 bank；default 身份 ↔ 存量 bank "kb"。
_KB_HEADERS = {"x-agentos-tenant": "default", "x-agentos-user": "admin"}

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# db_read 共享根自举（routes_memory 裸名导入 db_read 的前提，bootstrap 同款）
_SHARED_ROOT = _PLUGIN_DIR.parent
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))


def _load_module(name: str, mod_name: str, file: str) -> Any:
    path = _PLUGIN_DIR / file
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None, f"Cannot load {file}"
    assert spec.loader is not None, f"Cannot load {file}"
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
def kb() -> Any:
    module = _load_module("kb_extra", "hindsight_kb_extra_test", "knowledge_base.py")
    module.set_client(None)
    return module


class TestKbUploadEdges:
    def test_upload_whitespace_only_text_400(self, kb: Any) -> None:
        """解码后纯空白文本 → 400（无法提取文本）。"""
        client = MagicMock()
        kb.set_client(client)
        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("a.txt", b"   \n  ", headers=_KB_HEADERS))
        assert ei.value.status_code == 400
        client.aretain.assert_not_called()

    def test_upload_bank_kberror_fails_honestly(self, kb: Any) -> None:
        """bank 侧异常（含 KBError）→ 上传如实失败（唯一存储无兜底，500 归一）。"""
        client = MagicMock()

        def _raise_kberror(**kwargs: Any) -> Any:
            raise kb.KBError(503, "KNB_ING_9001", "服务不可用")

        client.aretain = AsyncMock(side_effect=_raise_kberror)
        client.acreate_bank = AsyncMock(return_value=None)
        client.documents = MagicMock()
        client.documents.delete_document = AsyncMock(return_value=None)
        kb.set_client(client)

        with pytest.raises(kb.KBError) as ei:
            _run(kb.upload_document("a.txt", b"content", headers=_KB_HEADERS))
        assert ei.value.status_code == 500
        assert ei.value.error_code == "KNB_ING_8001"

    def test_upload_primary_failure_degrades_with_warning_not_masked(
        self, kb: Any, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """verbatim 落库失败 → 降级 kb_chunks 落库成功 + 告警留痕（如实上报，
        不静默降级也不掩盖降级事实）。"""
        monkeypatch.setenv("HINDSIGHT_API_LLM_PROVIDER", "openai")
        client = MagicMock()
        client.acreate_bank = AsyncMock(return_value=None)
        client.documents = MagicMock()

        async def retain_primary_fails(**kw: Any) -> Any:
            if kw["bank_id"] == "kb":
                raise RuntimeError("vector down")
            return MagicMock(operation_id="c-fb", accepted=True)

        client.aretain = AsyncMock(side_effect=retain_primary_fails)
        kb.set_client(client)

        with caplog.at_level("WARNING"):
            result = _run(kb.upload_document("a.txt", b"content", headers=_KB_HEADERS))
        assert result["ingestion_mode"] == "chunks"
        assert result["degraded"] is True
        assert result["degradation_reason"]
        assert any("降级" in r.getMessage() for r in caplog.records)

    def test_upload_success_zero_cleanup_calls(self, kb: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """整文档单次 aretain 成功路径：零清理调用（单写面无清理面）。"""
        monkeypatch.setenv("HINDSIGHT_API_LLM_PROVIDER", "openai")
        client = MagicMock()
        client.aretain = AsyncMock(return_value=MagicMock(operation_id="", accepted=True))
        client.acreate_bank = AsyncMock(return_value=None)
        client.documents = MagicMock()
        client.documents.delete_document = AsyncMock(return_value=None)
        kb.set_client(client)

        result = _run(kb.upload_document("a.txt", b"hello", headers=_KB_HEADERS))
        assert result["chunks_imported"] == 1
        assert result["ingestion_mode"] == "verbatim"
        client.documents.delete_document.assert_not_called()


class TestKbSearchEdges:
    def test_search_skips_chunk_without_text(self, kb: Any) -> None:
        """chunks 集合含无文本条目 → 跳过（不产出空内容结果，不做无谓归属解析）。
        检索按调用方可见双 bank 扇出，主 bank 命中、降级 bank 空回。"""
        client = MagicMock()

        def recall_by_bank(**kw: Any) -> Any:
            if kw["bank_id"] == "kb":
                resp = MagicMock()
                resp.chunks = [
                    SimpleNamespace(model_dump=lambda: {"id": "c1", "text": "", "score": 0.9}),
                    SimpleNamespace(model_dump=lambda: {"id": "c2", "content": "有内容", "score": 0.8}),
                ]
                resp.results = []
                return resp
            return SimpleNamespace(chunks=[], results=[])

        client.arecall = AsyncMock(side_effect=recall_by_bank)
        client.documents = MagicMock()
        client.documents.get_document = AsyncMock(return_value=SimpleNamespace(model_dump=lambda: {}))
        kb.set_client(client)

        result = _run(kb.search("q", headers=_KB_HEADERS))

        assert result["total"] == 1
        assert result["results"][0]["content"] == "有内容"
        client.documents.get_document.assert_awaited_once()

    def test_search_results_fallback_skips_no_text(self, kb: Any) -> None:
        """results 兜底面：无文本条目跳过；dict 条目与 model_dump 条目混用。"""

        def recall_by_bank(**kw: Any) -> Any:
            if kw["bank_id"] == "kb":
                resp = MagicMock()
                resp.chunks = []
                resp.results = [
                    {"id": "f1", "text": "事实", "score": 0.5},
                    {"id": "f2", "content": "", "score": 0.4},
                ]
                return resp
            return SimpleNamespace(chunks=[], results=[])

        client = MagicMock()
        client.arecall = AsyncMock(side_effect=recall_by_bank)
        kb.set_client(client)

        result = _run(kb.search("q", headers=_KB_HEADERS))

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
        result = _run(routes.list_episodes(["default"], page=3, page_size=10))
        assert result == {"items": [], "total": 0, "page": 3, "page_size": 10}

    def test_list_semantic_degrades_without_backend(self, routes: Any) -> None:
        result = _run(routes.list_semantic(["default"]))
        assert result == {"items": [], "total": 0}

    def test_search_post_degrades_without_backend(self, routes: Any) -> None:
        """有 body 但后端缺失 → 空结果（不抛）。"""
        result = _run(routes.search_memories_post(["default"], {"query": "q"}))
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
