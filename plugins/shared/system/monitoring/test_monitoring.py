# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""monitoring 插件（监控服务接口适配层）单元测试。

覆盖（对齐 plugins/shared/system/monitoring/server.py）：
1. MCP 工具：get_metrics / get_health / record_llm_request* / record_tool_execution / update_task_status
2. 生命周期：on_load（启动上报循环）/ on_unload（取消并停止）
3. record_metric 上报降级（capability 未注入 KeyError / 异常 → 静默）
4. http.handle 路由：system/metrics、task 统计、token/cache、payload-diag（含路径穿越防护）、tool-calls、webview 页面、404/500
5. _parse_payload_diag_filename / _query_tool_calls 解析与降级

外部依赖：performance_monitor 用 sys.modules 伪模块替代；record_metric 用
伪 async 函数注入；SQLite 用 tmp_path 真实库文件（不依赖内核）。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
# routes_search 等模块在调用期 import 共享根的 kernel_db（生产环境平铺 sys.path
# 含 shared 根）：单文件直跑时无其他用例代为注入，这里自锚定，不依赖跨用例污染。
_SHARED_ROOT = _PLUGIN_DIR.parent.parent
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))


@pytest.fixture(autouse=True)
def _restore_performance_monitor_module():
    """用例后还原 performance_monitor 的 sys.modules 缓存。

    本文件的 fake 模块只带 PerformanceMonitor 一个名字；若不清除，同进程
    后续测试（如 test_active_requests 经 server.py 平铺 import）会拿到残缺
    fake 而 ERROR/断言失败——车道内实测串扰（2026-08-21 接线进插桩车道暴露）。
    """
    had = "performance_monitor" in sys.modules
    saved = sys.modules.get("performance_monitor")
    yield
    if had and saved is not None:
        sys.modules["performance_monitor"] = saved
    else:
        sys.modules.pop("performance_monitor", None)


# ── 伪 performance_monitor 模块（server 懒加载路径） ──
class FakePerformanceMonitor:
    """模拟 PerformanceMonitor 的最小接口。"""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self._task_stats = {"completed_tasks": 2, "running_tasks": 1, "pending_tasks": 0}
        self._llm_stats = {"request_count": 3, "active_requests": 1, "error_count": 0, "total_response_time": 1.5}
        self._tool_stats = {"cache_hits": 4, "cache_misses": 6}
        self.llm_requests: list[tuple[float, bool]] = []
        self.tool_executions: list[tuple[float, bool, bool]] = []
        self.task_updates: list[tuple] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def get_system_metrics(self) -> Any:
        # server 侧按属性访问（system.cpu_usage），用 SimpleNamespace 对齐
        return types.SimpleNamespace(
            cpu_usage=30.0,
            memory_usage=50.0,
            disk_usage=60.0,
            network_sent=1024.0,
            network_recv=2048.0,
        )

    async def get_current_metrics(self) -> dict[str, Any]:
        return {"system": {"cpu_usage": 30.0}, "llm": {}, "tool": {}, "task": {}}

    def get_health_status(self) -> dict[str, Any]:
        return {"status": "healthy", "issues": [], "metrics": {}}

    def record_llm_request(self, response_time: float, error: bool = False) -> None:
        self.llm_requests.append((response_time, error))

    def record_tool_execution(self, execution_time: float, cache_hit: bool = False, error: bool = False) -> None:
        self.tool_executions.append((execution_time, cache_hit, error))

    def update_task_status(self, pending: int, running: int, completed: int, task_time: float = 0) -> None:
        self.task_updates.append((pending, running, completed, task_time))


def _install_fake_performance_monitor() -> None:
    """无条件替换为 fake（真实模块由 _restore_performance_monitor_module
    在用例后还原——车道内更早的模块会把真 performance_monitor 灌进缓存，
    旧的 not-in-sys.modules 守卫会静默跳过注入导致 fake 断言失败）。"""
    fake_mod = types.ModuleType("performance_monitor")
    fake_mod.PerformanceMonitor = FakePerformanceMonitor
    sys.modules["performance_monitor"] = fake_mod


def _load_server() -> Any:
    """动态加载 server.py（唯一模块名，避免与插件全局状态互相污染）。"""
    _install_fake_performance_monitor()
    mod_name = "monitoring_server_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestCollectStateTasksStatus:
    """任务列表状态映射：引擎把 run_status 写进 state 基线（单一运行状态真值），
    task 域键（task.status）缺席的管道（如聊天会话）必须回退 run_status，
    而非恒显 unknown（GUI 黑盒测试 2026-09-11：调试-任务状态列全 unknown）。"""

    def test_status_falls_back_to_kernel_run_status(self, monkeypatch) -> None:
        mod = _load_server()

        class _FakeHandle:
            async def call(self, _name: str, _params: dict) -> list[dict[str, Any]]:
                return [
                    {
                        "pipeline_id": "p-1",
                        "thread_id": "thread-1",
                        "run_status": "completed",
                        "current_phase": "exit",
                        "track.total_tokens": 125648,
                    },
                    {"pipeline_id": "p-2", "thread_id": "thread-2", "task.status": "running"},
                    {"pipeline_id": "p-3", "thread_id": "thread-3"},
                ]

        class _FakePlugin:
            def get_capability(self, _cap: str) -> _FakeHandle:
                return _FakeHandle()

        monkeypatch.setattr(mod, "plugin", _FakePlugin())
        items = _run(mod._collect_state_tasks())
        by_id = {i["pipeline_id"]: i for i in items}
        # task.* 缺席 → 回退内核 run_status；run_status 字段同源（非 router.stop_reason）
        assert by_id["p-1"]["status"] == "completed"
        assert by_id["p-1"]["run_status"] == "completed"
        # task.* 存在时优先
        assert by_id["p-2"]["status"] == "running"
        # 双双缺席 → unknown（诚实状态机，不猜）
        assert by_id["p-3"]["status"] == "unknown"


def _make_module_with_monitor() -> tuple[Any, FakePerformanceMonitor]:
    mod = _load_server()
    monitor = FakePerformanceMonitor()
    mod._monitor = monitor
    mod._reporter_task = None
    return mod, monitor


# ═══════════════════════════════════════════════════════════
# MCP 工具：指标/健康/记录
# ═══════════════════════════════════════════════════════════


class TestMonitoringTools:
    def test_get_metrics(self) -> None:
        mod, monitor = _make_module_with_monitor()
        result = _run(mod.monitoring_get_metrics())
        assert result["system"]["cpu_usage"] == 30.0

    def test_get_health(self) -> None:
        mod, _ = _make_module_with_monitor()
        result = _run(mod.monitoring_get_health())
        assert result["status"] == "healthy"

    def test_record_llm_request(self) -> None:
        mod, monitor = _make_module_with_monitor()
        result = _run(mod.monitoring_record_llm_request(response_time=2.5, error=True))
        assert result == {"recorded": True}
        assert monitor.llm_requests == [(2.5, True)]

    def test_record_tool_execution(self) -> None:
        mod, monitor = _make_module_with_monitor()
        result = _run(mod.monitoring_record_tool_execution(execution_time=0.3, cache_hit=True))
        assert result == {"recorded": True}
        assert monitor.tool_executions == [(0.3, True, False)]

    def test_update_task_status(self) -> None:
        mod, monitor = _make_module_with_monitor()
        result = _run(mod.monitoring_update_task_status(pending=1, running=2, completed=3, task_time=4.0))
        assert result == {"updated": True}
        assert monitor.task_updates == [(1, 2, 3, 4.0)]

    def test_ensure_monitor_lazy_creates(self) -> None:
        """_monitor 未初始化 → _ensure_monitor 延迟创建。"""
        mod = _load_server()
        mod._monitor = None
        assert isinstance(mod._ensure_monitor(), FakePerformanceMonitor)


# ═══════════════════════════════════════════════════════════
# 生命周期：on_load / on_unload + record_metric 上报
# ═══════════════════════════════════════════════════════════


class FakeRecorder:
    """记录 record_metric 调用的伪插件 record_metric。"""

    def __init__(self, behavior: str = "ok") -> None:
        self.calls: list[tuple] = []
        self._behavior = behavior

    async def __call__(self, name: str, value: float, metric_type: str = "counter", labels: dict | None = None, unit: str | None = None, **kwargs: Any) -> Any:
        self.calls.append((name, value, metric_type, labels, unit))
        if self._behavior == "key_error":
            raise KeyError("metrics capability not injected")
        if self._behavior == "error":
            raise RuntimeError("aggregator down")
        return None


class TestLifecycle:
    def test_on_load_starts_monitor_and_reporter(self) -> None:
        """on_load 启动 monitor + 上报任务；on_unload 在同一事件循环内取消任务。"""
        mod = _load_server()
        mod.plugin.record_metric = FakeRecorder()  # type: ignore[method-assign]
        # _on_load 创建的上报任务绑定当前事件循环，卸载必须同循环执行，
        # 否则跨循环 cancel 会抛 RuntimeError（Event loop is closed）
        loop = asyncio.new_event_loop()
        monitor = None
        try:
            loop.run_until_complete(mod._on_load({}))
            monitor = mod._monitor
            assert isinstance(monitor, FakePerformanceMonitor)
            assert monitor.started is True
            assert mod._reporter_task is not None and not mod._reporter_task.done()
            loop.run_until_complete(mod._on_unload({}))
        finally:
            loop.close()
        assert monitor.stopped is True  # type: ignore[union-attr]
        assert mod._reporter_task is None
        assert mod._monitor is None

    def test_on_unload_idempotent(self) -> None:
        mod = _load_server()
        _run(mod._on_unload({}))
        assert mod._monitor is None

    def test_report_system_metrics_once_ok(self) -> None:
        mod, _ = _make_module_with_monitor()
        recorder = FakeRecorder()
        mod.plugin.record_metric = recorder  # type: ignore[method-assign]
        _run(mod._report_system_metrics_once())
        names = [c[0] for c in recorder.calls]
        assert "system.cpu_usage_ratio" in names
        assert "system.memory_usage_ratio" in names
        assert "system.disk_usage_ratio" in names
        assert "system.network_sent_kbytes_per_sec" in names
        assert "system.network_recv_kbytes_per_sec" in names
        # 单位与标签
        cpu_call = next(c for c in recorder.calls if c[0] == "system.cpu_usage_ratio")
        assert cpu_call[1] == pytest.approx(0.3)
        assert cpu_call[3] == {"source": "psutil"}
        assert cpu_call[4] == "ratio"

    def test_report_metrics_once_no_monitor(self) -> None:
        mod = _load_server()
        mod._monitor = None
        _run(mod._report_system_metrics_once())  # 不抛异常

    def test_report_metrics_once_record_metric_key_error(self) -> None:
        """metrics capability 未注入（KeyError）→ 静默跳过。"""
        mod, _ = _make_module_with_monitor()
        mod.plugin.record_metric = FakeRecorder(behavior="key_error")  # type: ignore[method-assign]
        _run(mod._report_system_metrics_once())  # 不抛异常

    def test_report_metrics_once_record_metric_error(self) -> None:
        """record_metric 抛其他异常 → 仅记 debug 日志继续。"""
        mod, _ = _make_module_with_monitor()
        mod.plugin.record_metric = FakeRecorder(behavior="error")  # type: ignore[method-assign]
        _run(mod._report_system_metrics_once())

    def test_report_metrics_once_monitor_error_degrades(self) -> None:
        """get_system_metrics 抛异常 → 直接返回。"""
        mod, _ = _make_module_with_monitor()

        async def _boom():
            raise RuntimeError("psutil failed")

        mod._monitor.get_system_metrics = _boom  # type: ignore[method-assign]
        _run(mod._report_system_metrics_once())


# ═══════════════════════════════════════════════════════════
# http.handle 路由
# ═══════════════════════════════════════════════════════════


def _decode_body(response: dict[str, Any]) -> Any:
    """解包 HttpHandleResponse 的 base64 body。"""
    body = response["data"]["body"]
    return json.loads(base64.b64decode(body).decode("utf-8"))


class TestHttpHandle:
    def test_system_metrics_route(self) -> None:
        mod, _ = _make_module_with_monitor()
        resp = _run(mod.http_handle(path="/ext/monitoring/system/metrics", method="GET"))
        assert resp["success"] is True
        payload = _decode_body(resp)
        assert "cpu_usage" in payload["metrics"]
        assert "memory" in payload["metrics"]

    def test_task_statistics_route(self) -> None:
        mod, monitor = _make_module_with_monitor()
        resp = _run(mod.http_handle(path="/ext/monitoring/tasks/statistics", method="GET"))
        payload = _decode_body(resp)
        stats = payload["statistics"]
        assert stats["total"] == 3
        assert stats["succeeded"] == 2
        assert stats["running"] == 1

    def test_tasks_list_route_pagination(self) -> None:
        mod, _ = _make_module_with_monitor()
        resp = _run(
            mod.http_handle(path="/ext/monitoring/tasks", method="GET", query={"page": "2", "page_size": "10"})
        )
        payload = _decode_body(resp)
        assert payload == {"items": [], "total": 0, "page": 2, "page_size": 10}

    def test_token_usage_route(self) -> None:
        mod, monitor = _make_module_with_monitor()
        monitor._llm_stats = {"request_count": 7, "active_requests": 2, "error_count": 1, "total_response_time": 9.0}
        resp = _run(mod.http_handle(path="/ext/monitoring/token-usage", method="GET"))
        payload = _decode_body(resp)
        assert payload["token_usage"]["request_count"] == 7
        assert payload["token_usage"]["active_requests"] == 2

    def test_cache_stats_route(self) -> None:
        mod, _ = _make_module_with_monitor()
        resp = _run(mod.http_handle(path="/ext/monitoring/cache-stats", method="GET"))
        payload = _decode_body(resp)
        assert payload["cache_stats"]["cache_hits"] == 4
        assert payload["cache_stats"]["hit_rate"] == 40.0

    def test_unknown_route_404(self) -> None:
        mod, _ = _make_module_with_monitor()
        resp = _run(mod.http_handle(path="/ext/monitoring/nope", method="GET"))
        assert resp["success"] is True
        assert _decode_body(resp)["error"] == "not found"

    def test_exception_route_500(self) -> None:
        """处理器抛异常 → _error 500。"""
        mod, _ = _make_module_with_monitor()
        mod._collect_system_metrics = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
        resp = _run(mod.http_handle(path="/ext/monitoring/system/metrics", method="GET"))
        assert resp["success"] is False
        assert "boom" in resp["error"]
        assert resp["data"]["status"] == 500


# ═══════════════════════════════════════════════════════════
# payload-diag：列表 / 文件名解析 / 单文件读取（防路径穿越）
# ═══════════════════════════════════════════════════════════


class TestPayloadDiag:
    def test_parse_filename_valid(self) -> None:
        mod = _load_server()
        meta = mod._parse_payload_diag_filename("1723380000000__deepseek-v4-flash__a1b2c3d4__12msg.json")
        assert meta == {"ts": 1723380000000, "model": "deepseek-v4-flash", "msgs_hash": "a1b2c3d4", "msg_count": 12}

    def test_parse_filename_model_with_double_underscore(self) -> None:
        mod = _load_server()
        meta = mod._parse_payload_diag_filename("100__my__model__h__2msg.json")
        assert meta is not None and meta["model"] == "my__model"

    def test_parse_filename_invalid(self) -> None:
        mod = _load_server()
        assert mod._parse_payload_diag_filename("plain.txt") is None
        assert mod._parse_payload_diag_filename("a.json") is None  # 无 msg 后缀
        assert mod._parse_payload_diag_filename("1__2__3msg.json") is None  # 段数不足
        assert mod._parse_payload_diag_filename("abc__m__h__nmsg.json") is None  # ts 非法

    def test_list_payload_diag(self, tmp_path: Path, monkeypatch) -> None:
        mod = _load_server()
        monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
        diag_dir = tmp_path / "logs" / "payload_diag"
        diag_dir.mkdir(parents=True)
        (diag_dir / "100__model-a__h1__1msg.json").write_text("{}", encoding="utf-8")
        (diag_dir / "200__model-b__h2__2msg.json").write_text("{}", encoding="utf-8")
        (diag_dir / "garbage.txt").write_text("x", encoding="utf-8")

        items = mod._list_payload_diag()
        # 时间倒序，非法文件名被跳过
        assert [i["ts"] for i in items] == [200, 100]
        assert all("name" in i and "size" in i for i in items)

    def test_read_payload_diag_file(self, tmp_path: Path, monkeypatch) -> None:
        mod = _load_server()
        monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
        diag_dir = tmp_path / "logs" / "payload_diag"
        diag_dir.mkdir(parents=True)
        (diag_dir / "100__m__h__1msg.json").write_text('{"ok": 1}', encoding="utf-8")

        result = mod._read_payload_diag("100__m__h__1msg.json")
        assert result["content"] == '{"ok": 1}'

    def test_read_payload_diag_path_traversal_blocked(self) -> None:
        """防路径穿越：分隔符 / 反斜杠 / .. 一律拒绝。"""
        mod = _load_server()
        for bad in ("../etc/passwd", "a/b.json", "a\\b.json", "", "x.txt"):
            result = mod._read_payload_diag(bad)
            assert "error" in result

    def test_read_payload_diag_not_found(self, tmp_path: Path, monkeypatch) -> None:
        mod = _load_server()
        monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
        result = mod._read_payload_diag("100__m__h__1msg.json")
        assert result["error"] == "file not found"

    def test_payload_diag_pages(self) -> None:
        mod, _ = _make_module_with_monitor()
        for path in ("/ext/monitoring/page/payload-diag", "/ext/monitoring/page/tool-calls"):
            resp = _run(mod.http_handle(path=path, method="GET"))
            assert resp["success"] is True
            assert resp["data"]["status"] == 200
            assert resp["data"]["body_encoding"] == "base64"


# ═══════════════════════════════════════════════════════════
# tool-calls：SQLite 查询 + 筛选 + 降级
# ═══════════════════════════════════════════════════════════


def _make_kernel_db(tmp_path: Path) -> Path:
    """内核 traces 表真实落库形态（引擎 persist_step_trace）：

    plugin_id = 配置 step id（core/prepare/...），工具结果数组在
    patch_data.tool_results（tool_core 结果经 state diff 落 trace）。
    另造两行负控制：
    - plugin_id='pipeline_tool_core' 但无 tool_results 键（llm 形态 diff）——
      证明查询按内容谓词而非 plugin_id 选择；
    - plugin_id='prepare' 携带其他键——无 tool_results 不入选。
    租户分布：tr-1/tr-2（工具行）在 tenant-a，tr-3/tr-4 在 tenant-b——
    租户过滤测试的互斥可见性前提。
    """
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE traces (trace_id TEXT, run_id TEXT, created_at TEXT, plugin_id TEXT, patch_data TEXT, tenant_id TEXT)")
    conn.execute(
        "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?)",
        (
            "tr-1",
            "run-1",
            "2026-01-01T00:00:00",
            "core",
            json.dumps(
                {
                    "_executed_tool_calls": [{"name": "bash_execute"}, {"name": "file_write"}],
                    "tool_results": [
                        {"tool_name": "bash_execute", "success": True, "data": {"ok": 1}, "duration_ms": 50.0},
                        {
                            "tool_name": "file_write",
                            "success": False,
                            "error": "denied",
                            "data": None,
                            "duration_ms": 2500.0,
                        },
                    ],
                }
            ),
            "tenant-a",
        ),
    )
    conn.execute(
        "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?)",
        (
            "tr-2",
            "run-1",
            "2026-01-01T00:01:00",
            "core",
            json.dumps({"tool_results": [{"tool_name": "file_read", "success": True, "data": "x", "duration_ms": 3.0}]}),
            "tenant-a",
        ),
    )
    # 负控制①：插件 id 恰为 pipeline_tool_core 但 patch_data 无 tool_results。
    conn.execute(
        "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?)",
        ("tr-3", "run-2", "2026-01-01T00:02:00", "pipeline_tool_core", json.dumps({"raw_result": "你好"}), "tenant-b"),
    )
    # 负控制②：普通 step 行，无工具结果。
    conn.execute(
        "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?)",
        ("tr-4", "run-2", "2026-01-01T00:03:00", "prepare", json.dumps({"tool_schemas_count": 41}), "tenant-b"),
    )
    conn.commit()
    conn.close()
    return db_path


class TestToolCalls:
    def test_query_tool_calls_all(self, tmp_path: Path, monkeypatch) -> None:
        mod = _load_server()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_kernel_db(tmp_path)))
        result = mod._query_tool_calls({}, "tenant-a")
        # tenant-a 两行 tool-carrying step 轨迹共 3 条结果；负控制两行（含
        # plugin_id 恰为 pipeline_tool_core 的行）均不入选——内容谓词，与
        # plugin_id 无关。
        assert result["total"] == 3
        assert {i["tool_name"] for i in result["items"]} == {"bash_execute", "file_write", "file_read"}

    def test_query_tool_calls_filters(self, tmp_path: Path, monkeypatch) -> None:
        mod = _load_server()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_kernel_db(tmp_path)))
        # 按工具名
        by_tool = mod._query_tool_calls({"tool_name": "bash_execute"}, "tenant-a")
        assert by_tool["total"] == 1 and by_tool["items"][0]["tool_name"] == "bash_execute"
        # 按状态 error
        by_status = mod._query_tool_calls({"status": "error"}, "tenant-a")
        assert by_status["total"] == 1 and by_status["items"][0]["success"] == 0
        assert by_status["items"][0]["error"] == "denied"
        # 按最小耗时
        by_dur = mod._query_tool_calls({"min_duration": "1000"}, "tenant-a")
        assert by_dur["total"] == 1 and by_dur["items"][0]["tool_name"] == "file_write"
        # 非法 min_duration：忽略过滤条件（修复后：不追加 SQL 占位符、
        # 查询正常返回全部结果，不再因绑定数不匹配报错）
        ok = mod._query_tool_calls({"min_duration": "abc"}, "tenant-a")
        assert ok["total"] == 3
        # limit 钳制
        clamped = mod._query_tool_calls({"limit": "999"}, "tenant-a")
        assert clamped["total"] == 3

    def test_query_tool_calls_tenant_isolation(self, tmp_path: Path, monkeypatch) -> None:
        """两租户数据互不可见：tenant-b 的行（含无工具结果的负控制）对
        tenant-a 查询不可见，反之亦然。"""
        mod = _load_server()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_kernel_db(tmp_path)))
        a = mod._query_tool_calls({}, "tenant-a")
        assert a["total"] == 3
        assert all(i["run_id"] == "run-1" for i in a["items"])
        # tenant-b 的 traces 行 patch_data 无 tool_results → 工具查询恒空；
        # 租户互斥由 tenant-a 查不到 tenant-b 的 run-2 行证明。
        b = mod._query_tool_calls({}, "tenant-b")
        assert b["items"] == [] and b["total"] == 0
        # 租户不存在 → 空集（查询域内无该租户数据）
        ghost = mod._query_tool_calls({}, "tenant-ghost")
        assert ghost["items"] == [] and ghost["total"] == 0

    def test_query_tool_calls_missing_tenant_fails_closed(self, tmp_path: Path, monkeypatch) -> None:
        """身份头缺失（空租户）→ 空集而非全量查询（fail-closed，防跨租户泄露）。"""
        mod = _load_server()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_kernel_db(tmp_path)))
        result = mod._query_tool_calls({}, "")
        assert result["items"] == [] and result["total"] == 0
        assert "tenant" in result["error"]

    def test_query_tool_calls_db_missing(self, tmp_path: Path, monkeypatch) -> None:
        mod = _load_server()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(tmp_path / "nonexistent.db"))
        result = mod._query_tool_calls({}, "tenant-a")
        assert result["items"] == [] and "not found" in result["error"]

    def test_query_tool_calls_db_error(self, tmp_path: Path, monkeypatch) -> None:
        """数据库损坏 → 返回错误而非崩溃。"""
        mod = _load_server()
        db_path = tmp_path / "bad.db"
        db_path.write_bytes(b"not a sqlite file")
        monkeypatch.setenv("AGENTOS_DB_PATH", str(db_path))
        result = mod._query_tool_calls({})
        assert result["items"] == [] and result["error"]

    def test_tool_calls_route(self, tmp_path: Path, monkeypatch) -> None:
        """http.handle 路由把内核注入的 x-agentos-tenant 头传给租户过滤查询。"""
        mod, _ = _make_module_with_monitor()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_kernel_db(tmp_path)))
        resp = _run(mod.http_handle(
            path="/ext/monitoring/tool-calls",
            method="GET",
            headers={"x-agentos-tenant": "tenant-a"},
        ))
        payload = _decode_body(resp)
        assert payload["total"] == 3

    def test_tool_calls_route_missing_tenant_fails_closed(self, tmp_path: Path, monkeypatch) -> None:
        """无身份头（非内核认证分发）→ 空集而非全量（fail-closed）。"""
        mod, _ = _make_module_with_monitor()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_kernel_db(tmp_path)))
        resp = _run(mod.http_handle(path="/ext/monitoring/tool-calls", method="GET"))
        payload = _decode_body(resp)
        assert payload["items"] == [] and payload["total"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 插件运行态端点（/ext/monitoring/plugins——metrics-admin 读面桥）
# ═══════════════════════════════════════════════════════════════════════════


def _async_provider(payload: Any, seen: dict[str, Any] | None = None):
    """伪内核 provider：捕获 kwargs（_authorization 透传契约）恒返回 payload。"""

    async def _p(**kwargs: Any) -> Any:
        if seen is not None:
            seen.update(kwargs)
        return payload

    return _p


class TestPluginRuntimeRoute:
    """kernel_reads.plugin_runtime（metrics-admin list/query 组装）+ 路由降级。"""

    def setup_method(self) -> None:
        import kernel_reads

        self.kernel_reads = kernel_reads
        self.mod, _ = _make_module_with_monitor()

    def teardown_method(self) -> None:
        self.kernel_reads.reset_providers()

    def test_assembles_rows_columns_and_lifecycle_totals(self) -> None:
        seen_list: dict[str, Any] = {}
        seen_query: dict[str, Any] = {}
        self.kernel_reads.set_provider(
            "metrics-admin-list",
            _async_provider(
                {
                    "status": 200,
                    "body": {
                        "series": [
                            {"plugin_id": "llm", "name": "process.alive", "latest": 1},
                            {"plugin_id": "llm", "name": "process.pid", "latest": 123},
                            {
                                "plugin_id": "llm",
                                "name": "process.memory_rss_bytes",
                                "latest": 52_428_800,
                            },
                            {
                                "plugin_id": "llm",
                                "name": "process.uptime_seconds",
                                "latest": 3600,
                            },
                            {
                                "plugin_id": "llm",
                                "name": "process.last_crash_ts",
                                "latest": 0,
                            },
                            {"plugin_id": "dead", "name": "process.alive", "latest": 0},
                            # 非进程态 series（业务计数）不入运行态行
                            {"plugin_id": "llm", "name": "tokens_used", "latest": 9},
                        ],
                        "total": 7,
                    },
                },
                seen_list,
            ),
        )
        self.kernel_reads.set_provider(
            "metrics-admin-query",
            _async_provider(
                {
                    "status": 200,
                    "body": {
                        "metrics": [
                            {
                                "plugin_id": "kernel",
                                "name": "lifecycle.plugin_load_total",
                                "samples": [
                                    {"ts": 1, "value": 3.0},
                                    {"ts": 2, "value": 2.0},
                                ],
                            },
                            {
                                "plugin_id": "kernel",
                                "name": "lifecycle.plugin_error_total",
                                "samples": [{"ts": 1, "value": 1.0}],
                            },
                            # 非目标名（其余内核计数）不计入 lifecycle
                            {
                                "plugin_id": "kernel",
                                "name": "kernel.api.dispatcher_errors",
                                "samples": [{"ts": 1, "value": 9.0}],
                            },
                        ]
                    },
                },
                seen_query,
            ),
        )
        resp = _run(
            self.mod.http_handle(
                path="/ext/monitoring/plugins",
                method="GET",
                headers={"authorization": "Bearer tok"},
            )
        )
        assert resp["success"] is True
        body = _decode_body(resp)
        assert body["total"] == 2
        rows = {r["plugin_id"]: r for r in body["rows"]}
        assert rows["llm"]["status"] == "running"
        assert rows["llm"]["pid"] == 123
        assert rows["llm"]["memory_rss_mb"] == 50.0
        assert rows["llm"]["uptime_seconds"] == 3600
        assert rows["dead"]["status"] == "dead"
        assert rows["dead"]["last_crash_ts"] == 0
        # lifecycle 计数 = 目标两名计数器样本求和
        assert body["lifecycle"] == {"plugin_load_total": 5, "plugin_error_total": 1}
        # 凭证透传契约（内核 handler 侧做 admin/viewer 角色校验）
        assert seen_list == {"authorization": "Bearer tok"}
        assert seen_query == {"authorization": "Bearer tok", "plugin_id": "kernel"}
        # 中文表头列声明（前端表格零改动渲染）
        assert [c["key"] for c in body["columns"]][:2] == ["plugin_id", "status"]

    def test_capability_unavailable_degrades_to_empty(self) -> None:
        # provider 未注入（内核能力不可用）→ HTTP 200 空结构（读面降级契约）
        resp = _run(
            self.mod.http_handle(path="/ext/monitoring/plugins", method="GET", headers={})
        )
        assert resp["success"] is True
        body = _decode_body(resp)
        assert body["rows"] == []
        assert body["total"] == 0
        assert body["lifecycle"] == {"plugin_load_total": 0, "plugin_error_total": 0}

    def test_kernel_error_envelope_degrades_to_empty(self) -> None:
        # 内核信封非 200（如非 admin 角色 403）→ 空结构不崩 handler
        err = {"status": 403, "error": {"code": "403", "message": "需要 admin 或 viewer 角色"}}
        self.kernel_reads.set_provider("metrics-admin-list", _async_provider(err))
        self.kernel_reads.set_provider("metrics-admin-query", _async_provider(err))
        resp = _run(
            self.mod.http_handle(path="/ext/monitoring/plugins", method="GET", headers={})
        )
        assert resp["success"] is True
        body = _decode_body(resp)
        assert body["rows"] == []
        assert body["lifecycle"] == {"plugin_load_total": 0, "plugin_error_total": 0}


# ═══════════════════════════════════════════════════════════
# Token 用量聚合（按模型 / 按日）
# ═══════════════════════════════════════════════════════════


def _make_llm_traces_db(tmp_path: Path) -> Path:
    """造含多模型 llm_usage 轨迹的内核 DB（含一条早期无 model 归属的行）。"""
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE traces (trace_id TEXT, run_id TEXT, created_at TEXT,"
        " plugin_id TEXT, patch_data TEXT)"
    )
    usage_rows = [
        ("tr-1", "2026-09-01T08:00:00+00:00", {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "model": "deepseek-v4-flash", "provider": "deepseek"}),
        ("tr-2", "2026-09-01T09:30:00+00:00", {"input_tokens": 200, "output_tokens": 20, "total_tokens": 220, "model": "deepseek-v4-flash", "provider": "deepseek"}),
        ("tr-3", "2026-09-02T14:00:00+00:00", {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55, "model": "MiniMax-M3", "provider": "minimax"}),
        ("tr-4", "2026-08-30T12:00:00+00:00", {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}),
    ]
    for tid, ts, usage in usage_rows:
        conn.execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?, ?)",
            (tid, "run-1", ts, "core", json.dumps({"llm_usage": usage})),
        )
    conn.commit()
    conn.close()
    return db_path


class TestTokenUsageByModel:
    def test_groups_by_model_with_icons(self, tmp_path: Path, monkeypatch) -> None:
        mod, _ = _make_module_with_monitor()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_llm_traces_db(tmp_path)))
        payload = mod._collect_token_usage()

        rows = {r["model"]: r for r in payload["rows"]}
        assert set(rows) == {"deepseek-v4-flash", "MiniMax-M3", "（未记录模型）"}
        # 同模型多轮聚合：输入/输出/合计/次数逐列累加
        assert rows["deepseek-v4-flash"]["input_tokens"] == 300
        assert rows["deepseek-v4-flash"]["output_tokens"] == 30
        assert rows["deepseek-v4-flash"]["total_tokens"] == 330
        assert rows["deepseek-v4-flash"]["requests"] == 2
        assert rows["MiniMax-M3"]["requests"] == 1
        # 图标按 provider 前缀映射；无归属回退缺省
        assert rows["deepseek-v4-flash"]["icon"] == "🐋"
        assert rows["MiniMax-M3"]["icon"] == "🐚"
        assert rows["（未记录模型）"]["icon"] == "🔤"

    def test_totals_equal_sum_of_model_rows(self, tmp_path: Path, monkeypatch) -> None:
        # 性质断言：总量卡口径 = 各模型行之和（同一查询源，不许两套账）
        mod, _ = _make_module_with_monitor()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_llm_traces_db(tmp_path)))
        payload = mod._collect_token_usage()
        assert payload["prompt_tokens"] == sum(r["input_tokens"] for r in payload["rows"])
        assert payload["completion_tokens"] == sum(r["output_tokens"] for r in payload["rows"])
        assert payload["total_tokens"] == 330 + 55 + 10
        # 行按合计倒序（最大模型在前）
        totals = [r["total_tokens"] for r in payload["rows"]]
        assert totals == sorted(totals, reverse=True)

    def test_db_missing_degrades_to_empty(self, tmp_path: Path, monkeypatch) -> None:
        mod, _ = _make_module_with_monitor()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(tmp_path / "nonexistent.db"))
        payload = mod._collect_token_usage()
        assert payload["rows"] == []
        assert payload["total_tokens"] == 0

    def test_model_icon_prefix_rules(self) -> None:
        mod = _load_server()
        # provider 优先、模型名兜底、未命中缺省
        assert mod._model_icon("glm-5.3-flash", "zhipu_coding") == "🤖"
        assert mod._model_icon("glm-5.3-flash", "") == "🤖"
        assert mod._model_icon("some-unknown", "some-unknown") == "🔤"


class TestTokenUsageByTime:
    def test_groups_by_utc_day_desc(self, tmp_path: Path, monkeypatch) -> None:
        mod, _ = _make_module_with_monitor()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_llm_traces_db(tmp_path)))
        payload = mod._collect_token_usage_by_time()

        days = {r["date"]: r for r in payload["rows"]}
        assert set(days) == {"2026-09-01", "2026-09-02", "2026-08-30"}
        assert days["2026-09-01"]["total_tokens"] == 330
        assert days["2026-09-01"]["requests"] == 2
        assert days["2026-08-30"]["total_tokens"] == 10
        # 日期倒序（最近的在前）
        dates = [r["date"] for r in payload["rows"]]
        assert dates == sorted(dates, reverse=True)
        # 总量性质：按日行之和 = 跨运行总量（与按模型口径同源）
        assert sum(r["total_tokens"] for r in payload["rows"]) == 395

    def test_db_missing_degrades_to_empty(self, tmp_path: Path, monkeypatch) -> None:
        mod, _ = _make_module_with_monitor()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(tmp_path / "nonexistent.db"))
        payload = mod._collect_token_usage_by_time()
        assert payload["rows"] == []
        assert payload["total"] == 0

    def test_route_envelope(self, tmp_path: Path, monkeypatch) -> None:
        mod, _ = _make_module_with_monitor()
        monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_llm_traces_db(tmp_path)))
        resp = _run(mod.http_handle(path="/ext/monitoring/token-usage/by-time", method="GET"))
        assert resp["success"] is True
        body = _decode_body(resp)
        assert body["columns"][0]["key"] == "date"
        assert len(body["rows"]) == 3


# ═══════════════════════════════════════════════════════════
# M3：search/sessions 域租户过滤（fail-closed，与 tool-calls 域同纪律）
# ═══════════════════════════════════════════════════════════


def _make_runs_for_search(tmp_path: Path) -> Path:
    """runs 表两租户各一管道，驱动 _tenant_pipeline_ids 的可见集判定。"""
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, pipeline_id TEXT, tenant_id TEXT, created_at TEXT)"
    )
    conn.execute("INSERT INTO runs VALUES ('r-1', 'p-tenant-a', 'tenant-a', '2026-01-01T00:00:00')")
    conn.execute("INSERT INTO runs VALUES ('r-2', 'p-tenant-b', 'tenant-b', '2026-01-02T00:00:00')")
    conn.commit()
    conn.close()
    return db_path


def _make_sessions_for_search(tmp_path: Path) -> Path:
    """sessions 表两租户各一同名会话（真实标题源）+ runs 表供消息域可见集。"""
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, pipeline_id TEXT, tenant_id TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE sessions (thread_id TEXT PRIMARY KEY, title TEXT, tenant_id TEXT, "
        "last_active_at TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO runs VALUES ('r-a', 'p-tenant-a', 'tenant-a', '2026-01-01T00:00:00')")
    conn.execute("INSERT INTO runs VALUES ('r-b', 'p-tenant-b', 'tenant-b', '2026-01-02T00:00:00')")
    conn.execute(
        "INSERT INTO sessions VALUES ('thread-a', 'stress2-19 租户甲会话', 'tenant-a', "
        "'2026-01-03T00:00:00', '2026-01-03T00:00:00')"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('thread-b', 'stress2-19 租户乙会话', 'tenant-b', "
        "'2026-01-03T00:00:00', '2026-01-03T00:00:00')"
    )
    conn.commit()
    conn.close()
    return db_path


class TestSearchTenantFilter:
    def _load_search(self) -> Any:
        mod_name = "routes_search_tenant_test"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "routes_search.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module

    def test_tenant_pipeline_ids_scoped_to_tenant(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_runs_for_search(tmp_path)))
        mod = self._load_search()
        assert mod._tenant_pipeline_ids("tenant-a") == {"p-tenant-a"}
        assert mod._tenant_pipeline_ids("tenant-b") == {"p-tenant-b"}
        assert mod._tenant_pipeline_ids("tenant-nobody") == set()

    def test_search_missing_tenant_is_empty_not_full(self, tmp_path: Path, monkeypatch) -> None:
        # fail-closed：缺身份头返回空集，绝不退化全量搜索（跨租户消息正文泄露面）
        monkeypatch.setenv("AGENTOS_DB_PATH", str(_make_runs_for_search(tmp_path)))
        mod = self._load_search()
        result = asyncio.run(mod.search(q="秘密", type="all", limit=20, tenant_id=""))
        assert result["sessions"] == [] and result["messages"] == []

    def test_search_filters_hits_by_tenant_pipelines(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(
            "AGENTOS_DB_PATH", str(_make_sessions_for_search(tmp_path))
        )
        mod = self._load_search()
        mine = asyncio.run(mod.search(q="会话", type="session", limit=20, tenant_id="tenant-a"))
        assert [s["id"] for s in mine["sessions"]] == ["thread-a"]
        theirs = asyncio.run(mod.search(q="会话", type="session", limit=20, tenant_id="tenant-b"))
        assert [s["id"] for s in theirs["sessions"]] == ["thread-b"]

    def test_session_search_hits_sessions_table_scoped_to_tenant(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """会话标题搜索命中 sessions 表（真实标题源），命中 id=thread_id 且按租户隔离。"""
        monkeypatch.setenv(
            "AGENTOS_DB_PATH", str(_make_sessions_for_search(tmp_path))
        )
        mod = self._load_search()
        mine = asyncio.run(
            mod.search(q="stress2-19", type="session", limit=20, tenant_id="tenant-a")
        )
        assert [s["id"] for s in mine["sessions"]] == ["thread-a"]
        assert "stress2-19" in mine["sessions"][0]["title"]
        theirs = asyncio.run(
            mod.search(q="stress2-19", type="session", limit=20, tenant_id="tenant-b")
        )
        assert [s["id"] for s in theirs["sessions"]] == ["thread-b"]

    def test_session_search_title_no_hit_returns_empty(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(
            "AGENTOS_DB_PATH", str(_make_sessions_for_search(tmp_path))
        )
        mod = self._load_search()
        none = asyncio.run(
            mod.search(q="zzz-nonexistent", type="session", limit=20, tenant_id="tenant-a")
        )
        assert none["sessions"] == []
