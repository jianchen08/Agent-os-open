# @feature: FP-0.2.六 记忆检索 | @ci: python-coverage
"""memory HTTP 面身份归属过滤（X4-F1 跨租户读泄漏修复）。

行为锚点（fail-closed）：
- 身份 = 内核 dispatcher 权威注入的 x-agentos-tenant / x-agentos-user 头
  （http_dispatcher.rs 认证后注入、匿名请求剥离——handler 可见即可信）；
  缺任一头 → 可见 bank 空集，绝不返回全量。
- 会话 bank（thread-*）归属：db-read sessions 面（租户内建收窄，Authorization
  原头交内核复权）查 thread_id 是否落在调用方租户；查询失败 / 无凭证 →
  会话 bank 全部不可见。
- 默认 bank：仅 admin 种子租户（tenant_id="default"）可见。
- 端到端（dispatch 层）：list/detail/search/episodes/stats/recall 只见归属内
  bank；不同身份互不可见。

替身边界：db-read 内核库是外部依赖——FakeDbRead 模拟内核复权 + sessions 面
租户收窄；hindsight 后端为逐 bank 行为可控替身（真链路见 test_routes_byid_cross_bank）。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# db_read 共享根自举（routes_memory 裸名导入 db_read 的前提，bootstrap_plugin 同款）
_SHARED_ROOT = _PLUGIN_DIR.parent
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from db_read import DbReadQueryError  # noqa: E402

pytestmark = pytest.mark.unit

_TENANT_ALICE = "tenant-alice"
_TENANT_BOB = "tenant-bob"
_TENANT_ADMIN = "default"  # admin 种子租户（一用户一租户，admin tenant_id 固定 "default"）

# sessions 表实物行（thread_id, tenant_id）——alice/bob 各持一个会话 bank
_SESSIONS_ROWS: list[list[Any]] = [
    ["thread-alice", _TENANT_ALICE],
    ["thread-bob", _TENANT_BOB],
]

# Authorization 原头 → 内核复权出的租户（身份链替身）
_AUTH_TO_TENANT = {
    "Bearer-alice": _TENANT_ALICE,
    "Bearer-bob": _TENANT_BOB,
    "Bearer-admin": _TENANT_ADMIN,
}


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _load_routes(unique: str) -> Any:
    """动态加载 routes_memory.py（独立模块名，避免跨文件模块级状态污染）。"""
    spec = importlib.util.spec_from_file_location(unique, _PLUGIN_DIR / "routes_memory.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    spec.loader.exec_module(module)
    return module


def _load_server(unique: str) -> Any:
    """动态加载 server.py（每次新建，避免模块级状态跨测试污染）。"""
    spec = importlib.util.spec_from_file_location(unique, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    spec.loader.exec_module(module)
    return module


def _identity_headers(tenant: str, user: str, authorization: str = "") -> dict[str, str]:
    """构造内核注入形态的身份头（authorization 为入站原头，透传件）。"""
    headers = {"x-agentos-tenant": tenant, "x-agentos-user": user}
    if authorization:
        headers["authorization"] = authorization
    return headers


class FakeDbRead:
    """db-read.query 替身：模拟内核身份复权 + sessions 面租户收窄。

    收窄语义 = 内核契约：SELECT 结果强制按身份链租户过滤——凭证映射不出
    租户（无凭证/未知凭证）时复权失败，返回空集。
    """

    def __init__(self, rows: list[list[Any]], fail: bool = False) -> None:
        self.rows = rows
        self.fail = fail
        self.calls: list[tuple[str, str, dict[str, Any] | None, str]] = []

    async def query(self, plugin: Any, face: str, sql: str, params: dict[str, Any] | None, authorization: str) -> list[list[Any]]:
        self.calls.append((face, sql, params, authorization))
        if self.fail:
            raise DbReadQueryError("kernel storage unavailable", 503)
        tenant = _AUTH_TO_TENANT.get(authorization or "")
        if not tenant:
            return []  # 复权失败语义：不出任何行
        in_list = list((params or {}).values())
        return [list(r) for r in self.rows if r[1] == tenant and r[0] in in_list]


class StubBackend:
    """逐 bank 行为可控的 IMemoryBackend 替身（list_banks/documents/search/delete）。"""

    def __init__(
        self,
        banks: list[str],
        docs_by_bank: dict[str, list[dict[str, Any]]] | None = None,
        docs_by_id: dict[str, dict[str, dict[str, Any]]] | None = None,
        search_index: dict[str, list[dict[str, Any]]] | None = None,
        deletable: set[tuple[str, str]] | None = None,
    ) -> None:
        self.banks = banks
        self.docs_by_bank = docs_by_bank or {}
        self.docs_by_id = docs_by_id or {}
        self.search_index = search_index or {}
        self.deletable = deletable or set()

    async def list_banks(self) -> list[str]:
        return list(self.banks)

    async def get_documents(
        self, user_id: str, document_id: str = "", tags: list[str] | None = None,
        tags_match: str = "any_strict", limit: int = 20, **kw: Any,
    ) -> list[dict[str, Any]]:
        if document_id:
            doc = self.docs_by_id.get(user_id, {}).get(document_id)
            return [dict(doc)] if doc else []
        items = self.docs_by_bank.get(user_id, [])
        if tags:
            wanted = set(tags)
            items = [d for d in items if wanted & {str(t) for t in (d.get("tags") or [])}]
        return [dict(d) for d in items[:limit]]

    async def search(self, query: str, user_id: str, top_k: int = 5, memory_type: str | None = None, **kw: Any) -> list[dict[str, Any]]:
        hits = [
            dict(m)
            for m in self.search_index.get(user_id, [])
            if query and query.strip() in str(m.get("content", ""))
        ]
        return hits[:top_k]

    async def delete(self, user_id: str, memory_id: str | None = None) -> bool:
        return (user_id, str(memory_id)) in self.deletable


def _doc(id_: str, text: str, mtype: str = "semantic", created_at: str = "2026-09-20T00:00:00Z") -> dict[str, Any]:
    """sidecar documents 面条目（retain 注入 type:* 服务端标签）。"""
    return {
        "id": id_,
        "original_text": text,
        "tags": [f"type:{mtype}"],
        "document_metadata": {"memory_type": mtype},
        "created_at": created_at,
    }


def _mem(id_: str, content: str, score: float = 1.0) -> dict[str, Any]:
    """后端统一形态记忆条目（search 通路）。"""
    return {"id": id_, "content": content, "score": score, "memory_type": "semantic", "metadata": {}}


def _full_backend() -> StubBackend:
    """三人世界替身：alice/bob 各持会话 bank，default bank 存旧共享记忆。"""
    return StubBackend(
        banks=["thread-alice", "thread-bob", "default"],
        docs_by_bank={
            "thread-alice": [
                _doc("mem-alice-1", "alice专属记忆蓝鲸", "semantic"),
                _doc("ep-alice-1", "alice情景", "episode"),
            ],
            "thread-bob": [
                _doc("mem-bob-1", "bob专属记忆海豚", "semantic"),
                _doc("ep-bob-1", "bob情景", "episode"),
            ],
            "default": [_doc("mem-default-1", "default旧共享记忆", "semantic")],
        },
        docs_by_id={
            "thread-alice": {"mem-alice-1": _doc("mem-alice-1", "alice专属记忆蓝鲸")},
            "thread-bob": {"mem-bob-1": _doc("mem-bob-1", "bob专属记忆海豚")},
            "default": {"mem-default-1": _doc("mem-default-1", "default旧共享记忆")},
        },
        search_index={
            "thread-alice": [_mem("mem-alice-1", "alice专属记忆蓝鲸")],
            "thread-bob": [_mem("mem-bob-1", "bob专属记忆海豚")],
            "default": [_mem("mem-default-1", "default旧共享记忆")],
        },
        deletable={
            ("thread-alice", "mem-alice-1"),
            ("thread-bob", "mem-bob-1"),
            ("default", "mem-default-1"),
        },
    )


# ═══════════════════════════════════════════════════════════
# resolve_caller_banks：身份头 → 可见 bank 集合（fail-closed 矩阵）
# ═══════════════════════════════════════════════════════════


class TestResolveCallerBanks:
    @pytest.fixture
    def routes_mod(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        mod = _load_routes("hindsight_routes_identity_unit")
        yield mod
        mod.set_memory_backend(None)
        sys.modules.pop("hindsight_routes_identity_unit", None)

    def test_missing_identity_headers_fail_closed(
        self, routes_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺 x-agentos-tenant / x-agentos-user 任一头 → 空集（绝不返回全量）。"""
        monkeypatch.setattr(routes_mod, "db_read_query", FakeDbRead(_SESSIONS_ROWS).query)
        routes_mod.set_memory_backend(_full_backend())

        for headers in (
            None,
            {},
            {"x-agentos-tenant": _TENANT_ALICE},
            {"x-agentos-user": "u-alice"},
        ):
            banks = _run(routes_mod.resolve_caller_banks(None, headers, routes_mod._get_memory_backend()))
            assert banks == [], headers

    def test_owned_session_bank_visible_foreign_invisible(
        self, routes_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """alice 只见 thread-alice；thread-bob（他人会话）不可见；非 admin 不见 default。"""
        monkeypatch.setattr(routes_mod, "db_read_query", FakeDbRead(_SESSIONS_ROWS).query)
        routes_mod.set_memory_backend(_full_backend())

        banks = _run(routes_mod.resolve_caller_banks(
            None, _identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice"), routes_mod._get_memory_backend()
        ))
        assert banks == ["thread-alice"]

    def test_mutual_invisibility_between_users(
        self, routes_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """bob 换身份：只见 thread-bob，与 alice 集合互斥。"""
        monkeypatch.setattr(routes_mod, "db_read_query", FakeDbRead(_SESSIONS_ROWS).query)
        routes_mod.set_memory_backend(_full_backend())

        banks = _run(routes_mod.resolve_caller_banks(
            None, _identity_headers(_TENANT_BOB, "u-bob", "Bearer-bob"), routes_mod._get_memory_backend()
        ))
        assert banks == ["thread-bob"]

    def test_admin_tenant_sees_default_bank(
        self, routes_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """admin 种子租户（tenant=default）：default bank 可见 + 自己的会话 bank。"""
        admin_rows = [["thread-admin-1", _TENANT_ADMIN]]
        monkeypatch.setattr(routes_mod, "db_read_query", FakeDbRead(admin_rows).query)
        # 发现面含他人 bank（thread-bob）——不在 admin 会话行内，收窄后不可见
        routes_mod.set_memory_backend(StubBackend(banks=["thread-admin-1", "thread-bob"]))

        banks = _run(routes_mod.resolve_caller_banks(
            None, _identity_headers(_TENANT_ADMIN, "u-admin", "Bearer-admin"), routes_mod._get_memory_backend()
        ))
        assert banks == ["thread-admin-1", "default"]

    def test_non_admin_tenant_default_bank_invisible(
        self, routes_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """普通用户（tenant≠default）：default bank 不可见（M-9 恒查收敛）。"""
        rows = [list(r) for r in _SESSIONS_ROWS] + [["thread-alice-2", _TENANT_ALICE]]
        monkeypatch.setattr(routes_mod, "db_read_query", FakeDbRead(rows).query)
        routes_mod.set_memory_backend(
            StubBackend(banks=["thread-alice", "thread-alice-2", "thread-bob", "default"])
        )

        banks = _run(routes_mod.resolve_caller_banks(
            None, _identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice"), routes_mod._get_memory_backend()
        ))
        assert "default" not in banks
        assert banks == ["thread-alice", "thread-alice-2"]

    def test_dbread_failure_session_banks_fail_closed(
        self, routes_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """db-read 查询失败 → 会话 bank 全部不可见；admin 仍见 default（身份头可判）。"""
        monkeypatch.setattr(routes_mod, "db_read_query", FakeDbRead([], fail=True).query)
        routes_mod.set_memory_backend(_full_backend())

        banks = _run(routes_mod.resolve_caller_banks(
            None, _identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice"), routes_mod._get_memory_backend()
        ))
        assert banks == []

        admin_banks = _run(routes_mod.resolve_caller_banks(
            None, _identity_headers(_TENANT_ADMIN, "u-admin", "Bearer-admin"), routes_mod._get_memory_backend()
        ))
        assert admin_banks == ["default"]

    def test_missing_authorization_session_banks_fail_closed(
        self, routes_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """有身份头但无 Authorization 原头 → 内核复权无锚，会话 bank 不可见。"""
        monkeypatch.setattr(routes_mod, "db_read_query", FakeDbRead(_SESSIONS_ROWS).query)
        routes_mod.set_memory_backend(_full_backend())

        banks = _run(routes_mod.resolve_caller_banks(
            None, _identity_headers(_TENANT_ALICE, "u-alice"), routes_mod._get_memory_backend()
        ))
        assert banks == []

    def test_backend_without_bank_discovery_degrades_to_default_only(
        self, routes_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """后端无 list_banks（最小替身）→ 无会话 bank；default 按租户判定。"""
        monkeypatch.setattr(routes_mod, "db_read_query", FakeDbRead(_SESSIONS_ROWS).query)
        routes_mod.set_memory_backend(SimpleNamespace())

        banks = _run(routes_mod.resolve_caller_banks(
            None, _identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice"), routes_mod._get_memory_backend()
        ))
        assert banks == []

        admin_banks = _run(routes_mod.resolve_caller_banks(
            None, _identity_headers(_TENANT_ADMIN, "u-admin", "Bearer-admin"), routes_mod._get_memory_backend()
        ))
        assert admin_banks == ["default"]

    def test_dbread_call_contract(
        self, routes_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """db-read 调用契约：sessions 面、租户列投影、Authorization 原头转发。"""
        db = FakeDbRead(_SESSIONS_ROWS)
        monkeypatch.setattr(routes_mod, "db_read_query", db.query)
        routes_mod.set_memory_backend(_full_backend())

        _run(routes_mod.resolve_caller_banks(
            None, _identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice"), routes_mod._get_memory_backend()
        ))
        face, sql, params, authorization = db.calls[0]
        assert face == "sessions"
        assert "tenant_id" in sql  # 收窄查询须投影真实租户列（内核 422 拒保护）
        assert authorization == "Bearer-alice"
        assert params is not None
        assert "thread-alice" in params.values()


# ═══════════════════════════════════════════════════════════
# dispatch 端到端：list / detail / search / episodes / stats / recall
# ═══════════════════════════════════════════════════════════


def _decode_http(out: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """http.handle 出参 → (HTTP 状态, 解码后 body)。"""
    data = out["data"]
    body = json.loads(base64.b64decode(data["body"]).decode("utf-8"))
    return int(data["status"]), body


class FakeHindsightClient:
    """内存 hindsight 客户端：arecall 按 bank 返回（跨 bank 结果可区分）。"""

    def __init__(self, results_by_bank: dict[str, list[dict[str, Any]]]) -> None:
        self.results_by_bank = results_by_bank
        self.called_banks: list[str] = []

    async def arecall(self, **kwargs: Any) -> Any:
        bank = kwargs["bank_id"]
        self.called_banks.append(bank)
        return SimpleNamespace(results=[dict(item) for item in self.results_by_bank.get(bank, [])])


def _recall_item(id_: str, bank: str, score: float) -> dict[str, Any]:
    return {"id": id_, "content": f"{bank} 的记忆", "scores": {"final": score}}


_RECALL_RESULTS = {
    "thread-alice": [_recall_item("r-alice", "alice", 0.9)],
    "thread-bob": [_recall_item("r-bob", "bob", 0.95)],
    "default": [_recall_item("r-default", "default", 0.8)],
}


@pytest.fixture
def dispatch(monkeypatch: pytest.MonkeyPatch) -> Any:
    """端到端测试台：canonical routes_memory（server 分发解析名）+ fresh server。"""
    import routes_memory as mod  # 裸名导入 = server 分发解析到的同一模块

    srv = _load_server("hindsight_server_identity_dispatch")
    db = FakeDbRead(_SESSIONS_ROWS)
    mod.set_memory_backend(_full_backend())
    monkeypatch.setattr(mod, "db_read_query", db.query)
    srv._memory_backend = mod._get_memory_backend()
    srv._memory_backend_attempted = True
    yield SimpleNamespace(srv=srv, mod=mod, db=db)
    mod.set_memory_backend(None)
    srv._client = None
    srv._memory_backend = None
    srv._memory_backend_attempted = False


def _recall_client(d: Any) -> FakeHindsightClient:
    client = FakeHindsightClient(_RECALL_RESULTS)
    d.srv._client = client
    return client


class TestMemoryDomainIdentityDispatch:
    def test_list_scoped_to_owned_banks(self, dispatch: Any) -> None:
        """list：alice 只见 thread-alice 条目；bob 互斥；无身份头 → 空集。"""
        prefix = "/ext/hindsight_memory_service/memory"

        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=prefix, method="GET", plugin_id="hindsight_memory_service",
            headers=_identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice"),
        )))
        assert status == 200
        assert {item["id"] for item in body["items"]} == {"mem-alice-1", "ep-alice-1"}

        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=prefix, method="GET", plugin_id="hindsight_memory_service",
            headers=_identity_headers(_TENANT_BOB, "u-bob", "Bearer-bob"),
        )))
        assert {item["id"] for item in body["items"]} == {"mem-bob-1", "ep-bob-1"}

        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=prefix, method="GET", plugin_id="hindsight_memory_service", headers=None,
        )))
        assert status == 200
        assert body == {"items": [], "total": 0}

    def test_detail_owned_hit_and_foreign_404(self, dispatch: Any) -> None:
        """detail：本人记忆可查看；他人 bank 的 id 结构化 404（跨户不可读）。"""
        prefix = "/ext/hindsight_memory_service/memory"
        alice = _identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice")

        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=f"{prefix}/mem-alice-1", method="GET", plugin_id="hindsight_memory_service", headers=alice,
        )))
        assert status == 200
        assert body["id"] == "mem-alice-1"

        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=f"{prefix}/mem-bob-1", method="GET", plugin_id="hindsight_memory_service", headers=alice,
        )))
        assert status == 404
        assert body["detail"] == "未找到相关记忆"

    def test_delete_foreign_bank_404(self, dispatch: Any) -> None:
        """delete：他人 bank 的 id 不可删（404），本人可删。"""
        prefix = "/ext/hindsight_memory_service/memory"
        alice = _identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice")

        status, _body = _decode_http(_run(dispatch.srv.http_handle(
            path=f"{prefix}/mem-bob-1", method="DELETE", plugin_id="hindsight_memory_service", headers=alice,
        )))
        assert status == 404

        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path=f"{prefix}/mem-alice-1", method="DELETE", plugin_id="hindsight_memory_service", headers=alice,
        )))
        assert status == 200
        assert body == {"message": "记忆已删除"}

    def test_search_scoped_to_owned_banks(self, dispatch: Any) -> None:
        """search：query 同时命中多 bank 内容时只回归属内条目。"""
        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path="/ext/hindsight_memory_service/memory/search", method="GET",
            plugin_id="hindsight_memory_service",
            headers=_identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice"),
            query={"query": "记忆", "top_k": "5"},
        )))
        assert status == 200
        assert {item["id"] for item in body["items"]} == {"mem-alice-1"}

    def test_episodes_scoped_to_owned_banks(self, dispatch: Any) -> None:
        """episodes 列表 + 详情：只见归属内情景记忆。"""
        alice = _identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice")

        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path="/ext/hindsight_memory_service/memory/episodes", method="GET",
            plugin_id="hindsight_memory_service", headers=alice,
        )))
        assert status == 200
        assert {item["id"] for item in body["items"]} == {"ep-alice-1"}

        status, _body = _decode_http(_run(dispatch.srv.http_handle(
            path="/ext/hindsight_memory_service/memory/episodes/ep-bob-1", method="GET",
            plugin_id="hindsight_memory_service", headers=alice,
        )))
        assert status == 404

    def test_stats_scoped_to_owned_banks(self, dispatch: Any) -> None:
        """stats：alice 计数只含自己的条目；无身份头 → 全 0。"""
        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path="/ext/hindsight_memory_service/memory/stats", method="GET",
            plugin_id="hindsight_memory_service",
            headers=_identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice"),
        )))
        assert status == 200
        assert body == {"episode_count": 1, "knowledge_count": 1, "total_count": 2, "last_updated": ""}

        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path="/ext/hindsight_memory_service/memory/stats", method="GET",
            plugin_id="hindsight_memory_service", headers=None,
        )))
        assert status == 200
        assert body["total_count"] == 0

    def test_semantic_scoped_to_owned_banks(self, dispatch: Any) -> None:
        """semantic：跨户条目不出现。"""
        status, body = _decode_http(_run(dispatch.srv.http_handle(
            path="/ext/hindsight_memory_service/memory/semantic", method="GET",
            plugin_id="hindsight_memory_service",
            headers=_identity_headers(_TENANT_BOB, "u-bob", "Bearer-bob"),
        )))
        assert status == 200
        assert [item["id"] for item in body["items"]] == ["mem-bob-1"]


class TestRecallEndpointIdentity:
    def test_recall_scoped_to_owned_banks(self, dispatch: Any) -> None:
        """alice recall：只见自己会话 bank 的结果；不触他人 bank 与 default。"""
        client = _recall_client(dispatch)

        out = _run(dispatch.srv.http_handle(
            path="/ext/hindsight_memory_service/recall", method="GET",
            plugin_id="hindsight_memory_service",
            headers=_identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice"),
            query={"query": "记忆", "limit": "10"},
        ))
        status, body = _decode_http(out)
        assert status == 200
        assert {item["id"] for item in body["results"]} == {"r-alice"}
        assert client.called_banks == ["thread-alice"]

    def test_recall_no_identity_empty(self, dispatch: Any) -> None:
        """无身份头 → 空结果（fail-closed），且不触任何 bank。"""
        client = _recall_client(dispatch)

        out = _run(dispatch.srv.http_handle(
            path="/ext/hindsight_memory_service/recall", method="GET",
            plugin_id="hindsight_memory_service", headers=None,
            query={"query": "记忆", "limit": "10"},
        ))
        status, body = _decode_http(out)
        assert status == 200
        assert body == {"results": [], "total": 0}
        assert client.called_banks == []

    def test_recall_admin_sees_default_bank(self, dispatch: Any) -> None:
        """admin 租户：default bank 结果可见（恒查 default 收敛为按身份可查）。"""
        client = _recall_client(dispatch)

        out = _run(dispatch.srv.http_handle(
            path="/ext/hindsight_memory_service/recall", method="GET",
            plugin_id="hindsight_memory_service",
            headers=_identity_headers(_TENANT_ADMIN, "u-admin", "Bearer-admin"),
            query={"query": "记忆", "limit": "10"},
        ))
        status, body = _decode_http(out)
        assert status == 200
        assert {item["id"] for item in body["results"]} == {"r-default"}
        assert client.called_banks == ["default"]

    def test_recall_empty_query_rejected_without_bank_touch(self, dispatch: Any) -> None:
        """空 query 维持契约拒绝形态（error 提示），不触发 bank 扇出。"""
        client = _recall_client(dispatch)

        out = _run(dispatch.srv.http_handle(
            path="/ext/hindsight_memory_service/recall", method="GET",
            plugin_id="hindsight_memory_service",
            headers=_identity_headers(_TENANT_ALICE, "u-alice", "Bearer-alice"),
            query={"query": " ", "limit": "10"},
        ))
        status, body = _decode_http(out)
        assert status == 200
        assert body["results"] == []
        assert "query is required" in body.get("error", "")
        assert client.called_banks == []
