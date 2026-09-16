# @feature: FP-0.2.可观测性 可观测性基座 | @ci: python-coverage
"""monitoring /ext/monitoring/** HTTP 路由分发 + on_load provider 桥 + 指标上报测试。

既有用例直接测业务帮助函数；本文件补路由分发层（http_handle 域分发、
query 解析回退、错误信封契约）与 on_load 注入的 kernel_reads provider 闭包。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

_SERVER_PY = (
    Path(__file__).resolve().parents[2] / "plugins" / "shared" / "system" / "monitoring" / "server.py"
)
_spec = importlib.util.spec_from_file_location("monitoring_server_routes_ut", _SERVER_PY)
server = importlib.util.module_from_spec(_spec)
sys.modules["monitoring_server_routes_ut"] = server
_spec.loader.exec_module(server)


def _data(resp: dict[str, Any], status: int = 200) -> Any:
    """解码 _ok(json_response(payload, status)) 信封的 body（域内错误也走此形态）。"""
    assert resp["success"] is True
    data = resp["data"]
    assert data["status"] == status
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]))


def _top_error_status(resp: dict[str, Any]) -> int:
    """顶层 _error 信封（success False）携带的 HTTP 状态。"""
    assert resp["success"] is False
    return resp["data"]["status"]


class _Handle:
    """能力句柄替身：记录 call 并按 method 返回预设值。"""

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._results = results or {}

    async def call(self, method: str, params: dict) -> Any:
        self.calls.append((method, params))
        v = self._results.get(method)
        if isinstance(v, Exception):
            raise v
        return v


def _stub_module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


class Recorder:
    """异步记录器：记 (args, kwargs) 并返回预设值或抛预设异常。"""

    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self._result = result
        self._exc = exc

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self._exc is not None:
            raise self._exc
        return self._result


# ═══════════════════════════════════════════════════════════
# /ext/monitoring/tasks：分页与 query 回退
# ═══════════════════════════════════════════════════════════


class TestTasksRoute:
    async def test_pagination_slices_items(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, str | None] = {}

        async def fake_collect(status: str | None = None) -> list[dict]:
            seen["status"] = status
            return [{"task.id": f"t{i}"} for i in range(25)]

        monkeypatch.setattr(server, "_collect_state_tasks", fake_collect)

        resp = await server.http_handle(
            path="/ext/monitoring/tasks", method="GET", query={"page": "2", "page_size": "10", "status": "running"}
        )
        body = _data(resp)
        assert [r["task.id"] for r in body["items"]] == [f"t{i}" for i in range(10, 20)]
        assert body["total"] == 25
        assert body["page"] == 2
        assert body["page_size"] == 10
        assert seen["status"] == "running"

    async def test_illegal_page_params_fall_back_and_empty_status_becomes_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, str | None] = {}

        async def fake_collect(status: str | None = None) -> list[dict]:
            seen["status"] = status
            return [{"task.id": "t0"}]

        monkeypatch.setattr(server, "_collect_state_tasks", fake_collect)
        resp = await server.http_handle(
            path="/ext/monitoring/tasks",
            method="GET",
            query={"page": "abc", "page_size": "xyz", "status": ""},
        )
        body = _data(resp)
        assert body["page"] == 1
        # 性质断言：page_size 无论输入如何都收敛到 [1, 200]
        assert 1 <= body["page_size"] <= 200
        assert seen["status"] is None  # 空串过滤视为不过滤

    async def test_huge_page_size_capped_at_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_collect(status: str | None = None) -> list[dict]:
            return []

        monkeypatch.setattr(server, "_collect_state_tasks", fake_collect)
        body = _data(
            await server.http_handle(path="/ext/monitoring/tasks", method="GET", query={"page_size": "9999"})
        )
        assert body["page_size"] == 200


# ═══════════════════════════════════════════════════════════
# traces / pipeline-state / orphans / payload-diag 路由
# ═══════════════════════════════════════════════════════════


class TestDiagnosticRoutes:
    async def test_traces_requires_pipeline_id(self) -> None:
        resp = await server.http_handle(path="/ext/monitoring/traces", method="GET", query={})
        assert _top_error_status(resp) == 400

    async def test_traces_limit_falls_back_on_garbage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        diag = Recorder(result={"traces": []})
        monkeypatch.setitem(
            sys.modules, "pipeline_diagnostics", _stub_module("pipeline_diagnostics", list_pipeline_traces=diag)
        )
        body = _data(
            await server.http_handle(
                path="/ext/monitoring/traces",
                method="GET",
                query={"pipeline_id": "p1", "limit": "not-a-number"},
            )
        )
        assert body == {"traces": []}
        assert diag.calls == [((), {"pipeline_id": "p1", "limit": 200})]

    async def test_pipeline_state_requires_pipeline_id(self) -> None:
        resp = await server.http_handle(path="/ext/monitoring/pipeline-state", method="GET", query={})
        assert _top_error_status(resp) == 400

    async def test_pipeline_state_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        getter = Recorder(result={"pipeline_id": "p9", "state": {"k": "v"}})
        monkeypatch.setitem(
            sys.modules, "pipeline_diagnostics", _stub_module("pipeline_diagnostics", get_pipeline_state_full=getter)
        )
        body = _data(
            await server.http_handle(path="/ext/monitoring/pipeline-state", method="GET", query={"pipeline_id": "p9"})
        )
        assert body["pipeline_id"] == "p9"
        assert getter.calls == [(("p9",), {})]

    async def test_orphans_param_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        orphans = Recorder(result=[])
        monkeypatch.setitem(
            sys.modules, "execution_records", _stub_module("execution_records", list_orphan_runs=orphans)
        )
        _data(await server.http_handle(path="/ext/monitoring/orphans", method="GET", query={}))
        assert orphans.calls[-1][1] == {"min_minutes": 10, "limit": 50}

        await server.http_handle(
            path="/ext/monitoring/orphans", method="GET", query={"min_minutes": "abc", "limit": "7"}
        )
        assert orphans.calls[-1][1] == {"min_minutes": 10, "limit": 7}  # 非法 min_minutes 回退，合法 limit 生效

    async def test_payload_diag_routes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server, "_list_payload_diag", lambda: [{"name": "a.json", "ts": 1}])
        body = _data(await server.http_handle(path="/ext/monitoring/payload-diag", method="GET", query={}))
        assert body == {"items": [{"name": "a.json", "ts": 1}], "total": 1}

        monkeypatch.setattr(server, "_read_payload_diag", lambda name: {"name": name, "content": "{}"})
        body = _data(
            await server.http_handle(
                path="/ext/monitoring/payload-diag/file", method="GET", query={"name": "a.json"}
            )
        )
        assert body == {"name": "a.json", "content": "{}"}

    async def test_plugin_runtime_bridge_passes_authorization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        runtime = Recorder(result={"rows": []})
        monkeypatch.setattr(server.kernel_reads, "plugin_runtime", runtime)
        await server.http_handle(
            path="/ext/monitoring/plugins", method="GET", headers={"Authorization": "Bearer tok"}
        )
        assert runtime.calls == [((), {"authorization": "Bearer tok"})]


class TestKernelReadsPluginRuntime:
    """kernel_reads.plugin_runtime 真实执行：行组装 + lifecycle 聚合（改行回归锚）。"""

    async def test_assembles_rows_and_lifecycle_from_providers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        list_provider = Recorder(result={"series": [
            {"name": "process.alive", "plugin_id": "bash", "latest": 1},
            {"name": "process.pid", "plugin_id": "bash", "latest": 4242},
            {"name": "process.memory_rss_bytes", "plugin_id": "bash", "latest": 157286400},
            {"name": "process.uptime_seconds", "plugin_id": "bash", "latest": 90.0},
            {"name": "process.last_crash_ts", "plugin_id": "bash", "latest": 0},
            # 非进程 gauge / 非字符串 plugin_id 行被剔除
            {"name": "lifecycle.plugin_load_total", "plugin_id": "bash", "latest": 1},
            {"name": "process.alive", "plugin_id": 7, "latest": 1},
        ]})
        query_provider = Recorder(result={"metrics": [
            {"name": "lifecycle.plugin_load_total",
             "samples": [{"value": 3}, {"value": 4.5}, "junk"]},
            {"name": "lifecycle.plugin_error_total",
             "samples": [{"value": 2}]},
            {"name": "system.cpu_usage_ratio", "samples": [{"value": 9}]},  # 非 lifecycle 不计
        ]})
        monkeypatch.setattr(
            server.kernel_reads, "_PROVIDERS",
            {"metrics-admin-list": list_provider, "metrics-admin-query": query_provider},
        )

        result = await server.kernel_reads.plugin_runtime(authorization="Bearer tok")

        assert list_provider.calls == [((), {"authorization": "Bearer tok"})]
        assert query_provider.calls == [((), {"authorization": "Bearer tok", "plugin_id": "kernel"})]
        assert result["total"] == 1
        row = result["rows"][0]
        assert row["plugin_id"] == "bash"
        assert row["status"] == "running"
        assert row["alive"] == 1
        assert row["pid"] == 4242
        assert row["memory_rss_mb"] == 150.0  # 157286400 / 1MiB
        assert row["uptime_seconds"] == 90.0
        assert row["last_crash_ts"] == 0  # gauge 缺失/零值归 0
        assert result["lifecycle"] == {"plugin_load_total": 7, "plugin_error_total": 2}

    async def test_missing_providers_degrade_to_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server.kernel_reads, "_PROVIDERS", {})
        result = await server.kernel_reads.plugin_runtime(authorization="")
        assert result["total"] == 0
        assert result["rows"] == []
        assert result["lifecycle"] == {"plugin_load_total": 0, "plugin_error_total": 0}


# ═══════════════════════════════════════════════════════════
# execution / sessions / search 域分发
# ═══════════════════════════════════════════════════════════


class TestExecutionDomain:
    async def test_non_execution_path_404(self) -> None:
        resp = await server._handle_execution_domain("/ext/other", "GET", "", {}, {})
        assert _data(resp, 404) == {"error": "not an execution path", "path": "/ext/other"}

    async def test_unexpected_error_maps_to_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        boom = Recorder(exc=RuntimeError("db exploded"))
        monkeypatch.setitem(
            sys.modules, "execution_records", _stub_module("execution_records", list_execution_records=boom)
        )
        resp = await server.http_handle(path="/ext/monitoring/execution/records", method="GET", query={})
        body = _data(resp, 500)
        assert "db exploded" in body["detail"]


class TestSessionsDomain:
    @pytest.fixture
    def empty_runs_db(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """空 runs 表的临时内核库：一切 session 视为本租户（无冲突判据）。"""
        db = tmp_path / "kernel.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE runs (pipeline_id TEXT, tenant_id TEXT)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(server, "_kernel_db_path", lambda: str(db))
        return db

    async def test_missing_tenant_identity_403(self) -> None:
        resp = await server._handle_sessions_domain(
            "/ext/monitoring/sessions/s1/total-token-usage", "GET", "", {}, {}
        )
        assert _data(resp, 403) == {"error": "missing tenant identity"}

    async def test_non_sessions_path_404(self) -> None:
        resp = await server._handle_sessions_domain("/ext/other", "GET", "", {}, {"x-agentos-tenant": "t1"})
        assert _data(resp, 404)["error"] == "not a sessions path"

    async def test_total_token_usage_owned_session(self, monkeypatch: pytest.MonkeyPatch, empty_runs_db: Path) -> None:
        usage = Recorder(result={"total_tokens": 42})
        monkeypatch.setitem(
            sys.modules,
            "execution_records",
            _stub_module("execution_records", get_session_total_token_usage=usage),
        )
        body = _data(
            await server._handle_sessions_domain(
                "/ext/monitoring/sessions/s1/total-token-usage",
                "GET",
                "",
                {},
                {"x-agentos-tenant": "t1"},
            )
        )
        assert body == {"total_tokens": 42}
        assert usage.calls == [(("s1",), {})]

    async def test_cross_tenant_session_404(self, monkeypatch: pytest.MonkeyPatch, empty_runs_db: Path) -> None:
        conn = sqlite3.connect(empty_runs_db)
        conn.execute("INSERT INTO runs VALUES ('s1', 'other-tenant')")
        conn.commit()
        conn.close()
        usage = Recorder(result={"total_tokens": 42})
        monkeypatch.setitem(
            sys.modules,
            "execution_records",
            _stub_module("execution_records", get_session_total_token_usage=usage),
        )
        resp = await server._handle_sessions_domain(
            "/ext/monitoring/sessions/s1/total-token-usage", "GET", "", {}, {"x-agentos-tenant": "t1"}
        )
        assert _data(resp, 404) == {"error": "not found", "path": "/ext/monitoring/sessions/s1/total-token-usage"}
        assert usage.calls == []  # 拒绝在业务查询前，不泄露存在性

    async def test_context_token_usage_passes_parent(self, monkeypatch: pytest.MonkeyPatch, empty_runs_db: Path) -> None:
        usage = Recorder(result={"total_tokens": 7})
        monkeypatch.setitem(
            sys.modules,
            "execution_records",
            _stub_module("execution_records", get_session_context_token_usage=usage),
        )
        body = _data(
            await server._handle_sessions_domain(
                "/ext/monitoring/sessions/s2/context-token-usage",
                "GET",
                "",
                {"parent_execution_record_id": "rec-9"},
                {"x-agentos-tenant": "t1"},
            )
        )
        assert body == {"total_tokens": 7}
        assert usage.calls == [(("s2",), {"parent_execution_record_id": "rec-9"})]

    async def test_unknown_sub_404_and_error_500(self, monkeypatch: pytest.MonkeyPatch, empty_runs_db: Path) -> None:
        headers = {"x-agentos-tenant": "t1"}
        resp = await server._handle_sessions_domain("/ext/monitoring/sessions/bogus", "GET", "", {}, headers)
        _data(resp, 404)

        usage = Recorder(exc=RuntimeError("boom"))
        monkeypatch.setitem(
            sys.modules,
            "execution_records",
            _stub_module("execution_records", get_session_total_token_usage=usage),
        )
        resp = await server._handle_sessions_domain(
            "/ext/monitoring/sessions/s1/total-token-usage", "GET", "", {}, headers
        )
        _data(resp, 500)


class TestSearchDomain:
    def _patch_search(self, monkeypatch: pytest.MonkeyPatch, result: Any = None, exc: Exception | None = None):
        stub = Recorder(result=result, exc=exc)
        monkeypatch.setitem(sys.modules, "routes_search", _stub_module("routes_search", search=stub))
        return stub

    async def test_non_search_path_404(self) -> None:
        resp = await server._handle_search_domain("/ext/other", "GET", "", {}, {})
        assert _data(resp, 404)["error"] == "not a search path"

    async def test_search_params_and_tenant_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = self._patch_search(monkeypatch, result={"items": []})
        body = _data(
            await server._handle_search_domain(
                "/ext/monitoring/search",
                "GET",
                "",
                {"q": "贪吃蛇", "type": "session", "limit": "7"},
                {"x-agentos-tenant": "t1"},
            )
        )
        assert body == {"items": []}
        assert stub.calls == [((), {"q": "贪吃蛇", "type": "session", "limit": 7, "tenant_id": "t1"})]

    async def test_invalid_type_maps_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_search(monkeypatch, exc=ValueError("type 非法"))
        resp = await server._handle_search_domain(
            "/ext/monitoring/search", "GET", "", {"q": "x", "type": "bogus"}, {"x-agentos-tenant": "t1"}
        )
        _data(resp, 422)

    async def test_unexpected_error_maps_500_and_unknown_sub_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_search(monkeypatch, exc=RuntimeError("boom"))
        resp = await server._handle_search_domain(
            "/ext/monitoring/search", "GET", "", {"q": "x"}, {"x-agentos-tenant": "t1"}
        )
        _data(resp, 500)

        self._patch_search(monkeypatch, result={})
        resp = await server._handle_search_domain(
            "/ext/monitoring/search/bogus", "GET", "", {}, {"x-agentos-tenant": "t1"}
        )
        _data(resp, 404)


# ═══════════════════════════════════════════════════════════
# payload_diag / 租户冲突 / tool-calls 帮助函数错误分支
# ═══════════════════════════════════════════════════════════


class TestPayloadDiagHelpers:
    def test_list_skips_files_whose_stat_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        diag = tmp_path / "pd"
        diag.mkdir()
        (diag / "1700000000__model-a__beef__3msg.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(server, "_payload_diag_dir", lambda: str(diag))
        def flaky_getsize(*_a: str) -> int:
            raise OSError("vanished")

        monkeypatch.setattr("os.path.getsize", flaky_getsize)
        assert server._list_payload_diag() == []  # 单文件 stat 失败不崩、跳过

    def test_clear_counts_and_tolerates_remove_failures(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        diag = tmp_path / "pd"
        diag.mkdir()
        (diag / "a.json").write_text("{}", encoding="utf-8")
        (diag / "b.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(server, "_payload_diag_dir", lambda: str(diag))
        real_remove = __import__("os").remove

        def flaky_remove(p: str) -> None:
            if p.endswith("a.json"):
                raise OSError("locked")
            real_remove(p)

        monkeypatch.setattr("os.remove", flaky_remove)
        assert server._clear_payload_diag_files() == 1  # 失败容错计数，成功者照删

    def test_read_reports_oserror(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        diag = tmp_path / "pd"
        diag.mkdir()
        (diag / "a.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(server, "_payload_diag_dir", lambda: str(diag))

        import builtins

        real = builtins.open

        def flaky_open(file: Any, *a: Any, **kw: Any):
            if str(file).endswith("a.json"):
                raise OSError("permission denied")
            return real(file, *a, **kw)

        monkeypatch.setattr(builtins, "open", flaky_open)
        result = server._read_payload_diag("a.json")
        assert "permission denied" in result["error"]

    def test_project_root_fallback_when_no_marker_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import os

        monkeypatch.setattr(os.path, "isdir", lambda _p: False)
        assert server._resolve_project_root() == os.path.dirname(server.__file__)


class TestTenantConflictAndToolCalls:
    def _db(self, tmp_path: Path) -> Path:
        db = tmp_path / "kernel.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE runs (pipeline_id TEXT, tenant_id TEXT)")
        conn.execute("INSERT INTO runs VALUES ('p1', 't-other')")
        conn.commit()
        conn.close()
        return db

    def test_conflict_db_missing_or_error_is_false(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(server, "_kernel_db_path", lambda: str(tmp_path / "nope.db"))
        assert server._pipeline_tenant_conflict("p1", "t1") is False  # 库缺失：无判据放行

        db = self._db(tmp_path)
        monkeypatch.setattr(server, "_kernel_db_path", lambda: str(db))
        def flaky_connect(*_a: Any, **_kw: Any):
            raise sqlite3.Error("locked")

        monkeypatch.setattr(sqlite3, "connect", flaky_connect)
        assert server._pipeline_tenant_conflict("p1", "t1") is False  # 查询失败 fail-open（无判据）

    def test_query_tool_calls_success_and_error_filters(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db = tmp_path / "kernel.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE traces (trace_id TEXT, run_id TEXT, created_at TEXT, patch_data TEXT, tenant_id TEXT)"
        )
        ok_row = '{"tool_results":[{"tool_name":"bash","success":1,"error":null,"duration_ms":120.0}]}'
        conn.execute(
            "INSERT INTO traces VALUES ('tr1','r1','2026-09-13T00:00:00Z',?, 't1')", (ok_row,)
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(server, "_kernel_db_path", lambda: str(db))

        result = server._query_tool_calls({"status": "success"}, tenant_id="t1")
        assert result["total"] == 1
        assert result["items"][0]["tool_name"] == "bash"
        assert result["items"][0]["success"] == 1

        assert server._query_tool_calls({"status": "error"}, tenant_id="t1")["total"] == 0
        # 非法 min_duration 只忽略过滤，不改变结果集（SQL 绑定数不变）
        assert server._query_tool_calls({"min_duration": "abc"}, tenant_id="t1")["total"] == 1
        assert server._query_tool_calls({}, tenant_id="t1")["total"] == 1

    def test_query_tool_calls_sqlite_error_degrades(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        db = self._db(tmp_path)
        monkeypatch.setattr(server, "_kernel_db_path", lambda: str(db))
        def bad_connect(*_a: Any, **_kw: Any) -> None:
            raise sqlite3.Error("bad db")

        monkeypatch.setattr(sqlite3, "connect", bad_connect)
        result = server._query_tool_calls({}, tenant_id="t1")
        assert result["total"] == 0
        assert "bad db" in result["error"]

    def test_query_tool_calls_fail_closed_without_tenant(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(server, "_kernel_db_path", lambda: str(self._db(tmp_path)))
        assert server._query_tool_calls({}, tenant_id="") == {
            "items": [],
            "total": 0,
            "error": "missing tenant identity",
        }


# ═══════════════════════════════════════════════════════════
# token 聚合 DB 错误降级
# ═══════════════════════════════════════════════════════════


class TestTokenUsageDegradation:
    @pytest.fixture
    def local_monitor(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        monitor = types.SimpleNamespace(_llm_stats={"request_count": 3})
        monkeypatch.setattr(server, "_monitor", monitor)
        return monitor

    @pytest.fixture
    def broken_db(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读库入口故障注入：collect 内部 `import sqlite3` 是局部绑定，
        patch 标准库模块本体（monkeypatch 用例级还原）。"""
        monkeypatch.setattr(
            sqlite3,
            "connect",
            lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.Error("bad read")),
        )

    def test_by_model_query_error_degrades_to_local_counts(
        self, broken_db: None, local_monitor: Any
    ) -> None:
        result = server._collect_token_usage()
        assert result["rows"] == []
        assert result["labels"] == []
        assert result["datasets"] == [{"label": "输入 Tokens", "data": []}, {"label": "输出 Tokens", "data": []}]
        assert result["request_count"] == 3  # 降级后仍带本地计数

    def test_by_time_query_error_degrades_to_empty(self, broken_db: None) -> None:
        result = server._collect_token_usage_by_time()
        assert result["rows"] == []


def _make_gate_sleep(n: int):
    """把 while True 轮询的 sleep 变成 reached/go 双向栅栏（同 test_isolation_service_server）。"""
    barriers = [{"reached": asyncio.Event(), "go": asyncio.Event()} for _ in range(n)]
    state = {"i": 0}

    async def fake_sleep(_seconds: float) -> None:
        i = state["i"]
        state["i"] += 1
        if i < n:
            barriers[i]["reached"].set()
            await barriers[i]["go"].wait()
        else:
            raise asyncio.CancelledError

    return barriers, fake_sleep


# ═══════════════════════════════════════════════════════════
# on_load provider 桥闭包
# ═══════════════════════════════════════════════════════════


class _StubMonitor:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


async def _noop() -> None:
    return None


class TestOnLoadProviderBridge:
    async def test_providers_translate_params_into_capability_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handle = _Handle({
            "pipeline-runs.list": [{"run": 1}],
            "messages.list": [{"msg": 1}],
            "list": "not-a-list",  # 非列表防御 → []
            "traces.list_by_pipeline": [{"t": 1}],
            "pipeline-runs.list_by_pipeline": [{"r": 1}],
            "table_query": [{"row": 1}],
            "clear_execution_data": {"deleted": 9},
            "metrics-admin.list": {"series": []},
            "query": {"metrics": []},
        })
        monkeypatch.setattr(server.plugin, "get_capability", lambda _name: handle)
        providers: dict[str, Any] = {}
        monkeypatch.setattr(server.kernel_reads, "set_provider", lambda name, fn: providers.update({name: fn}))
        monkeypatch.setitem(sys.modules, "performance_monitor", _stub_module("performance_monitor", PerformanceMonitor=_StubMonitor))
        monkeypatch.setattr(server, "_report_metrics_loop", _noop)

        await server._on_load({})

        assert server._monitor.started
        assert set(providers) == {
            "pipeline-runs", "messages", "pipeline-state", "traces", "runs-by-pipeline",
            "db-admin-query", "db-admin-clear", "metrics-admin-list", "metrics-admin-query",
        }

        assert await providers["pipeline-runs"](status=None, limit=42) == [{"run": 1}]
        assert handle.calls[-1] == ("pipeline-runs.list", {"status": "", "limit": 42})
        await providers["pipeline-runs"](status="running", limit=1)
        assert handle.calls[-1][1]["status"] == "running"

        await providers["messages"]("p1", None)
        assert handle.calls[-1] == ("messages.list", {"pipeline_id": "p1"})
        await providers["messages"]("p1", 7)
        assert handle.calls[-1][1]["limit"] == 7

        assert await providers["pipeline-state"]() == []  # 非列表返回值防御
        handle._results["list"] = [{"row": 1}]
        assert await providers["pipeline-state"]() == [{"row": 1}]

        await providers["traces"]("p1")
        assert handle.calls[-1] == ("traces.list_by_pipeline", {"pipeline_id": "p1"})
        await providers["runs-by-pipeline"]("p1")
        assert handle.calls[-1] == ("pipeline-runs.list_by_pipeline", {"pipeline_id": "p1"})

        await providers["db-admin-query"]("traces", filter=["a=x"], sort="created_at", limit=9)
        assert handle.calls[-1] == ("table_query", {"table": "traces", "limit": 9, "filter": ["a=x"], "sort": "created_at"})
        await providers["db-admin-query"]("traces")
        assert handle.calls[-1][1] == {"table": "traces", "limit": 500}  # 缺省不携带 filter/sort

        await providers["db-admin-clear"]("Bearer tok")
        assert handle.calls[-1] == ("clear_execution_data", {"_authorization": "Bearer tok"})
        await providers["db-admin-clear"]()
        assert handle.calls[-1][1] == {}

        await providers["metrics-admin-list"]("Bearer tok")
        assert handle.calls[-1] == ("list", {"_authorization": "Bearer tok"})
        await providers["metrics-admin-list"]()
        assert handle.calls[-1][1] == {}

        await providers["metrics-admin-query"]("Bearer tok", plugin_id="kernel", window="1h")
        assert handle.calls[-1] == ("query", {"window": "1h", "_authorization": "Bearer tok", "plugin": "kernel"})
        await providers["metrics-admin-query"]()
        assert handle.calls[-1][1] == {"window": "24h"}

        # 收尾：reporter 任务（noop）与 monitor 复位，不污染后续用例
        await server._on_unload({})

    async def test_provider_injection_failure_degrades_but_monitor_boots(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(name: str, fn: Any) -> None:
            raise RuntimeError("bridge broken")

        monkeypatch.setattr(server.kernel_reads, "set_provider", boom)
        monkeypatch.setitem(sys.modules, "performance_monitor", _stub_module("performance_monitor", PerformanceMonitor=_StubMonitor))
        monkeypatch.setattr(server, "_report_metrics_loop", _noop)

        await server._on_load({})  # 注入失败不崩
        assert server._monitor.started
        await server._on_unload({})


class TestOnUnloadAndReportLoop:
    async def test_unload_cancels_reporter_and_stops_monitor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hang = asyncio.Event()
        monkeypatch.setattr(server, "_reporter_task", None)

        async def hanging_loop() -> None:
            await hang.wait()

        monitor = _StubMonitor()
        monkeypatch.setattr(server, "_monitor", monitor)
        task = asyncio.create_task(hanging_loop())
        monkeypatch.setattr(server, "_reporter_task", task)

        await server._on_unload({})
        assert task.cancelled()
        assert monitor.stopped
        assert server._monitor is None
        assert server._reporter_task is None

    async def test_report_loop_survives_once_failure_then_honours_cancel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        barriers, fake_sleep = _make_gate_sleep(1)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        calls = {"n": 0}

        async def once_side_effect() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("sample failed")  # 非取消异常：循环吞掉继续
            raise asyncio.CancelledError  # 取消：循环放行退出

        monkeypatch.setattr(server, "_report_system_metrics_once", once_side_effect)

        task = asyncio.create_task(server._report_metrics_loop())
        await asyncio.wait_for(barriers[0]["reached"].wait(), timeout=5)  # 第 1 轮异常已被吞
        barriers[0]["go"].set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert task.cancelled()
        assert calls["n"] == 2



# ═══════════════════════════════════════════════════════════
# _report_system_metrics_once：采样/上报分支
# ═══════════════════════════════════════════════════════════


class TestReportSystemMetricsOnce:
    async def test_no_monitor_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server, "_monitor", None)
        await server._report_system_metrics_once()  # 不抛即通过

    async def test_sample_failure_skips_report(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monitor = types.SimpleNamespace(
            get_system_metrics=Recorder(exc=RuntimeError("psutil down")),
        )
        monkeypatch.setattr(server, "_monitor", monitor)
        recorded: list[Any] = []

        async def fake_record(*a: Any, **kw: Any) -> None:
            recorded.append((a, kw))

        monkeypatch.setattr(server.plugin, "record_metric", fake_record)
        await server._report_system_metrics_once()
        assert recorded == []

    async def test_successful_sample_reports_five_gauges(self, monkeypatch: pytest.MonkeyPatch) -> None:
        system = types.SimpleNamespace(
            cpu_usage=50.0, memory_usage=60.0, disk_usage=85.0, network_sent=10.0, network_recv=20.0
        )
        monitor = types.SimpleNamespace(get_system_metrics=Recorder(result=system))
        monkeypatch.setattr(server, "_monitor", monitor)
        recorded: list[tuple] = []

        async def fake_record(name, value, kind, labels, unit=None):
            recorded.append((name, value, kind, labels, unit))

        monkeypatch.setattr(server.plugin, "record_metric", fake_record)
        await server._report_system_metrics_once()

        names = [r[0] for r in recorded]
        assert names == [
            "system.cpu_usage_ratio", "system.memory_usage_ratio", "system.disk_usage_ratio",
            "system.network_sent_kbytes_per_sec", "system.network_recv_kbytes_per_sec",
        ]
        by_name = {r[0]: r for r in recorded}
        assert by_name["system.cpu_usage_ratio"][1] == pytest.approx(0.5)
        assert by_name["system.disk_usage_ratio"][1] == pytest.approx(0.85)
        for r in recorded:
            assert r[2] == "gauge"
            assert r[3] == {"source": "psutil"}

    async def test_record_metric_keyerror_stops_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        system = types.SimpleNamespace(
            cpu_usage=1.0, memory_usage=1.0, disk_usage=1.0, network_sent=0, network_recv=0
        )
        monkeypatch.setattr(server, "_monitor", types.SimpleNamespace(get_system_metrics=Recorder(result=system)))

        async def raise_keyerror(*a: Any, **kw: Any) -> None:
            raise KeyError("metrics capability missing")

        monkeypatch.setattr(server.plugin, "record_metric", raise_keyerror)
        await server._report_system_metrics_once()  # 能力未注入静默返回

    async def test_record_metric_other_error_continues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        system = types.SimpleNamespace(
            cpu_usage=1.0, memory_usage=1.0, disk_usage=1.0, network_sent=0, network_recv=0
        )
        monkeypatch.setattr(server, "_monitor", types.SimpleNamespace(get_system_metrics=Recorder(result=system)))
        attempted: list[str] = []

        async def flaky_record(name, *a: Any, **kw: Any) -> None:
            attempted.append(name)
            if len(attempted) == 1:
                raise RuntimeError("transient")

        monkeypatch.setattr(server.plugin, "record_metric", flaky_record)
        await server._report_system_metrics_once()
        assert len(attempted) == 5  # 单条失败不阻断剩余指标上报
