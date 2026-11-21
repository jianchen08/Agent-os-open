# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @audit: T5#4 | @ci: none-local
"""server.py HTTP 展示面（http.handle 分发 + helpers）补充覆盖测试。

主用例见 test_hindsight_server.py；本文件补：
1. http_handle 顶层路由：recall（含 top_k 钳制）、stats、未知路径 404、异常 500；
2. memory 域分发全路由（list/search GET/POST/episodes/单条/consolidate/stats/semantic），
   含非 memory 路径 404、业务异常转 404/400、未预期异常转 500；
3. knowledge-base 域分发全路由（list/stats/check/search/upload/categories/tags/单条删），
   含非 kb 路径 404、upload 校验失败 400、业务异常转 400、未预期异常转 500；
4. helpers：_json_response/_ok/_decode_body/_parse_multipart/_kberr_response。

存储与检索逻辑走真实模块实现（tmp 数据目录 + mock 外部 client）。

[来源: plugins/shared/hindsight_memory/server.py]
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

# X10-F1 身份头契约：kb 分发把入站 headers 穿透到 knowledge_base（租户分 bank，
# 缺身份头读面空集/写面 403）；本文件锁路由与响应映射，用 default 身份驱动。
_KB_HEADERS = {"x-agentos-tenant": "default", "x-agentos-user": "admin"}

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 server.py（每次新建，避免模块级状态跨测试污染）。"""
    mod_name = "hindsight_server_http_test"
    path = _PLUGIN_DIR / "server.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
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


@pytest.fixture
def srv() -> Any:
    """每个测试独立 server 模块实例。"""
    module = _load_module()
    module._client = None
    module._memory_backend = None
    module._memory_backend_attempted = False
    yield module
    module._client = None
    module._memory_backend = None
    module._memory_backend_attempted = False


@pytest.fixture
def routes() -> Any:
    """routes_memory 模块（与 server 分发共享同一实例），测试后清空后端。"""
    import routes_memory as rmm

    yield rmm
    rmm.set_memory_backend(None)


@pytest.fixture
def default_owner(monkeypatch: pytest.MonkeyPatch, routes: Any) -> None:
    """路由单测身份替身：resolve 为单默认 bank（归属过滤语义归
    test_routes_identity_filter 专测，本文件只锁路由/响应映射）。"""
    async def _resolve(plugin: Any, headers: Any, backend: Any) -> list[str]:
        return [routes._DEFAULT_BANK_ID]

    monkeypatch.setattr(routes, "resolve_caller_banks", _resolve)


@pytest.fixture
def kb() -> Any:
    """knowledge_base 模块（bank 唯一存储，无本地状态），测试后清空 client。"""
    import knowledge_base as kbm

    kbm.set_client(None)
    yield kbm
    kbm.set_client(None)


# ═══════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════


class TestHttpHelpers:
    def test_json_response_roundtrip(self, srv: Any) -> None:
        """_json_response：base64 body 可解码回原文，支持状态码覆盖与非 ASCII。"""
        resp = srv._json_response({"message": "中文", "n": 1})
        assert resp["status"] == 200
        assert resp["body_encoding"] == "base64"
        decoded = json.loads(base64.b64decode(resp["body"]).decode("utf-8"))
        assert decoded == {"message": "中文", "n": 1}

        resp404 = srv._json_response({"detail": "x"}, 404)
        assert resp404["status"] == 404

    def test_ok_wraps_data(self, srv: Any) -> None:
        out = srv._ok({"a": 1})
        assert out == {"success": True, "data": {"a": 1}}

    def test_decode_body_variants(self, srv: Any) -> None:
        """base64 JSON / 明文 JSON / 空串三种输入。"""
        assert srv._decode_body("") == {}
        encoded = base64.b64encode(b'{"a": 1}').decode("ascii")
        assert srv._decode_body(encoded) == {"a": 1}
        assert srv._decode_body('{"b": 2}') == {"b": 2}

    def test_decode_body_invalid_json_raises(self, srv: Any) -> None:
        """非法 JSON → ValueError（调用方捕获转 400）。"""
        with pytest.raises(ValueError):
            srv._decode_body("not-json-at-all")

    def test_parse_multipart_fields(self, srv: Any) -> None:
        """普通字段为 str；文件字段为 {filename, content_type, data}。"""
        boundary = "bnd-test"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="title"\r\n\r\n'
            f"Hello\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="a.md"\r\n'
            f"Content-Type: text/markdown\r\n\r\n"
            f"DATA-BYTES\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        fields = srv._parse_multipart(f"multipart/form-data; boundary={boundary}", body)

        assert fields["title"] == "Hello"
        file_field = fields["file"]
        assert file_field["filename"] == "a.md"
        assert file_field["content_type"] == "text/markdown"
        assert file_field["data"] == b"DATA-BYTES"

    def test_parse_multipart_skips_nameless_part(self, srv: Any) -> None:
        """无 name 参数的分部被跳过（防御）。"""
        boundary = "b2"
        body = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data\r\n\r\n"
            f"junk\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        fields = srv._parse_multipart(f"multipart/form-data; boundary={boundary}", body)
        assert fields == {}

    def test_parse_multipart_not_multipart(self, srv: Any) -> None:
        """非 multipart 内容类型 → 空 dict。"""
        assert srv._parse_multipart("text/plain", b"hello") == {}

    def test_kberr_response_uses_status_and_message(self, srv: Any) -> None:
        """带 status_code/message 的业务异常转 HTTP 响应。"""
        exc = RuntimeError("未找到相关记忆")
        exc.status_code = 404  # type: ignore[attr-defined]
        exc.message = "未找到相关记忆"  # type: ignore[attr-defined]
        out = srv._kberr_response(exc)
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert out["data"]["status"] == 404
        assert payload == {"detail": "未找到相关记忆"}

    def test_kberr_response_defaults_500(self, srv: Any) -> None:
        """普通异常（无 status_code）→ 500 + str(exc) 消息。"""
        out = srv._kberr_response(RuntimeError("boom"))
        assert out["data"]["status"] == 500
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload == {"detail": "boom"}


# ═══════════════════════════════════════════════════════════
# http_handle 顶层路由
# ═══════════════════════════════════════════════════════════


class TestHttpHandleTop:
    def test_recall_route_survives_extreme_limits(self, srv: Any, default_owner: None) -> None:
        """recall 路由：limit 任意整数值不崩溃；top_k 不透传（hindsight API
        无 top_k 形参，token 预算驱动——与 summarize 修复同契约）。"""
        client = MagicMock()
        client.arecall = AsyncMock(return_value=MagicMock(results=[], chunks=[]))
        srv._client = client

        for limit in ("0", "999", "5"):
            out = _run(srv.http_handle(
                path="/ext/hindsight_memory_service/recall", method="GET",
                plugin_id="x", query={"query": "q", "limit": limit},
            ))
            assert out["success"] is True

        assert client.arecall.call_count == 3
        for call in client.arecall.call_args_list:
            kwargs = call.kwargs
            assert "top_k" not in kwargs, "arecall 无 top_k 形参，不得透传"
            assert kwargs["bank_id"] == "default"
            assert kwargs["query"] == "q"

    def test_stats_route_reports_state(self, srv: Any) -> None:
        """stats 路由：bank_id 回落 + initialized 如实。"""
        srv._client = None
        out = _run(srv.http_handle(
            path="/ext/hindsight_memory_service/stats", method="GET",
            plugin_id="x", query={"bank_id": "tenant-7"},
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["backend"] == "hindsight"
        assert payload["bank_id"] == "tenant-7"
        assert payload["initialized"] is False

    def test_unknown_path_404(self, srv: Any) -> None:
        out = _run(srv.http_handle(
            path="/ext/hindsight_memory_service/nope", method="GET", plugin_id="x",
        ))
        assert out["success"] is True
        assert out["data"]["status"] == 404

    def test_top_level_exception_500_envelope(self, srv: Any) -> None:
        """http_handle 未捕获异常 → {success: False} 500 信封（不崩溃）。"""
        out = _run(srv.http_handle(
            path="/ext/hindsight_memory_service/recall", method="GET",
            plugin_id="x", query={"query": "q", "limit": "abc"},
        ))
        assert out["success"] is False
        assert out["data"]["status"] == 500


# ═══════════════════════════════════════════════════════════
# memory 域分发
# ═══════════════════════════════════════════════════════════


def _inject_backend(srv: Any, backend: MagicMock) -> None:
    """短路懒注入，直接使用给定 mock 后端（与既有测试同款）。"""
    srv._memory_backend = backend
    srv._memory_backend_attempted = True


def _mem_item(id_: str, mtype: str = "semantic", **meta: Any) -> dict[str, Any]:
    return {"id": id_, "content": f"content-{id_}", "score": 1.0, "memory_type": mtype, "metadata": meta}


def _doc_item(id_: str, mtype: str = "semantic", tags: list[str] | None = None) -> dict[str, Any]:
    """sidecar documents 面条目（retain 注入的 type:* 服务端标签形态）。"""
    return {
        "id": id_,
        "original_text": f"content-{id_}",
        "tags": [f"type:{mtype}"] + list(tags or []),
        "document_metadata": {"memory_type": mtype},
        "created_at": "t0",
    }


class TestMemoryDomainDispatch:
    def test_non_memory_path_404(self, srv: Any) -> None:
        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/other", "GET", "", {}
        ))
        assert out["data"]["status"] == 404
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert "not a memory path" in payload["error"]

    def test_list_route_params(self, srv: Any, default_owner: None, routes: Any) -> None:
        backend = MagicMock()
        backend.get_documents = AsyncMock(return_value=[_doc_item("m1", "episode", tags=["t"])])
        _inject_backend(srv, backend)

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory", "GET", "",
            {"memory_type": "episode", "limit": "5", "offset": "1"},
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["total"] == 1
        assert payload["items"][0]["id"] == "m1"
        assert backend.get_documents.call_args.kwargs["tags"] == ["type:episode"]
        assert backend.get_documents.call_args.kwargs["limit"] == 5

    def test_list_route_invalid_int_defaults(self, srv: Any, default_owner: None) -> None:
        """limit/offset 非数字 → 回落默认值（20/0），不 500。"""
        backend = MagicMock()
        backend.get_documents = AsyncMock(return_value=[])
        _inject_backend(srv, backend)

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory", "GET", "",
            {"limit": "abc", "offset": "xyz"},
        ))
        assert out["data"]["status"] == 200
        assert backend.get_documents.call_args.kwargs["limit"] == 20

    def test_search_get_route(self, srv: Any, default_owner: None) -> None:
        backend = MagicMock()
        backend.search = AsyncMock(return_value=[_mem_item("s1")])
        _inject_backend(srv, backend)

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory/search", "GET", "",
            {"query": "q", "top_k": "7", "method": "keyword"},
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["total"] == 1
        assert backend.search.call_args.kwargs["query"] == "q"

    def test_search_post_route(self, srv: Any, default_owner: None) -> None:
        backend = MagicMock()
        backend.search = AsyncMock(return_value=[_mem_item("s2")])
        _inject_backend(srv, backend)
        body = base64.b64encode(b'{"query": "q", "top_k": 3}').decode("ascii")

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory/search", "POST", body, {}
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["total"] == 1
        assert backend.search.call_args.kwargs["top_k"] == 3

    def test_episodes_routes(self, srv: Any, default_owner: None) -> None:
        backend = MagicMock()
        backend.get_documents = AsyncMock(return_value=[_doc_item("e1", "episode", tags=["a"])])
        backend.search = AsyncMock(return_value=[_mem_item("e1", "episode", tags=["a"])])
        _inject_backend(srv, backend)

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory/episodes", "GET", "",
            {"page": "2", "page_size": "10"},
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["page"] == 2
        assert payload["items"][0]["id"] == "e1"
        assert backend.get_documents.call_args.kwargs["tags"] == ["type:episode"]

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory/episodes/e1", "GET", "", {}
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["id"] == "e1"

    def test_semantic_and_consolidate_routes(self, srv: Any, default_owner: None) -> None:
        backend = MagicMock()
        backend.get_documents = AsyncMock(return_value=[_doc_item("sem1")])
        del backend.reflect  # 无 reflect 能力的后端 → 整合走空操作 stub
        _inject_backend(srv, backend)

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory/semantic", "GET", "", {}
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["items"][0]["source_type"] == "memory_backend"

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory/consolidate", "POST", "", {}
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["success"] is True
        assert payload["consolidated_count"] == 0

    def test_stats_route_counts_by_type(self, srv: Any, default_owner: None) -> None:
        async def _get_documents(**kwargs: Any) -> list[dict[str, Any]]:
            return [_doc_item("x")] if kwargs.get("tags") == ["type:episode"] else [_doc_item("y"), _doc_item("z")]

        backend = MagicMock()
        backend.get_documents = AsyncMock(side_effect=_get_documents)
        _inject_backend(srv, backend)

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory/stats", "GET", "", {}
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload == {
            "episode_count": 1, "knowledge_count": 2, "total_count": 3, "last_updated": "",
        }

    def test_single_memory_get_and_delete_routes(self, srv: Any, default_owner: None) -> None:
        backend = MagicMock()
        backend.get_documents = AsyncMock(return_value=[_doc_item("m9")])
        backend.delete = AsyncMock(return_value=True)
        _inject_backend(srv, backend)

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory/m9", "GET", "", {}
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["id"] == "m9"
        assert payload["content"] == "content-m9"

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory/m9", "DELETE", "", {}
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload == {"message": "记忆已删除"}
        assert backend.delete.call_args.kwargs["memory_id"] == "m9"

    def test_no_route_404(self, srv: Any) -> None:
        _inject_backend(srv, MagicMock())
        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory/foo/bar", "PATCH", "", {}
        ))
        assert out["data"]["status"] == 404

    def test_business_error_maps_status(self, srv: Any) -> None:
        """MemoryAPIError（如 limit 越界 400）→ 对应 HTTP 状态。"""
        _inject_backend(srv, MagicMock())
        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory", "GET", "", {"limit": "0"},
        ))
        assert out["data"]["status"] == 400
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["detail"]

    def test_missing_memory_404_detail(self, srv: Any, default_owner: None) -> None:
        backend = MagicMock()
        backend.get_documents = AsyncMock(return_value=[])
        _inject_backend(srv, backend)

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory/gone", "GET", "", {}
        ))
        assert out["data"]["status"] == 404
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["detail"] == "未找到相关记忆"

    def test_unexpected_error_500(self, srv: Any, default_owner: None) -> None:
        backend = MagicMock()
        backend.search = AsyncMock(side_effect=RuntimeError("backend blew up"))
        _inject_backend(srv, backend)

        out = _run(srv._handle_memory_domain(
            "/ext/hindsight_memory_service/memory", "GET", "", {}
        ))
        assert out["data"]["status"] == 500
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["error"] == "internal server error"


# ═══════════════════════════════════════════════════════════
# knowledge-base 域分发
# ═══════════════════════════════════════════════════════════


def _multipart_body(filename: str, content: str, field_name: str = "file") -> tuple[str, str]:
    boundary = "kbBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: text/markdown\r\n\r\n"
        f"{content}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    return base64.b64encode(body).decode("ascii"), f"multipart/form-data; boundary={boundary}"


def _kb_client() -> MagicMock:
    client = MagicMock()
    client.aretain = AsyncMock(return_value=MagicMock(operation_id="c0", accepted=True))
    client.acreate_bank = AsyncMock(return_value=None)
    return client


class _FakeDocs:
    """hindsight documents 面替身（外部依赖）：list 分页+tags 过滤 / get 404 / delete。"""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def add(self, doc: dict[str, Any]) -> None:
        self.docs[doc["id"]] = doc

    async def list_documents(self, bank_id: str = "", tags: list[str] | None = None,
                             tags_match: str = "any_strict", limit: int = 100,
                             offset: int = 0) -> Any:
        docs = list(self.docs.values())
        if tags:
            if tags_match == "all":
                docs = [d for d in docs if set(tags) <= set(d.get("tags") or [])]
            else:
                docs = [d for d in docs if set(tags) & set(d.get("tags") or [])]
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


def _kb_doc(doc_id: str, kb_id: str, kb_name: str = "i1.md") -> dict[str, Any]:
    return {
        "id": doc_id,
        "bank_id": "kb",
        "original_text": f"chunk-{doc_id}",
        "created_at": "t0",
        "updated_at": "t0",
        "tags": ["type:knowledge", f"kb_item:{kb_id}", "filetype:md"],
        "document_metadata": {
            "kb_id": kb_id, "kb_name": kb_name, "kb_size": "3", "kb_mime": "text/markdown",
            "kb_chunk_total": "1", "source": "knowledge_base", "created_at": "t0",
        },
    }


def _kb_client_with_docs() -> tuple[MagicMock, _FakeDocs]:
    client = MagicMock()
    client.aretain = AsyncMock(return_value=MagicMock(operation_id="c0", accepted=True))
    client.acreate_bank = AsyncMock(return_value=None)
    docs = _FakeDocs()
    client.documents = docs
    return client, docs


class TestKbDomainDispatch:
    def test_non_kb_path_404(self, srv: Any) -> None:
        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/other", "GET", "", {}, _KB_HEADERS
        ))
        assert out["data"]["status"] == 404
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert "not a knowledge-base path" in payload["error"]

    def test_list_and_stats_routes(self, srv: Any, kb: Any) -> None:
        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base", "GET", "", {}, _KB_HEADERS
        ))
        assert json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8")) == []

        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base/stats", "GET", "", {}, _KB_HEADERS
        ))
        stats = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert stats["total"] == 0
        assert "categories_count" in stats

    def test_check_route_degrades_without_client(self, srv: Any, kb: Any) -> None:
        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base/check", "GET", "", {}, _KB_HEADERS
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["available"] is False
        assert payload["bank"] == "kb"

    def test_search_route_with_client(self, srv: Any, kb: Any) -> None:
        client, docs = _kb_client_with_docs()

        def recall_by_bank(**kw: Any) -> Any:
            if kw["bank_id"] == "kb":
                resp = MagicMock()
                resp.chunks = [SimpleNamespace(model_dump=lambda: {"id": "c0", "text": "命中", "score": 0.9})]
                resp.results = []
                return resp
            return SimpleNamespace(chunks=[], results=[])

        client.arecall = AsyncMock(side_effect=recall_by_bank)
        docs.add(_kb_doc("c0", "i1"))
        srv._client = client

        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base/search", "GET", "",
            {"query": "q", "top_k": "5"}, _KB_HEADERS,
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["total"] == 1
        assert payload["results"][0]["item_id"] == "i1"
        assert all(c.kwargs["tags_match"] == "all" for c in client.arecall.call_args_list)

    def test_search_empty_query_maps_400(self, srv: Any, kb: Any) -> None:
        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base/search", "GET", "",
            {"query": "   "}, _KB_HEADERS,
        ))
        assert out["data"]["status"] == 400
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["detail"] == "检索词不能为空"

    def test_upload_success(self, srv: Any, kb: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HINDSIGHT_API_LLM_PROVIDER", "openai")
        srv._client = _kb_client()
        raw_body, content_type = _multipart_body("doc.md", "知识库内容 " * 300)

        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base/upload", "POST", raw_body,
            {}, {"Content-Type": content_type, **_KB_HEADERS},
        ))
        assert out["data"]["status"] == 200
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["message"] == "文件上传成功"
        assert payload["chunks_imported"] == 1
        assert payload["ingestion_mode"] == "verbatim"
        assert srv._client.aretain.call_count == payload["chunks_imported"]

    def test_upload_invalid_base64_400(self, srv: Any, kb: Any) -> None:
        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base/upload", "POST",
            "!!!not-base64!!!", {}, {"Content-Type": "multipart/form-data; boundary=x", **_KB_HEADERS},
        ))
        assert out["data"]["status"] == 400
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert "invalid upload body" in payload["error"]

    def test_upload_requires_multipart_400(self, srv: Any, kb: Any) -> None:
        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base/upload", "POST",
            base64.b64encode(b"x").decode("ascii"), {}, {"Content-Type": "text/plain", **_KB_HEADERS},
        ))
        assert out["data"]["status"] == 400
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert "multipart/form-data" in payload["error"]

    def test_upload_missing_file_field_400(self, srv: Any, kb: Any) -> None:
        raw_body, content_type = _multipart_body("doc.md", "content", field_name="other")
        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base/upload", "POST",
            raw_body, {}, {"Content-Type": content_type, **_KB_HEADERS},
        ))
        assert out["data"]["status"] == 400
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert "missing 'file' field" in payload["error"]

    def test_tags_route(self, srv: Any, kb: Any) -> None:
        client, docs = _kb_client_with_docs()
        docs.add(_kb_doc("c1", "i1"))
        srv._client = client

        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base/tags", "GET", "", {}, _KB_HEADERS,
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload == ["filetype:md"]

    def test_get_item_route(self, srv: Any, kb: Any) -> None:
        client, docs = _kb_client_with_docs()
        docs.add(_kb_doc("c1", "i1"))
        srv._client = client

        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base/i1", "GET", "", {}, _KB_HEADERS,
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["id"] == "i1"
        assert payload["chunk_count"] == 1

    def test_delete_item_route_cleans_chunks(self, srv: Any, kb: Any) -> None:
        client, docs = _kb_client_with_docs()
        docs.add(_kb_doc("c1", "i1"))
        docs.add(_kb_doc("c2", "i1"))
        srv._client = client

        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base/i1", "DELETE", "", {}, _KB_HEADERS,
        ))
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload == {"message": "条目已删除", "id": "i1"}
        assert docs.docs == {}
        assert _run(kb.list_items(headers=_KB_HEADERS)) == []

    def test_get_item_missing_maps_404(self, srv: Any, kb: Any) -> None:
        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base/gone", "GET", "", {}, _KB_HEADERS,
        ))
        assert out["data"]["status"] == 404
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["detail"] == "未找到知识库条目"

    def test_unexpected_error_500(self, srv: Any, kb: Any) -> None:
        """非 KBError 异常（bank documents 面故障）→ 500 信封。"""
        client = MagicMock()
        client.documents = MagicMock()
        client.documents.list_documents = AsyncMock(side_effect=RuntimeError("db down"))
        srv._client = client

        out = _run(srv._handle_kb_domain(
            "/ext/hindsight_memory_service/knowledge-base", "GET", "", {}, _KB_HEADERS,
        ))
        assert out["data"]["status"] == 500
        payload = json.loads(base64.b64decode(out["data"]["body"]).decode("utf-8"))
        assert payload["error"] == "internal server error"
