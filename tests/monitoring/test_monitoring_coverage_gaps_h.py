# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""Python 簇 H 缺口补测（monitoring 观测族）——coverage.xml 2026-09-14 口径缺行。

靶单（行号来自 HEAD 实测 coverage.xml，逐条对应见各测试 docstring）：
- pipeline_diagnostics.py 51-52（_as_text json 失败兜底）、60（_first_str 全空返回
  None）、74-75（raw_result.metadata 分支）、78-79（metadata.message 命中）；
- performance_monitor.py 481-484（start_monitoring 状态位 + 启动）、488-489
  （stop_monitoring 状态位 + 停止）；
- kernel_reads.py 51（_unwrap success=False → 空列表）、190-191（clear_execution_data
  非整型 status 兜底 500）、193（异常信封兜底 502）；
- server.py 440（pipeline-state.list 返回非 list → 空任务列表）、750-751
  （orphans limit 非法回退 50）、779（search 域分发入口）、918（context-token-usage
  跨租户 404）。

不可达行说明：本文件不保留任何"确实不可达"的行——上述靶单全部经公共入口
（函数调用 / http.handle 真实分发）触达。若后续某行因依赖升级变得不可达，
在此逐条登记原因，而非以 pragma 掩盖。

外部依赖（psutil 时钟、SQLite 库路径、capability 句柄）以假件/真实临时库替代；
监控循环不经真实 time.sleep（start_monitoring 的循环用注入 stop 语义驱动退出）。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_MON_DIR = Path(__file__).resolve().parents[2] / "plugins" / "shared" / "system" / "monitoring"


def _load(mod_name: str, filename: str) -> Any:
    """按唯一模块名装载 monitoring 目录模块（平铺 import 需目录在 sys.path）。"""
    if str(_MON_DIR) not in sys.path:
        sys.path.insert(0, str(_MON_DIR))
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _MON_DIR / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


pd = _load("pipeline_diagnostics_cluster_h", "pipeline_diagnostics.py")
pm = _load("performance_monitor_cluster_h", "performance_monitor.py")
kr = _load("kernel_reads_cluster_h", "kernel_reads.py")

_server = _load("monitoring_server_cluster_h", "server.py")


@pytest.fixture(autouse=True)
def _clean_providers():
    kr.reset_providers()
    yield
    kr.reset_providers()


def _data(resp: dict[str, Any], status: int = 200) -> Any:
    """解码 _ok(json_response(payload, status)) 信封的 body。"""
    assert resp["success"] is True
    data = resp["data"]
    assert data["status"] == status
    return json.loads(base64.b64decode(data["body"]))


# ═══════════════════════════════════════════════════════════
# pipeline_diagnostics：_as_text / _first_str / raw_result 摘要链
# ═══════════════════════════════════════════════════════════


class TestAsTextFallback:
    """error 字段收敛：json 不可序列化时退回 str()，不抛异常。"""

    @pytest.mark.parametrize(
        ("value", "fragment"),
        [
            ({1, 2}, "1"),
            (complex(0, 1), "j"),
        ],
        ids=["set", "complex"],
    )
    def test_unserializable_error_falls_back_to_str(self, value: Any, fragment: str) -> None:
        """51-52：set/complex 无法 json.dumps（TypeError）→ str() 兜底保留信息。

        两组输入区分度：set 的 str 含元素字面量，complex 的 str 含虚部后缀；
        性质断言：error 恒为非空字符串（读面契约"不因异常形态丢字段"）。
        """
        row = pd.project_trace({"trace_id": "t", "patch_data": {"raw_error": value}})
        assert row["error"] is not None
        assert fragment in row["error"]
        assert row["error"] == str(value)

    def test_circular_error_falls_back_to_str(self) -> None:
        """51-52（ValueError 支）：自引用列表 json.dumps 抛 ValueError → str() 兜底。"""
        circular: list[Any] = []
        circular.append(circular)
        row = pd.project_trace({"trace_id": "t", "patch_data": {"raw_error": circular}})
        assert row["error"] is not None
        assert "[" in row["error"]  # str(list) 形态，非 JSON

    def test_serializable_error_keeps_json_form(self) -> None:
        """对照支：可序列化对象走 json.dumps（不断言内部路径，只断形态差异）。"""
        row = pd.project_trace({"trace_id": "t", "patch_data": {"raw_error": {"code": 7}}})
        assert row["error"] == '{"code": 7}'


class TestFirstStrGuards:
    """_first_str 仅接受非空且 strip 后非空的字符串。"""

    @pytest.mark.parametrize(
        ("candidates", "expected"),
        [
            ((None,), None),
            (("",), None),
            (("   ",), None),
            ((42,), None),
            ((None, "", "hit"), "hit"),
            (("first", "second"), "first"),
        ],
        ids=["none", "empty", "blank", "int", "last-nonblank", "first-wins"],
    )
    def test_returns_first_non_blank_string(self, candidates: tuple[Any, ...], expected: str | None) -> None:
        """60：候选全空/非字符串 → None；命中首个非空串（空白视为空）。"""
        assert pd._first_str(*candidates) == expected


class TestRawResultSummaryChain:
    """raw_result 摘要链：router 未命中时依次尝试字符串文本与元数据消息。"""

    def test_raw_result_dict_metadata_message_used(self) -> None:
        """74-75,78-79：raw_result 为 dict 且 metadata.message 非空 → 作为摘要。"""
        row = pd.project_trace({
            "trace_id": "t",
            "patch_data": {"raw_result": {"metadata": {"message": "任务已写入 /tmp/a.md"}}},
        })
        assert row["summary"] == "任务已写入 /tmp/a.md"

    @pytest.mark.parametrize(
        "raw_result",
        [
            {"metadata": {}},
            {"metadata": {"message": ""}},
            {"metadata": {"message": "   "}},
            {"metadata": None},
            {"metadata": 42},
            {},
        ],
        ids=["empty-meta", "blank-msg", "space-msg", "none-meta", "int-meta", "no-meta"],
    )
    def test_metadata_without_usable_message_yields_none(self, raw_result: dict[str, Any]) -> None:
        """74-75 守卫支：metadata 缺席/非 dict/message 为空白 → 摘要 None（不崩）。

        性质断言：全部退化形态都不产生摘要，也不抛异常——读面对畸形 patch 的
        契约是"降级空投影"。
        """
        row = pd.project_trace({"trace_id": "t", "patch_data": {"raw_result": raw_result}})
        assert row["summary"] is None

    def test_router_text_wins_over_raw_result(self) -> None:
        """对照支：router 文本存在时不进 raw_result 分支（优先级契约）。"""
        row = pd.project_trace({
            "trace_id": "t",
            "patch_data": {
                "router": {"last_response_text": "router 优先"},
                "raw_result": {"metadata": {"message": "次选"}},
            },
        })
        assert row["summary"] == "router 优先"


# ═══════════════════════════════════════════════════════════
# performance_monitor：start/stop_monitoring 生命周期
# ═══════════════════════════════════════════════════════════


class TestStartStopMonitoring:
    """公开生命周期入口：状态位置位/复位 + 委派 start/stop（循环不空转真等待）。

    监控循环以 ``_shutdown_event`` 驱动：stop_monitoring 置位后循环立即退出，
    不经真实 sleep（本测试在 stop 内取消任务，等待为有界的事件驱动）。
    """

    async def test_start_monitoring_sets_flags_and_starts_loop(self) -> None:
        """481-484：interval 入账、active 置位、响应时间容器复位、委派 start。"""
        mon = pm.PerformanceMonitor()
        mon._response_times = [1.0, 2.0, 3.0]
        mon._monitoring_active = False

        await mon.start_monitoring(interval=7)
        try:
            assert mon._monitoring_interval == 7
            assert mon._monitoring_active is True
            assert mon._response_times == []  # 每次启动重新计量
            assert mon._monitor_task is not None
        finally:
            await mon.stop_monitoring()

    async def test_stop_monitoring_clears_flag_and_cancels_task(self) -> None:
        """488-489：active 复位、委派 stop（循环任务取消并清空引用）。"""
        mon = pm.PerformanceMonitor()
        await mon.start_monitoring(interval=5)
        await mon.stop_monitoring()
        assert mon._monitoring_active is False
        assert mon._monitor_task is None

    @pytest.mark.parametrize("interval", [1, 30])
    async def test_start_stop_idempotent_across_intervals(self, interval: int) -> None:
        """两档间隔（1/30）区分度：状态机与间隔值一一对应，可反复起停。"""
        mon = pm.PerformanceMonitor()
        for _ in range(2):
            await mon.start_monitoring(interval=interval)
            assert mon._monitoring_interval == interval
            assert mon._monitoring_active is True
            await mon.stop_monitoring()
            assert mon._monitoring_active is False
            assert mon._monitor_task is None


# ═══════════════════════════════════════════════════════════
# kernel_reads：_unwrap 归一信封 + clear_execution_data 写面错误码
# ═══════════════════════════════════════════════════════════


class TestUnwrapEnvelope:
    """信封收敛：三种形态各归其位。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ({"success": False, "data": [{"a": 1}]}, []),
            ({"success": True, "data": [{"a": 1}]}, [{"a": 1}]),
            ({"success": False}, {"success": False}),  # 缺 data → 非归一信封，原样返回
        ],
        ids=["failure-empties", "success-passes", "no-data-key"],
    )
    def test_normalized_envelope_respects_success(self, raw: Any, expected: Any) -> None:
        """51：``{success, data}`` 归一信封 success=False → 空列表（不泄露半成品）。"""
        assert kr._unwrap(raw) == expected

    def test_db_admin_status_body_envelope(self) -> None:
        """对照支：db-admin ``{status, body}`` 信封取 body（与归一信封不同分支）。"""
        assert kr._unwrap({"status": 200, "body": {"rows": []}}) == {"rows": []}

    def test_success_false_reaches_rows_as_empty(self) -> None:
        """端到端性质：失败信封经 _rows 收敛为空列表（读面降级契约）。"""
        assert kr._rows(kr._unwrap({"success": False, "data": [{"a": 1}, "junk"]})) == []


class TestClearExecutionDataErrors:
    """写面错误码：能力缺失 503 / 通道异常 502 / 异常信封透传状态。"""

    async def test_missing_provider_raises_503(self) -> None:
        """写面能力未注入 → 503（绝不降级假成功）。"""
        with pytest.raises(kr.ClearExecutionDataError) as excinfo:
            await kr.clear_execution_data()
        assert excinfo.value.status == 503

    async def test_channel_exception_raises_502(self) -> None:
        """调用通道抛异常 → 502，原始异常挂在 cause 上（可追溯）。"""
        async def _boom(**kwargs: Any) -> Any:
            raise RuntimeError("channel down")

        kr.set_provider("db-admin-clear", _boom)
        with pytest.raises(kr.ClearExecutionDataError) as excinfo:
            await kr.clear_execution_data()
        assert excinfo.value.status == 502
        assert isinstance(excinfo.value.__cause__, RuntimeError)

    async def test_non_integer_status_falls_back_to_500(self) -> None:
        """190-191：信封 status 非整型（'boom'）→ 兜底 500（不透传畸形码）。"""
        async def _stub(**kwargs: Any) -> Any:
            return {"status": "boom", "error": {"message": "内核内部错误"}}

        kr.set_provider("db-admin-clear", _stub)
        with pytest.raises(kr.ClearExecutionDataError) as excinfo:
            await kr.clear_execution_data()
        assert excinfo.value.status == 500
        assert "内核内部错误" in str(excinfo.value)

    async def test_none_status_falls_back_to_500(self) -> None:
        """190-191（or 500 支）：status 缺席 → 500，message 用错误对象内文本。

        两组输入区分度：'boom' 走 int() 转换失败、None 走 ``or 500`` 短路；
        性质断言：两者 status 均落在合法 HTTP 状态区间。
        """
        async def _stub(**kwargs: Any) -> Any:
            return {"error": {"message": "未带状态"}}

        kr.set_provider("db-admin-clear", _stub)
        with pytest.raises(kr.ClearExecutionDataError) as excinfo:
            await kr.clear_execution_data()
        assert excinfo.value.status == 500
        assert 400 <= excinfo.value.status <= 599

    async def test_error_message_non_dict_uses_str(self) -> None:
        """错误对象非 dict → str() 兜底（消息不丢）。"""
        async def _stub(**kwargs: Any) -> Any:
            return {"status": 409, "error": "plain reason"}

        kr.set_provider("db-admin-clear", _stub)
        with pytest.raises(kr.ClearExecutionDataError) as excinfo:
            await kr.clear_execution_data()
        assert excinfo.value.status == 409
        assert "plain reason" in str(excinfo.value)

    async def test_anomalous_envelope_raises_502(self) -> None:
        """193：非 dict / 无 error 无 body 的畸形信封 → 502（fail-visible）。"""
        async def _stub(**kwargs: Any) -> Any:
            return ["not", "an", "envelope"]

        kr.set_provider("db-admin-clear", _stub)
        with pytest.raises(kr.ClearExecutionDataError) as excinfo:
            await kr.clear_execution_data()
        assert excinfo.value.status == 502

    async def test_success_envelope_returns_body(self) -> None:
        """对照支：status=200 且 body 为 dict → 原样返回清理结果。"""
        async def _stub(**kwargs: Any) -> Any:
            return {"status": 200, "body": {"cleared_count": 9}}

        kr.set_provider("db-admin-clear", _stub)
        assert await kr.clear_execution_data(authorization="Bearer t") == {"cleared_count": 9}


# ═══════════════════════════════════════════════════════════
# server.py：非 list 降级 / orphans 参数 / search 分发 / 跨租户 404
# ═══════════════════════════════════════════════════════════


class _Handle:
    """capability 句柄替身：按 method 返回预设值。"""

    def __init__(self, result: Any) -> None:
        self._result = result

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        return self._result


class TestCollectStateTasksNonList:
    """pipeline-state.list 返回形状异常时降级空列表。"""

    @pytest.mark.parametrize(
        ("payload", "valid"),
        [
            ([{"pipeline_id": "p1"}], True),
            ({"pipeline_id": "p1"}, False),
            ("rows", False),
            (None, False),
            (42, False),
        ],
        ids=["list", "dict", "str", "none", "int"],
    )
    async def test_non_list_payload_degrades_to_empty(
        self, monkeypatch: pytest.MonkeyPatch, payload: Any, valid: bool
    ) -> None:
        """440：能力返回非 list（dict/str/None/int）→ 空任务列表，不抛异常。

        性质断言：返回恒为 list；仅当真值是 list 时才派生条目（形状守卫）。
        """
        monkeypatch.setattr(
            _server.plugin, "get_capability", lambda _name: _Handle(payload)
        )
        items = await _server._collect_state_tasks()
        assert isinstance(items, list)
        assert bool(items) is valid


class TestOrphansParamFallback:
    """orphans 路由的 limit 参数解析回退。"""

    @pytest.fixture
    def capture_orphans(self, monkeypatch: pytest.MonkeyPatch):
        seen: list[dict[str, Any]] = []

        async def _list_orphan_runs(**kwargs: Any) -> dict[str, Any]:
            seen.append(kwargs)
            return {"items": [], "total": 0}

        import types

        stub = types.ModuleType("execution_records")
        stub.list_orphan_runs = _list_orphan_runs
        monkeypatch.setitem(sys.modules, "execution_records", stub)
        return seen

    @pytest.mark.parametrize(
        ("limit", "expected"),
        [
            ("7", 7),
            ("abc", 50),
            ("", 50),
            ("3.5", 50),
        ],
        ids=["valid", "garbage", "empty", "float"],
    )
    async def test_limit_falls_back_to_default(
        self, capture_orphans: list[dict[str, Any]], limit: str, expected: int
    ) -> None:
        """750-751：limit 非整型 → 回退 50；合法值原样生效。

        性质断言：传给业务函数的 limit 恒为正整数（前端契约不因畸形 query 破坏）。
        """
        await _server.http_handle(
            path="/ext/monitoring/orphans", method="GET", query={"limit": limit}
        )
        assert capture_orphans[-1]["limit"] == expected
        assert capture_orphans[-1]["limit"] > 0
        assert capture_orphans[-1]["min_minutes"] == 10

    async def test_absent_limit_uses_default(self, capture_orphans: list[dict[str, Any]]) -> None:
        """对照支：limit 缺席 → 默认 50（与非法值回退同一出口）。"""
        await _server.http_handle(path="/ext/monitoring/orphans", method="GET", query={})
        assert capture_orphans[-1]["limit"] == 50


class TestSearchDomainDispatch:
    """search 域分发入口（http.handle → _handle_search_domain）。"""

    @pytest.fixture
    def stub_search(self, monkeypatch: pytest.MonkeyPatch):
        import types

        seen: list[dict[str, Any]] = []

        async def _search(**kwargs: Any) -> dict[str, Any]:
            seen.append(kwargs)
            return {"query": kwargs.get("q", ""), "sessions": [], "messages": []}

        stub = types.ModuleType("routes_search")
        stub.search = _search
        monkeypatch.setitem(sys.modules, "routes_search", stub)
        return seen

    async def test_search_path_dispatches_to_routes_search(
        self, stub_search: list[dict[str, Any]]
    ) -> None:
        """779：/ext/monitoring/search 经 http.handle 进入 search 域业务函数。"""
        body = _data(
            await _server.http_handle(
                path="/ext/monitoring/search",
                method="GET",
                query={"q": "贪吃蛇", "type": "all", "limit": "5"},
                headers={"x-agentos-tenant": "t1"},
            )
        )
        assert body["query"] == "贪吃蛇"
        assert stub_search[-1]["tenant_id"] == "t1"
        assert stub_search[-1]["limit"] == 5

    async def test_search_subpath_trailing_slash_also_dispatches(
        self, stub_search: list[dict[str, Any]]
    ) -> None:
        """对照支：带尾斜杠的 search 子路径同样命中域分发（子路径归一）。"""
        _data(
            await _server.http_handle(
                path="/ext/monitoring/search/",
                method="GET",
                query={"q": "x"},
                headers={"x-agentos-tenant": "t1"},
            )
        )
        assert stub_search[-1]["type"] == "all"

    async def test_unknown_search_subpath_404_not_dispatch(
        self, stub_search: list[dict[str, Any]]
    ) -> None:
        """守卫支：未知子路径 → 404 信封，不触达业务函数。"""
        resp = await _server.http_handle(
            path="/ext/monitoring/search/bogus", method="GET", query={}, headers={}
        )
        assert _data(resp, 404)["error"] == "not found"
        assert stub_search == []


class TestContextTokenUsageTenantGuard:
    """context-token-usage 端点的跨租户拒绝（不泄露存在性）。"""

    @pytest.fixture
    def runs_db(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        db = tmp_path / "kernel.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE runs (pipeline_id TEXT, tenant_id TEXT)")
        conn.execute("INSERT INTO runs VALUES ('s-foreign', 'other-tenant')")
        conn.commit()
        conn.close()
        monkeypatch.setattr(_server, "_kernel_db_path", lambda: str(db))
        return db

    async def test_cross_tenant_context_usage_404(
        self, monkeypatch: pytest.MonkeyPatch, runs_db: Path
    ) -> None:
        """918：context-token-usage 目标管道归属他租户 → 404，业务函数不调用。"""
        import types

        called: list[Any] = []

        async def _usage(*args: Any, **kwargs: Any) -> dict[str, Any]:
            called.append((args, kwargs))
            return {"total_tokens": 1}

        stub = types.ModuleType("execution_records")
        stub.get_session_context_token_usage = _usage
        monkeypatch.setitem(sys.modules, "execution_records", stub)

        resp = await _server.http_handle(
            path="/ext/monitoring/sessions/s-foreign/context-token-usage",
            method="GET",
            query={},
            headers={"x-agentos-tenant": "t1"},
        )
        assert _data(resp, 404)["error"] == "not found"
        assert called == [], "跨租户拒绝须发生在业务查询前"

    async def test_owned_session_context_usage_passes_parent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """对照支：未知管道（runs 无行）放行，parent 参数透传业务函数。"""
        import types

        db = tmp_path / "kernel.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE runs (pipeline_id TEXT, tenant_id TEXT)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(_server, "_kernel_db_path", lambda: str(db))

        seen: list[dict[str, Any]] = []

        async def _usage(session_id: str, **kwargs: Any) -> dict[str, Any]:
            seen.append({"session_id": session_id, **kwargs})
            return {"total_tokens": 5, "is_estimated": True}

        stub = types.ModuleType("execution_records")
        stub.get_session_context_token_usage = _usage
        monkeypatch.setitem(sys.modules, "execution_records", stub)

        body = _data(
            await _server.http_handle(
                path="/ext/monitoring/sessions/s-new/context-token-usage",
                method="GET",
                query={"parent_execution_record_id": "rec-9"},
                headers={"x-agentos-tenant": "t1"},
            )
        )
        assert body == {"total_tokens": 5, "is_estimated": True}
        assert seen[-1]["session_id"] == "s-new"
        assert seen[-1]["parent_execution_record_id"] == "rec-9"
