# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""monitoring 插件（监控服务接口适配层）单元测试。

覆盖（对齐 plugins/shared/monitoring/server.py）：
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
import datetime
import importlib.util
import json
import os
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
# routes_search/kernel_reads 等模块在导入期 import 共享根的 db_read 桥
# （生产环境平铺 sys.path 含 shared 根）：单文件直跑时无其他用例代为注入，
# 这里自锚定，不依赖跨用例污染。
_SHARED_ROOT = _PLUGIN_DIR.parent
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


# ═══════════════════════════════════════════════════════════
# db-read 契约测试替身（C2 刀2）：共享替身 _libs/testing/db_read_fake
# ═══════════════════════════════════════════════════════════

from _libs.testing.db_read_fake import FakeDbRead  # noqa: E402 — 共享测试基建

# C3 替身统一身份映射（token → 租户；替身按身份租户收窄仿真）
_TOK_MAP = {"tok-a": "tenant-a", "tok-b": "tenant-b"}


def _install_fake_db_read(
    db_path: Path, token_tenants: dict[str, str] | None = None, fail: Exception | None = None
) -> FakeDbRead:
    """db-read provider 注入 kernel_reads 装配缝（用例后由 autouse 夹具还原）。"""
    import kernel_reads  # noqa: PLC0415 — 插件内核只读能力桥（同目录）

    fake = FakeDbRead(db_path, token_tenants, fail)
    kernel_reads.set_provider("db-read", fake.provider)
    return fake


@pytest.fixture(autouse=True)
def _reset_kernel_reads_providers():
    """用例后清空 kernel_reads provider 注册表（db-read 注入不跨用例渗漏）。"""
    import kernel_reads  # noqa: PLC0415

    yield
    kernel_reads.reset_providers()


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
# 监控模块装载与延迟初始化（LLM 工具面已退役，读面归 HTTP /ext/monitoring/**）
# ═══════════════════════════════════════════════════════════


class TestMonitoringTools:
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

    def test_system_metrics_route_to_thread_payload_shape(self) -> None:
        """X26-F1 行为不变契约：采样经 to_thread 后载荷字段集与状态码零变化。"""
        mod, _ = _make_module_with_monitor()
        resp = _run(mod.http_handle(path="/ext/monitoring/system/metrics", method="GET"))
        assert resp["success"] is True
        assert resp["data"]["status"] == 200
        metrics = _decode_body(resp)["metrics"]
        # 对齐前端 SystemMetrics 嵌套 TS 类型（server._collect_system_metrics 返回形）
        assert set(metrics) == {"cpu_usage", "memory", "disk", "uptime", "timestamp"}
        assert set(metrics["memory"]) == {"total", "used", "available", "usage_percent"}
        assert set(metrics["disk"]) == {"mount_point", "total", "used", "free", "usage_percent"}
        # X28-F1：标签 = 插件所在盘真实盘符（Windows 如 "D:\"；POSIX 无盘符回退 "/"）
        anchor_drive = os.path.splitdrive(str(_PLUGIN_DIR))[0]
        assert metrics["disk"]["mount_point"] == (anchor_drive + os.sep if anchor_drive else "/")
        assert metrics["uptime"] >= 0
        assert isinstance(metrics["cpu_usage"], float)

    def test_system_metrics_sampling_off_event_loop(self) -> None:
        """X26-F1 非阻塞语义：采样执行线程 ≠ 事件循环线程（旧病=事件循环内直调）。"""
        mod, _ = _make_module_with_monitor()
        sample_threads: list[int] = []

        def _fake_collect() -> dict[str, Any]:
            sample_threads.append(threading.get_ident())
            return {"cpu_usage": 1.0}

        mod._collect_system_metrics = _fake_collect  # type: ignore[method-assign]
        resp = _run(mod.http_handle(path="/ext/monitoring/system/metrics", method="GET"))
        assert resp["success"] is True
        assert _decode_body(resp)["metrics"] == {"cpu_usage": 1.0}
        # 采样被真实调用且不在主线程（_run 的事件循环跑在主线程）——
        # 若回退为事件循环内直调，此断言即红
        assert len(sample_threads) == 1
        assert sample_threads[0] != threading.main_thread().ident

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
        # 死计数器灌陈旧值：响应体必须无视之（P-149 退役，request_count 只认
        # traces 真值；本用例无 db-read 供给 → 次数降级真零）
        monitor._llm_stats = {"request_count": 7, "active_requests": 2, "error_count": 1, "total_response_time": 9.0}
        resp = _run(mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))
        payload = _decode_body(resp)
        assert payload["request_count"] == 0
        assert "active_requests" not in payload
        assert "error_count" not in payload
        assert "total_response_time" not in payload

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


class TestDiskCardRealDrive:
    """X28-F1：磁盘卡采样锚定插件实际所在盘（旧病=固定 psutil.disk_usage("/")）。

    Windows 下 "/" 固定采样语义失真：psutil 按当前进程工作目录解析出某个盘
    的数据，mount_point 标签却写 "/" 常量；多盘机器只见一盘。修复后契约：
    采样参数 = 插件 server.py 真实路径（psutil 解析其所在盘/挂载点），
    标签 = 该路径盘符（Windows 如 "D:\\"；POSIX 无盘符回退 "/"）。
    """

    @staticmethod
    def _fake_du(total: int = 100, used: int = 40, free: int = 60) -> types.SimpleNamespace:
        return types.SimpleNamespace(total=total, used=used, free=free, percent=40.0)

    def test_disk_usage_called_with_plugin_real_path(self, monkeypatch) -> None:
        """mock psutil.disk_usage：调用参数必须为插件目录真实路径，禁止 "/"。"""
        mod = _load_server()

        calls: list[str] = []

        def _fake_disk_usage(path: str) -> types.SimpleNamespace:
            calls.append(path)
            return self._fake_du()

        monkeypatch.setattr(mod.psutil, "disk_usage", _fake_disk_usage)
        metrics = mod._collect_system_metrics()
        # 锚点 = 插件目录（Windows disk_usage 只收目录，文件路径报 WinError 267）
        anchor = os.path.abspath(os.path.dirname(mod.__file__))
        # 区分度：旧实现传 "/" 常量，此断言即红
        assert calls == [anchor]
        assert calls[0] != "/"
        # total/used/free 透传该次采样（fake 值非真值即可区分透传链）
        assert metrics["disk"]["total"] == 100
        assert metrics["disk"]["used"] == 40
        assert metrics["disk"]["free"] == 60

    def test_mount_point_label_is_anchor_drive(self, monkeypatch) -> None:
        """标签 = 采样锚点盘符（测试内独立推导），非 "/" 常量。"""
        mod = _load_server()
        monkeypatch.setattr(
            mod.psutil, "disk_usage", lambda _p: self._fake_du()
        )
        metrics = mod._collect_system_metrics()
        anchor_drive = os.path.splitdrive(os.path.dirname(os.path.abspath(mod.__file__)))[0]
        expected = anchor_drive + os.sep if anchor_drive else "/"
        assert metrics["disk"]["mount_point"] == expected

    def test_mount_point_label_windows_drive_shape(self, monkeypatch) -> None:
        """Windows 形状专测：标签含盘符冒号+分隔符（如 "D:\\"）；POSIX 回退 "/"。"""
        mod = _load_server()
        monkeypatch.setattr(
            mod.psutil, "disk_usage", lambda _p: self._fake_du()
        )
        metrics = mod._collect_system_metrics()
        label = metrics["disk"]["mount_point"]
        if os.name == "nt":
            drive = os.path.splitdrive(os.path.dirname(os.path.abspath(mod.__file__)))[0]
            assert drive, "Windows 下锚点必有盘符（旧实现恒 '/' 常量，此断言即红）"
            assert label == drive + "\\"
        else:
            assert label == "/"


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

    def test_payload_diag_dir_defaults_to_user_space(self, tmp_path: Path, monkeypatch) -> None:
        """未设 AGENTOS_LOG_DIR：读端锚用户空间 logs/payload_diag（SDK 单源）。

        与写端 adapter 同一函数——插件位置推导已退役（装机布局下写端探测
        命中包内 resources、读端 env 命中用户空间，读写分叉致调试页看不到
        新调用，裁定 2026-11-02）。
        """
        mod = _load_server()
        monkeypatch.delenv("AGENTOS_LOG_DIR", raising=False)
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
        assert mod._payload_diag_dir() == str(tmp_path / "logs" / "payload_diag")

    def test_payload_diag_dir_env_override_beats_user_root(self, tmp_path: Path, monkeypatch) -> None:
        """AGENTOS_LOG_DIR 部署覆盖优先于用户空间缺省（两组区分度输入）。"""
        mod = _load_server()
        monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path / "deploy"))
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user"))
        assert mod._payload_diag_dir() == str(tmp_path / "deploy" / "logs" / "payload_diag")


# ═══════════════════════════════════════════════════════════
# diagnostics-settings：settings.yaml 诊断开关读写端点
# ═══════════════════════════════════════════════════════════


class TestDiagnosticsSettingsEndpoints:
    @staticmethod
    def _decode(resp: dict) -> dict:
        """解 http_json 信封：data.body 是 base64 的 JSON 字符串。"""
        import base64

        return json.loads(base64.b64decode(resp["data"]["body"]).decode("utf-8"))

    def _setup_sandbox(self, monkeypatch, tmp_path) -> None:
        """钉用户空间根到 tmp 并清 SDK 公共件缓存（不触真实 settings.yaml）。"""
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
        from agentos_plugin_sdk import diagnostics_settings as ds

        ds._cache.clear()

    def test_get_defaults_without_file(self, monkeypatch, tmp_path) -> None:
        """GET 无 settings.yaml → 全默认值（零操作前提）。"""
        mod = _load_server()
        self._setup_sandbox(monkeypatch, tmp_path)
        resp = _run(mod.http_handle(path="/ext/monitoring/diagnostics-settings", method="GET"))
        assert resp["success"] is True
        assert self._decode(resp)["diagnostics"] == {"payload_diag": True, "prompt_audit": False}

    def test_put_then_get_roundtrip(self, monkeypatch, tmp_path) -> None:
        """PUT 合并写 → GET 读回新值（读写同源，文件落用户空间根）。"""
        mod = _load_server()
        self._setup_sandbox(monkeypatch, tmp_path)
        resp = _run(mod.http_handle(
            path="/ext/monitoring/diagnostics-settings", method="PUT",
            raw_body='{"payload_diag": false}',
        ))
        assert resp["success"] is True
        assert self._decode(resp)["diagnostics"]["payload_diag"] is False
        assert (tmp_path / "settings.yaml").exists()
        resp2 = _run(mod.http_handle(path="/ext/monitoring/diagnostics-settings", method="GET"))
        assert self._decode(resp2)["diagnostics"]["payload_diag"] is False
        # 未提及的键保持默认
        assert self._decode(resp2)["diagnostics"]["prompt_audit"] is False

    def test_put_rejects_bad_body(self, monkeypatch, tmp_path) -> None:
        """坏 JSON / 非对象体 / 未知键 / 非 bool 值 → 400 且零写入。"""
        mod = _load_server()
        self._setup_sandbox(monkeypatch, tmp_path)
        for body in ("{not json", "[1,2]", '{"unknown_switch": true}', '{"payload_diag": "yes"}'):
            resp = _run(mod.http_handle(
                path="/ext/monitoring/diagnostics-settings", method="PUT", raw_body=body,
            ))
            assert resp["success"] is True
            assert resp["data"]["status"] == 400
        assert not (tmp_path / "settings.yaml").exists()

    def test_webview_pages(self) -> None:
        mod, _ = _make_module_with_monitor()
        resp = _run(mod.http_handle(path="/ext/monitoring/page/tool-calls", method="GET"))
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
    conn.execute("CREATE TABLE traces (trace_id TEXT, pipeline_id TEXT, created_at TEXT, plugin_id TEXT, patch_data TEXT, tenant_id TEXT)")
    conn.execute(
        "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?)",
        (
            "tr-1",
            "pipe-1",
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
            "pipe-1",
            "2026-01-01T00:01:00",
            "core",
            json.dumps({"tool_results": [{"tool_name": "file_read", "success": True, "data": "x", "duration_ms": 3.0}]}),
            "tenant-a",
        ),
    )
    # 负控制①：插件 id 恰为 pipeline_tool_core 但 patch_data 无 tool_results。
    conn.execute(
        "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?)",
        ("tr-3", "pipe-2", "2026-01-01T00:02:00", "pipeline_tool_core", json.dumps({"raw_result": "你好"}), "tenant-b"),
    )
    # 负控制②：普通 step 行，无工具结果。
    conn.execute(
        "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?)",
        ("tr-4", "pipe-2", "2026-01-01T00:03:00", "prepare", json.dumps({"tool_schemas_count": 41}), "tenant-b"),
    )
    conn.commit()
    conn.close()
    return db_path


def _make_windowed_db(
    tmp_path: Path,
    total: int,
    tool_results_by_pos: dict[int, list[dict[str, Any]]],
    tenant: str = "tenant-a",
) -> Path:
    """窗口口径测试库：total 条 trace 按 created_at 升序落库（pos 1..total，
    pos=1 最旧 / pos=total 最新），仅 tool_results_by_pos 指定的位置携带
    tool_results，其余为无工具结果普通 step diff。

    窗口边界测试以「最近 N 条」为参照系，pos 倒着数更直观（最近 50 条 =
    pos total-49..total）。
    """
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE traces (trace_id TEXT, pipeline_id TEXT, created_at TEXT, plugin_id TEXT, patch_data TEXT, tenant_id TEXT)"
    )
    rows = []
    for pos in range(1, total + 1):
        results = tool_results_by_pos.get(pos)
        patch: dict[str, Any] = {"tool_results": results} if results else {"raw_result": "no tools"}
        created = f"2026-01-01T{pos // 60:02d}:{pos % 60:02d}:00"
        rows.append((f"tr-{pos}", "pipe-1", created, "core", json.dumps(patch), tenant))
    conn.executemany("INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db_path


class TestToolCalls:
    def test_query_tool_calls_all(self, tmp_path: Path, monkeypatch) -> None:
        mod = _load_server()
        _install_fake_db_read(_make_kernel_db(tmp_path), _TOK_MAP)
        result = _run(mod._query_tool_calls({}, "tenant-a", "tok-a"))
        # tenant-a 两行 tool-carrying step 轨迹共 3 条结果；负控制两行（含
        # plugin_id 恰为 pipeline_tool_core 的行）均不入选——内容谓词，与
        # plugin_id 无关。
        assert result["total"] == 3
        assert {i["tool_name"] for i in result["items"]} == {"bash_execute", "file_write", "file_read"}

    def test_query_tool_calls_filters(self, tmp_path: Path, monkeypatch) -> None:
        mod = _load_server()
        _install_fake_db_read(_make_kernel_db(tmp_path), _TOK_MAP)
        # 按工具名
        by_tool = _run(mod._query_tool_calls({"tool_name": "bash_execute"}, "tenant-a", "tok-a"))
        assert by_tool["total"] == 1 and by_tool["items"][0]["tool_name"] == "bash_execute"
        # 按状态 error
        by_status = _run(mod._query_tool_calls({"status": "error"}, "tenant-a", "tok-a"))
        assert by_status["total"] == 1 and by_status["items"][0]["success"] == 0
        assert by_status["items"][0]["error"] == "denied"
        # 按最小耗时
        by_dur = _run(mod._query_tool_calls({"min_duration": "1000"}, "tenant-a", "tok-a"))
        assert by_dur["total"] == 1 and by_dur["items"][0]["tool_name"] == "file_write"
        # 非法 min_duration：忽略过滤条件（修复后：不追加 SQL 占位符、
        # 查询正常返回全部结果，不再因绑定数不匹配报错）
        ok = _run(mod._query_tool_calls({"min_duration": "abc"}, "tenant-a", "tok-a"))
        assert ok["total"] == 3
        # limit 钳制
        clamped = _run(mod._query_tool_calls({"limit": "999"}, "tenant-a", "tok-a"))
        assert clamped["total"] == 3

    def test_query_tool_calls_window_small_table_ordering(self, tmp_path: Path, monkeypatch) -> None:
        """形状1（小表，全部在窗口内）：明细窗口 = 最近 limit 条 trace →
        全部工具结果入选，行序按 created_at 倒序（最新 trace 的行在前）。"""
        mod = _load_server()
        _install_fake_db_read(_make_windowed_db(
            tmp_path,
            3,
            {
                1: [
                    {"tool_name": "bash_execute", "success": True, "duration_ms": 50.0},
                    {"tool_name": "file_write", "success": False, "error": "denied", "duration_ms": 2500.0},
                ],
                2: [{"tool_name": "file_read", "success": True, "duration_ms": 3.0}],
                # pos3 无工具结果：不产生行，但占窗口位
            },
        ), _TOK_MAP)
        result = _run(mod._query_tool_calls({}, "tenant-a", "tok-a"))
        assert result["total"] == 3
        created = [i["created_at"] for i in result["items"]]
        # 性质断言：created_at 非增（窗口内倒序）
        assert created == sorted(created, reverse=True)
        # 最新携带工具结果的 trace（pos2）的行排最前
        assert result["items"][0]["tool_name"] == "file_read"

    def test_query_tool_calls_window_excludes_out_of_window(self, tmp_path: Path, monkeypatch) -> None:
        """形状2（窗口边界）：明细窗口 = 最近 limit（默认 50）条 trace（无论
        是否含工具结果）；窗口外旧 trace 的工具结果不入选。60 条中仅最旧 1 条
        带工具结果、最新 50 条全无 → 默认窗口恒空。"""
        mod = _load_server()
        _install_fake_db_read(_make_windowed_db(
            tmp_path, 60, {1: [{"tool_name": "old_tool", "success": True, "duration_ms": 5.0}]},
        ), _TOK_MAP)
        assert _run(mod._query_tool_calls({}, "tenant-a", "tok-a"))["total"] == 0
        # 性质断言：窗口扩大到覆盖全部 60 条 → 窗口外数据回来（窗口单调）
        expanded = _run(mod._query_tool_calls({"limit": "60"}, "tenant-a", "tok-a"))
        assert expanded["total"] == 1
        assert expanded["items"][0]["tool_name"] == "old_tool"

    def test_query_tool_calls_window_cap_200(self, tmp_path: Path, monkeypatch) -> None:
        """limit 上限 200 同时是窗口上限：limit=999 钳到 200。210 条中工具
        结果恰放在最近 200 条边界两侧（pos10 出窗 / pos11 入窗）→ 单库同时
        钉钳制后窗口的入窗与出窗。"""
        mod = _load_server()
        _install_fake_db_read(_make_windowed_db(
            tmp_path,
            210,
            {
                10: [{"tool_name": "edge_out", "success": True, "duration_ms": 5.0}],
                11: [{"tool_name": "edge_in", "success": True, "duration_ms": 5.0}],
            },
        ), _TOK_MAP)
        clamped = _run(mod._query_tool_calls({"limit": "999"}, "tenant-a", "tok-a"))
        assert clamped["total"] == 1
        assert clamped["items"][0]["tool_name"] == "edge_in"

    def test_query_tool_calls_row_limit_within_window(self, tmp_path: Path, monkeypatch) -> None:
        """limit 仍钳明细行数：60 条 trace 各带 2 条结果（窗口 50 条展开
        100 行）→ 只返回最新 50 行，且行序保持 created_at 倒序。"""
        mod = _load_server()
        _install_fake_db_read(_make_windowed_db(
            tmp_path,
            60,
            {
                pos: [
                    {"tool_name": f"t{pos}", "success": True, "duration_ms": 1.0},
                    {"tool_name": f"u{pos}", "success": True, "duration_ms": 1.0},
                ]
                for pos in range(1, 61)
            },
        ), _TOK_MAP)
        result = _run(mod._query_tool_calls({}, "tenant-a", "tok-a"))
        assert result["total"] == 50
        created = [i["created_at"] for i in result["items"]]
        assert created == sorted(created, reverse=True)

    def test_query_tool_calls_tenant_isolation(self, tmp_path: Path, monkeypatch) -> None:
        """两租户数据互不可见：tenant-b 的行（含无工具结果的负控制）对
        tenant-a 查询不可见，反之亦然。"""
        mod = _load_server()
        _install_fake_db_read(_make_kernel_db(tmp_path), _TOK_MAP)
        a = _run(mod._query_tool_calls({}, "tenant-a", "tok-a"))
        assert a["total"] == 3
        assert all(i["pipeline_id"] == "pipe-1" for i in a["items"])
        # tenant-b 的 traces 行 patch_data 无 tool_results → 工具查询恒空；
        # 租户互斥由 tenant-a 查不到 tenant-b 的 run-2 行证明。
        b = _run(mod._query_tool_calls({}, "tenant-b", "tok-b"))
        assert b["items"] == [] and b["total"] == 0
        # 租户不存在 → 空集（查询域内无该租户数据）
        ghost = _run(mod._query_tool_calls({}, "tenant-ghost", "tok-ghost"))
        assert ghost["items"] == [] and ghost["total"] == 0

    def test_query_tool_calls_missing_tenant_fails_closed(self, tmp_path: Path, monkeypatch) -> None:
        """身份头缺失（空租户）→ 空集而非全量查询（fail-closed，防跨租户泄露）。"""
        mod = _load_server()
        _install_fake_db_read(_make_kernel_db(tmp_path), _TOK_MAP)
        result = _run(mod._query_tool_calls({}, ""))
        assert result["items"] == [] and result["total"] == 0
        assert "tenant" in result["error"]

    def test_query_tool_calls_db_missing(self, tmp_path: Path, monkeypatch) -> None:
        mod = _load_server()
        _install_fake_db_read(tmp_path / "nonexistent.db", _TOK_MAP)
        result = _run(mod._query_tool_calls({}, "tenant-a", "tok-a"))
        assert result["items"] == [] and result["error"]

    def test_query_tool_calls_db_error(self, tmp_path: Path, monkeypatch) -> None:
        """数据库损坏 → 返回错误而非崩溃。"""
        mod = _load_server()
        db_path = tmp_path / "bad.db"
        db_path.write_bytes(b"not a sqlite file")
        _install_fake_db_read(db_path, _TOK_MAP)
        result = _run(mod._query_tool_calls({}, "tenant-a", "tok-a"))
        assert result["items"] == [] and result["error"]

    def test_tool_calls_route(self, tmp_path: Path, monkeypatch) -> None:
        """http.handle 路由把内核注入的 x-agentos-tenant 头传给租户过滤查询。"""
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_kernel_db(tmp_path), _TOK_MAP)
        resp = _run(mod.http_handle(
            path="/ext/monitoring/tool-calls",
            method="GET",
            headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"},
        ))
        payload = _decode_body(resp)
        assert payload["total"] == 3

    def test_tool_calls_route_missing_tenant_fails_closed(self, tmp_path: Path, monkeypatch) -> None:
        """无身份头（非内核认证分发）→ 空集而非全量（fail-closed）。"""
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_kernel_db(tmp_path), _TOK_MAP)
        resp = _run(mod.http_handle(path="/ext/monitoring/tool-calls", method="GET"))
        payload = _decode_body(resp)
        assert payload["items"] == [] and payload["total"] == 0


class TestToolCallStats:
    """按工具聚合统计：计数/成败细分/平均耗时/窗口口径 + 租户 fail-closed。"""

    def test_stats_aggregation(self, tmp_path: Path, monkeypatch) -> None:
        mod = _load_server()
        _install_fake_db_read(_make_kernel_db(tmp_path), _TOK_MAP)
        result = _run(mod._query_tool_call_stats({}, "tenant-a", "tok-a"))
        # 窗口内 3 条调用 → bash_execute/file_read 各 1 次、file_write 1 次
        # （其 1 次为失败）；平均耗时为算术平均（file_write 仅 2500ms 一条）
        assert result["total"] == 3
        by_tool = {i["tool_name"]: i for i in result["items"]}
        assert by_tool["bash_execute"]["calls"] == 1
        assert by_tool["bash_execute"]["success_calls"] == 1
        assert by_tool["bash_execute"]["error_calls"] == 0
        assert by_tool["file_write"]["error_calls"] == 1
        assert by_tool["file_write"]["avg_duration_ms"] == 2500.0
        assert by_tool["file_read"]["success_calls"] == 1
        # 按调用次数倒序不适用于并列次数（全为 1），但性质上 calls 总和 = 明细总数
        assert sum(i["calls"] for i in result["items"]) == 3
        # 窗口字段回显（诚实口径：非全生命周期累计）
        assert result["window"] == 500

    def test_stats_window_excludes_out_of_window(self, tmp_path: Path, monkeypatch) -> None:
        """形状2（窗口边界）：聚合窗口 = 最近 limit（默认 500）条 trace（含
        无工具结果行）；窗口外旧 trace 不计入统计。501 条中仅最旧 1 条带工具
        结果 → 默认窗口聚合为空。"""
        mod = _load_server()
        _install_fake_db_read(_make_windowed_db(
            tmp_path, 501, {1: [{"tool_name": "old_tool", "success": True, "duration_ms": 5.0}]},
        ), _TOK_MAP)
        result = _run(mod._query_tool_call_stats({}, "tenant-a", "tok-a"))
        assert result["items"] == [] and result["total"] == 0
        assert result["window"] == 500
        # 性质断言：窗口扩大覆盖全部 501 条 → 窗口外工具计入（窗口单调）
        expanded = _run(mod._query_tool_call_stats({"limit": "2000"}, "tenant-a", "tok-a"))
        assert expanded["total"] == 1
        assert expanded["window"] == 2000
        assert expanded["items"][0]["tool_name"] == "old_tool"

    def test_stats_window_aggregates_only_inside(self, tmp_path: Path, monkeypatch) -> None:
        """形状3（窗口内混合聚合）：无工具结果行占窗口位但不产生计数；
        聚合列（calls/success/error/avg）与按次数倒序在窗口内成立。"""
        mod = _load_server()
        _install_fake_db_read(_make_windowed_db(
            tmp_path,
            4,
            {
                1: [{"tool_name": "b", "success": True, "duration_ms": 50.0}],
                4: [
                    {"tool_name": "a", "success": True, "duration_ms": 100.0},
                    {"tool_name": "a", "success": False, "error": "x", "duration_ms": 300.0},
                ],
            },
        ), _TOK_MAP)
        result = _run(mod._query_tool_call_stats({}, "tenant-a", "tok-a"))
        by_tool = {i["tool_name"]: i for i in result["items"]}
        assert by_tool["a"]["calls"] == 2
        assert by_tool["a"]["success_calls"] == 1
        assert by_tool["a"]["error_calls"] == 1
        assert by_tool["a"]["avg_duration_ms"] == 200.0
        assert by_tool["b"]["calls"] == 1
        # 按调用次数倒序：calls 高者在前；总调用数守恒（= 窗口内明细行数）
        assert [i["tool_name"] for i in result["items"]][0] == "a"
        assert sum(i["calls"] for i in result["items"]) == 3

    def test_stats_tenant_isolation_and_fail_closed(self, tmp_path: Path, monkeypatch) -> None:
        mod = _load_server()
        _install_fake_db_read(_make_kernel_db(tmp_path), _TOK_MAP)
        # 缺身份 → 空集 fail-closed
        missing = _run(mod._query_tool_call_stats({}, ""))
        assert missing["items"] == [] and "tenant" in missing["error"]
        # 租户互斥：tenant-b 无含工具结果行 → 空统计
        b = _run(mod._query_tool_call_stats({}, "tenant-b", "tok-b"))
        assert b["items"] == [] and b["total"] == 0

    def test_stats_route(self, tmp_path: Path, monkeypatch) -> None:
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_kernel_db(tmp_path), _TOK_MAP)
        resp = _run(mod.http_handle(
            path="/ext/monitoring/tool-calls/stats",
            method="GET",
            headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"},
        ))
        payload = _decode_body(resp)
        assert payload["total"] == 3
        assert {i["tool_name"] for i in payload["items"]} == {"bash_execute", "file_write", "file_read"}



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
                                "name": "process.memory_commit_bytes",
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
        assert rows["llm"]["memory_commit_mb"] == 50.0
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
    """造内核 DB 夹具：traces（按时间维度真值）+ pipeline_state（累计真值）。

    累计口径（token-usage / 按模型表）读 pipeline_state 的 track.llm_usage +
    llm_model；时间维度（by-time）读 traces 的逐轮 llm_usage。两表同库，
    此处一并造出，保持两端点各有其源。
    """
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE traces (trace_id TEXT, pipeline_id TEXT, created_at TEXT,"
        " plugin_id TEXT, patch_data TEXT, tenant_id TEXT)"
    )
    usage_rows = [
        ("tr-1", "2026-09-01T08:00:00+00:00", {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "cached_tokens": 80, "model": "deepseek-v4-flash", "provider": "deepseek"}),
        ("tr-2", "2026-09-01T09:30:00+00:00", {"input_tokens": 200, "output_tokens": 20, "total_tokens": 220, "cached_tokens": 100, "model": "deepseek-v4-flash", "provider": "deepseek"}),
        ("tr-3", "2026-09-02T14:00:00+00:00", {"input_tokens": 50, "output_tokens": 5, "total_tokens": 55, "cached_tokens": 45, "model": "MiniMax-M3", "provider": "minimax"}),
        # tr-4 为早期轨迹：无 cached 字段（按 0 计口径的回归样本）
        ("tr-4", "2026-08-30T12:00:00+00:00", {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}),
    ]
    for tid, ts, usage in usage_rows:
        conn.execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?)",
            (tid, "pipe-1", ts, "core", json.dumps({"llm_usage": usage}), "tenant-a"),
        )
    # pipeline_state：累计真值（与 traces 的逐轮和同量——两源一致是设计契约）
    conn.execute(
        "CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT,"
        " value TEXT, value_kind TEXT DEFAULT 'str', tenant_id TEXT, updated_at TEXT)"
    )
    state_rows = [
        # deepseek：两个管道各 110 / 220（合计 330，与 traces 两轮之和一致）；
        # 缓存命中 80 / 100 相加为 180（命中率 180/300 = 60%）
        ("pipe-a", "track.llm_usage", {"total_input_tokens": 100, "total_output_tokens": 10, "total_tokens": 110, "total_cached_tokens": 80, "last_input_tokens": 100, "last_output_tokens": 10}),
        ("pipe-a", "llm_model", "deepseek-v4-flash"),
        ("pipe-b", "track.llm_usage", {"total_input_tokens": 200, "total_output_tokens": 20, "total_tokens": 220, "total_cached_tokens": 100, "last_input_tokens": 200, "last_output_tokens": 20}),
        ("pipe-b", "llm_model", "deepseek-v4-flash"),
        # MiniMax：单管道 55（cached 45 → 命中率 90%）
        ("pipe-c", "track.llm_usage", {"total_input_tokens": 50, "total_output_tokens": 5, "total_tokens": 55, "total_cached_tokens": 45, "last_input_tokens": 50, "last_output_tokens": 5}),
        ("pipe-c", "llm_model", "MiniMax-M3"),
        # 无 model 归属的早期管道：归「（未记录模型）」行；无 cached 字段（按 0 计）
        ("pipe-d", "track.llm_usage", {"total_input_tokens": 7, "total_output_tokens": 3, "total_tokens": 10, "last_input_tokens": 7, "last_output_tokens": 3}),
    ]
    for pid, key, val in state_rows:
        if isinstance(val, str):
            conn.execute(
                "INSERT INTO pipeline_state VALUES (?, ?, ?, 'str', 'tenant-a', '2026-09-01T00:00:00Z')",
                (pid, key, val),
            )
        else:
            conn.execute(
                "INSERT INTO pipeline_state VALUES (?, ?, ?, 'json', 'tenant-a', '2026-09-01T00:00:00Z')",
                (pid, key, json.dumps(val)),
            )
    conn.commit()
    conn.close()
    return db_path


class TestTokenUsageByModel:
    def test_groups_by_model_with_icons(self, tmp_path: Path, monkeypatch) -> None:
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_llm_traces_db(tmp_path), _TOK_MAP)
        payload = _run(mod._collect_token_usage("tenant-a", "tok-a"))

        rows = {r["model"]: r for r in payload["rows"]}
        assert set(rows) == {"deepseek-v4-flash", "MiniMax-M3", "（未记录模型）"}
        # 同模型多管道聚合：各自的累计相加（state 按管道自持累计）
        assert rows["deepseek-v4-flash"]["input_tokens"] == 300
        assert rows["deepseek-v4-flash"]["output_tokens"] == 30
        assert rows["deepseek-v4-flash"]["total_tokens"] == 330
        assert rows["MiniMax-M3"]["total_tokens"] == 55
        assert rows["（未记录模型）"]["total_tokens"] == 10
        # 缓存命中逐行口径：cached/missed 相加，pct 按行累计值派生（两行命中率有区分度）
        assert rows["deepseek-v4-flash"]["cached_tokens"] == 180
        assert rows["deepseek-v4-flash"]["missed_tokens"] == 120
        assert rows["deepseek-v4-flash"]["cache_hit_pct"] == 60.0
        assert rows["MiniMax-M3"]["cached_tokens"] == 45
        assert rows["MiniMax-M3"]["missed_tokens"] == 5
        assert rows["MiniMax-M3"]["cache_hit_pct"] == 90.0
        # 早期管道无 cached 字段：按 0 计（missed = input，不产生负 missed）
        assert rows["（未记录模型）"]["cached_tokens"] == 0
        assert rows["（未记录模型）"]["missed_tokens"] == 7
        assert rows["（未记录模型）"]["cache_hit_pct"] == 0.0
        # 性质断言：cached ≤ input 且 pct 公式逐行成立
        for row in payload["rows"]:
            assert 0 <= row["cached_tokens"] <= row["input_tokens"]
            if row["input_tokens"] > 0:
                assert row["cache_hit_pct"] == round(
                    row["cached_tokens"] / row["input_tokens"] * 100, 1
                )
            else:
                assert row["cache_hit_pct"] == 0.0
        # 图标按模型名前缀映射；无归属回退缺省
        assert rows["deepseek-v4-flash"]["icon"] == "🐋"
        assert rows["MiniMax-M3"]["icon"] == "🐚"
        assert rows["（未记录模型）"]["icon"] == "🔤"

    def test_totals_equal_sum_of_model_rows(self, tmp_path: Path, monkeypatch) -> None:
        # 性质断言：总量卡口径 = 各模型行之和（同一查询源，不许两套账）
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_llm_traces_db(tmp_path), _TOK_MAP)
        payload = _run(mod._collect_token_usage("tenant-a", "tok-a"))
        assert payload["prompt_tokens"] == sum(r["input_tokens"] for r in payload["rows"])
        assert payload["completion_tokens"] == sum(r["output_tokens"] for r in payload["rows"])
        assert payload["total_tokens"] == 330 + 55 + 10
        # 缓存命中总量同源：cached/missed = 各行之和，pct 按总量派生（225/357 = 63.0）
        assert payload["cached_tokens"] == sum(r["cached_tokens"] for r in payload["rows"])
        assert payload["cached_tokens"] == 180 + 45 + 0
        assert payload["missed_tokens"] == 132
        assert payload["cache_hit_pct"] == 63.0
        assert payload["cache_hit_pct"] == round(
            payload["cached_tokens"] / payload["prompt_tokens"] * 100, 1
        )
        # 行按合计倒序（最大模型在前）
        totals = [r["total_tokens"] for r in payload["rows"]]
        assert totals == sorted(totals, reverse=True)

    def test_db_missing_degrades_to_empty(self, tmp_path: Path, monkeypatch) -> None:
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(tmp_path / "nonexistent.db", _TOK_MAP)
        payload = _run(mod._collect_token_usage("tenant-a", "tok-a"))
        assert payload["rows"] == []
        assert payload["total_tokens"] == 0
        # 降级路径缓存命中字段同步归 0（状态卡不显示脏值）
        assert payload["cached_tokens"] == 0
        assert payload["missed_tokens"] == 0
        assert payload["cache_hit_pct"] == 0.0

    def test_route_serves_widget_shape_top_level(self, tmp_path: Path, monkeypatch) -> None:
        """token-usage 路由响应体即 widget 消费形（顶层 rows/columns/labels/datasets）。

        前端 widget 取数链只解顶层 rows（表格）/datasets（图表）——响应必须与
        by-time 端点同形；具名信封包裹会让按模型表/图在数据存在时恒显无数据。
        """
        mod, monitor = _make_module_with_monitor()
        _install_fake_db_read(_make_llm_traces_db(tmp_path), _TOK_MAP)
        # 死计数器灌陈旧值：响应体必须无视之（P-149 退役；request_count 只认
        # traces 真值——夹具 4 条 llm_usage 轨迹，陈旧 7 与真值 4 有区分度）
        monitor._llm_stats = {"request_count": 7, "active_requests": 2, "error_count": 1, "total_response_time": 9.0}
        resp = _run(mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))
        assert resp["success"] is True
        body = _decode_body(resp)
        assert [c["key"] for c in body["columns"]][:2] == ["icon", "model"]
        assert len(body["rows"]) == 3
        # 缓存命中列在列尾（「合计 Tokens」之后），图表序列含缓存命中
        assert [c["key"] for c in body["columns"]][-2:] == ["cached_tokens", "cache_hit_pct"]
        assert body["datasets"][-1]["label"] == "缓存命中 Tokens"
        assert body["datasets"][-1]["data"] == [r["cached_tokens"] for r in body["rows"]]
        # 图表形状与表格同源同值
        assert body["labels"] == [r["model"] for r in body["rows"]]
        # 调用次数真值（P-147）：traces 逐轮 COUNT 按模型落行，顶层 = 各行之和
        rows_by_model = {r["model"]: r for r in body["rows"]}
        assert rows_by_model["deepseek-v4-flash"]["requests"] == 2
        assert rows_by_model["MiniMax-M3"]["requests"] == 1
        assert rows_by_model["（未记录模型）"]["requests"] == 1
        assert body["request_count"] == sum(r["requests"] for r in body["rows"]) == 4
        # 总量 = 各模型行之和（单一账本，不许两套口径）
        assert body["total_tokens"] == sum(r["total_tokens"] for r in body["rows"])
        assert body["cached_tokens"] == sum(r["cached_tokens"] for r in body["rows"])

    def test_token_card_valuekey_resolves_served_payload(self, tmp_path: Path, monkeypatch) -> None:
        """plugin.json 用量组状态卡的 valueKey 必须能从路由响应体按点号路径取到值。

        ui_schema 声明与端点响应形的跨工件一致性锁：valueKey 解析落空 = 状态卡
        恒空；两端点形态与声明必须同刀变更。覆盖 token_card（累计 Tokens）与
        token_cache_card（缓存命中率，同一端点不同 valueKey）。
        """
        manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        cards = [w for w in manifest["ui_schema"]["widgets"] if w["id"] in ("token_card", "token_cache_card")]
        assert {c["id"] for c in cards} == {"token_card", "token_cache_card"}
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_llm_traces_db(tmp_path), _TOK_MAP)
        resp = _run(mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))
        body = _decode_body(resp)
        for card in cards:
            value_key = card["props"]["valueKey"]
            cur: Any = body
            for seg in value_key.split("."):
                assert isinstance(cur, dict) and seg in cur, f"valueKey {value_key!r} 在响应体解析落空"
                cur = cur[seg]
            assert isinstance(cur, (int, float)) and cur > 0

    def test_model_icon_prefix_rules(self) -> None:
        mod = _load_server()
        # provider 优先、模型名兜底、未命中缺省
        assert mod._model_icon("glm-5.3-flash", "zhipu_coding") == "🤖"
        assert mod._model_icon("glm-5.3-flash", "") == "🤖"
        assert mod._model_icon("some-unknown", "some-unknown") == "🔤"


class TestTokenUsageByTime:
    def test_groups_by_utc_day_desc(self, tmp_path: Path, monkeypatch) -> None:
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_llm_traces_db(tmp_path), _TOK_MAP)
        payload = _run(mod._collect_token_usage_by_time("tenant-a", "tok-a"))

        days = {r["date"]: r for r in payload["rows"]}
        assert set(days) == {"2026-09-01", "2026-09-02", "2026-08-30"}
        assert days["2026-09-01"]["total_tokens"] == 330
        assert days["2026-09-01"]["requests"] == 2
        assert days["2026-08-30"]["total_tokens"] == 10
        # 按日缓存命中：逐轮 cached_tokens SUM 到日粒度（两轮 80+100=180 → 60%）
        assert days["2026-09-01"]["cached_tokens"] == 180
        assert days["2026-09-01"]["missed_tokens"] == 120
        assert days["2026-09-01"]["cache_hit_pct"] == 60.0
        assert days["2026-09-02"]["cached_tokens"] == 45
        assert days["2026-09-02"]["missed_tokens"] == 5
        assert days["2026-09-02"]["cache_hit_pct"] == 90.0
        # 早期轨迹无 cached 字段：按 0 计（missed = input）
        assert days["2026-08-30"]["cached_tokens"] == 0
        assert days["2026-08-30"]["missed_tokens"] == 7
        assert days["2026-08-30"]["cache_hit_pct"] == 0.0
        # 日期倒序（最近的在前）
        dates = [r["date"] for r in payload["rows"]]
        assert dates == sorted(dates, reverse=True)
        # 总量性质：按日行之和 = 跨运行总量（与按模型口径同源）
        assert sum(r["total_tokens"] for r in payload["rows"]) == 395
        assert sum(r["cached_tokens"] for r in payload["rows"]) == 180 + 45 + 0
        # 性质断言：cached ≤ input 且 pct 公式逐日成立
        for row in payload["rows"]:
            assert 0 <= row["cached_tokens"] <= row["input_tokens"]
            if row["input_tokens"] > 0:
                assert row["cache_hit_pct"] == round(
                    row["cached_tokens"] / row["input_tokens"] * 100, 1
                )
            else:
                assert row["cache_hit_pct"] == 0.0

    def test_db_missing_degrades_to_empty(self, tmp_path: Path, monkeypatch) -> None:
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(tmp_path / "nonexistent.db", _TOK_MAP)
        payload = _run(mod._collect_token_usage_by_time("tenant-a", "tok-a"))
        assert payload["rows"] == []
        assert payload["total"] == 0

    def test_route_envelope(self, tmp_path: Path, monkeypatch) -> None:
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_llm_traces_db(tmp_path), _TOK_MAP)
        resp = _run(mod.http_handle(path="/ext/monitoring/token-usage/by-time", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))
        assert resp["success"] is True
        body = _decode_body(resp)
        assert body["columns"][0]["key"] == "date"
        assert len(body["rows"]) == 3
        # 缓存命中列在列尾，图表序列与 rows 同源同值
        assert [c["key"] for c in body["columns"]][-2:] == ["cached_tokens", "cache_hit_pct"]
        assert body["datasets"][-1] == {
            "label": "缓存命中 Tokens",
            "data": [r["cached_tokens"] for r in body["rows"]],
        }


def _patch_counting_aggregate(
    mod: Any, monkeypatch, calls: list[int], sleep_secs: float = 0.0
) -> None:
    """数据层计数桩：两条聚合 collect 路径都计数并可按需注入耗时。

    直接包 collect 函数本身（而非其内部 SQL 模块）——collect 是缓存层的
    真实调用对象，实现内部换源不影响本桩的观测语义。桩作用在调用方传入的
    模块实例上（_load_server 每次重建模块，不能按模块名取）。
    """
    import asyncio as _asyncio

    def wrap(real: Any) -> Any:
        async def counting(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if sleep_secs > 0:
                # async 聚合路径没有同步阻塞可睡：用事件循环睡眠等效注入耗时
                await _asyncio.sleep(sleep_secs)
            return await real(*args, **kwargs)

        return counting

    monkeypatch.setattr(mod, "_collect_token_usage", wrap(mod._collect_token_usage))
    monkeypatch.setattr(
        mod, "_collect_token_usage_by_time", wrap(mod._collect_token_usage_by_time)
    )


class TestTokenAggregationBurstCache:
    """traces 聚合单次全表 JSON 解析 ~1.3s（1.4GB patch_data）且 sidecar 单线程
    串行——监控页首屏多数据源并发直查会排队撞内核路由 timeout_ms（实测 7 并发
    4 发 504）。路由层必须按 TTL 合并同键重复/并发查询。"""

    def test_repeated_route_call_reuses_cached_aggregation(self, tmp_path: Path, monkeypatch) -> None:
        """TTL 内重复请求不重算聚合（数据层只打一次），且返回载荷一致。"""
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_llm_traces_db(tmp_path), _TOK_MAP)
        calls: list[int] = []
        _patch_counting_aggregate(mod, monkeypatch, calls)

        r1 = _run(mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))
        r2 = _run(mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))

        assert len(calls) == 1
        assert _decode_body(r1) == _decode_body(r2)
        assert len(_decode_body(r1)["rows"]) == 3

    def test_concurrent_route_calls_collapse_to_one_aggregation(self, tmp_path: Path, monkeypatch) -> None:
        """并发突发只算一次（否则 3×0.5s 串行排队必然撞路由超时）。"""
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_llm_traces_db(tmp_path), _TOK_MAP)
        calls: list[int] = []
        _patch_counting_aggregate(mod, monkeypatch, calls, sleep_secs=0.5)

        async def burst() -> list[dict[str, Any]]:
            return list(
                await asyncio.gather(
                    *[mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}) for _ in range(3)]
                )
            )

        t0 = time.monotonic()
        responses = _run(burst())
        elapsed = time.monotonic() - t0

        assert len(calls) == 1
        assert elapsed < 1.4
        bodies = [_decode_body(r) for r in responses]
        assert all(b == bodies[0] for b in bodies)

    def test_by_time_cache_is_key_scoped(self, tmp_path: Path, monkeypatch) -> None:
        """两个聚合端点各持缓存键：互不串数据，且各自只算一次。"""
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_llm_traces_db(tmp_path), _TOK_MAP)
        calls: list[int] = []
        _patch_counting_aggregate(mod, monkeypatch, calls)

        _run(mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))
        _run(mod.http_handle(path="/ext/monitoring/token-usage/by-time", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))
        _run(mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))
        _run(mod.http_handle(path="/ext/monitoring/token-usage/by-time", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))

        assert len(calls) == 2

    def test_ttl_expiry_recomputes(self, tmp_path: Path, monkeypatch) -> None:
        """TTL 归零后重新聚合（缓存不越过声明周期）。"""
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_llm_traces_db(tmp_path), _TOK_MAP)
        calls: list[int] = []
        _patch_counting_aggregate(mod, monkeypatch, calls)
        monkeypatch.setattr(mod, "_AGG_CACHE_TTL_SECS", 0.0)

        _run(mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))
        _run(mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))

        assert len(calls) == 2

    def test_slow_aggregation_does_not_block_other_routes(self, tmp_path: Path, monkeypatch) -> None:
        """慢聚合期间同进程其它端点不得排队（队头阻塞回归护栏）。

        monitoring 与 task_service / cost_control 合宿同一 sidecar 进程：聚合若在
        事件循环内同步执行，轻量端点（内存直读的 cache-stats）会被拖到聚合耗时
        量级（实测 0.00s → 1.7s）。同步 sqlite 调用必须卸载到工作线程。
        """
        mod, _ = _make_module_with_monitor()
        _install_fake_db_read(_make_llm_traces_db(tmp_path), _TOK_MAP)
        slow = 0.5
        _patch_counting_aggregate(mod, monkeypatch, [], sleep_secs=slow)

        async def scenario() -> float:
            # 起点必须在慢任务进入执行之前：若聚合在事件循环内同步跑，接下来的
            # 轻量请求要等它整个跑完才被调度——被阻塞的时长即这里测到的 elapsed。
            t0 = time.monotonic()
            slow_task = asyncio.ensure_future(
                mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"})
            )
            await asyncio.sleep(0)  # 仅让出一次控制权，不消耗聚合耗时
            light = await mod.http_handle(path="/ext/monitoring/cache-stats", method="GET")
            elapsed = time.monotonic() - t0
            assert light["success"] is True
            await slow_task
            return elapsed

        elapsed = _run(scenario())
        assert elapsed < slow / 2, f"轻量端点被慢聚合阻塞了 {elapsed:.2f}s（应远小于 {slow}s）"

    def test_failed_aggregation_does_not_poison_cache(self, tmp_path: Path, monkeypatch) -> None:
        """聚合失败降级不崩，且 TTL 过期后自愈（不永久卡在降级态）。

        设计契约：_collect_token_usage 内部对契约失败降级空行（读面不崩），
        该降级结果在 TTL 内复用（避免对已故障的数据面打风暴），超时后重新计算
        恢复。故障注入在 db-read 替身（真实路径的读库入口），首个请求通道异常、
        后续恢复。
        """
        mod, _ = _make_module_with_monitor()

        calls: list[int] = []

        class _FlakyDbRead(FakeDbRead):
            async def call(self, method: str, params: dict, timeout: float | None = None) -> Any:
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("transient bridge failure")
                return await super().call(method, params, timeout)

        import kernel_reads  # noqa: PLC0415

        kernel_reads.set_provider(
            "db-read", _FlakyDbRead(_make_llm_traces_db(tmp_path), _TOK_MAP).provider
        )

        # 首轮失败：降级空行，读面不崩
        first = _run(mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))
        assert first["success"] is True
        assert _decode_body(first)["rows"] == [], "故障轮降级空行"

        # TTL 过期后自愈：重新计算并恢复真实数据
        monkeypatch.setattr(mod, "_AGG_CACHE_TTL_SECS", 0.0)
        second = _run(mod.http_handle(path="/ext/monitoring/token-usage", method="GET", headers={"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}))
        # 重算发生 = 读面调用在首轮（1 次 state 查询失败即降级）之后增长；不断言
        # 精确次数（恢复轮 state + traces 各一次，耦合聚合内部查询面属实现细节）
        assert len(calls) > 1, "TTL 过期必须重新计算"
        assert len(_decode_body(second)["rows"]) == 3, "库恢复后数据自愈"


def _make_cross_period_traces_db(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """造跨时段 traces 夹具：今天 / 3 天前 / 30 天前各一轮（时钟稳健）。

    时间戳按真实 now（UTC）相对生成——固定历史日期会随钟表漂移翻转 days
    窗口判定；相对 now 的日期桶使 days 裁剪结果与运行日期无关。各桶
    total_tokens 取不同字面值（11/22/44）供结果集区分度断言。
    返回 (db_path, {"today"/"3d_ago"/"30d_ago": UTC 日期文本})。
    """
    now = datetime.datetime.now(datetime.UTC)
    buckets = {"today": 0, "3d_ago": 3, "30d_ago": 30}
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE traces (trace_id TEXT, pipeline_id TEXT, created_at TEXT,"
        " plugin_id TEXT, patch_data TEXT, tenant_id TEXT)"
    )
    dates: dict[str, str] = {}
    for label, days_ago in buckets.items():
        ts = (now - datetime.timedelta(days=days_ago)).isoformat()
        dates[label] = ts[:10]
        total = 11 * (2 ** list(buckets).index(label))
        usage = {
            "input_tokens": total - 1,
            "output_tokens": 1,
            "total_tokens": total,
            "cached_tokens": 0,
        }
        conn.execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?)",
            (f"tr-{label}", "pipe-1", ts, "core", json.dumps({"llm_usage": usage}), "tenant-a"),
        )
    conn.commit()
    conn.close()
    return db_path, dates


class TestByTimeParamWiring:
    """X15-F1（P-146）：by-time 端点时间参数接线 + 聚合缓存键参数维度。

    P-146 原状：days/since/until 只存在于聚合函数签名，分发器从不接线——
    恒全表聚合（traces 53.9MB 每 60s 轮询一次全表 GROUP BY 扫描）；且缓存键
    无参数维度，接线后不同参数会共享同一条目串数据。
    """

    _HEADERS = {"x-agentos-tenant": "tenant-a", "authorization": "tok-a"}

    def _by_time(self, mod: Any, query: dict[str, str] | None) -> Any:
        resp = _run(mod.http_handle(
            path="/ext/monitoring/token-usage/by-time",
            method="GET",
            query=query,
            headers=dict(self._HEADERS),
        ))
        assert resp["success"] is True
        assert resp["data"]["status"] == 200
        return _decode_body(resp)

    def test_route_wires_days_and_explicit_range(self, tmp_path: Path, monkeypatch) -> None:
        """days/since/until 从 query 接线进聚合：不同参数返回不同结果集。"""
        mod, _ = _make_module_with_monitor()
        db_path, dates = _make_cross_period_traces_db(tmp_path)
        _install_fake_db_read(db_path, _TOK_MAP)

        def _dates_of(query: dict[str, str] | None) -> set[str]:
            return {r["date"] for r in self._by_time(mod, query)["rows"]}

        all_dates = _dates_of(None)
        assert all_dates == set(dates.values()), "无参 = 全表聚合（三个时段桶）"
        week = _dates_of({"days": "7"})
        assert week == {dates["today"], dates["3d_ago"]}, "days=7 裁掉 30 天前桶"
        assert week != all_dates, "?days=7 与无参必须返回不同结果集（接线实证）"
        assert _dates_of({"days": "1"}) == {dates["today"]}, "days=1 只剩今天桶"
        # 显式日期区间（闭区间）优先于 days，单日区间只留该桶
        rows = self._by_time(mod, {"since": dates["3d_ago"], "until": dates["3d_ago"]})["rows"]
        assert {r["date"] for r in rows} == {dates["3d_ago"]}
        assert rows[0]["total_tokens"] == 22, "区间内桶取自建值（22 = 3 天前桶字面值）"
        # 上边界放行：days=90 覆盖全部相对时段（days>0 SQL 路径贯通）
        assert _dates_of({"days": "90"}) == all_dates

    def test_cache_key_distinguishes_time_params(self, tmp_path: Path, monkeypatch) -> None:
        """缓存键含参数维度：同参命中同条目，异参各持条目不串数据。"""
        mod, _ = _make_module_with_monitor()
        db_path, dates = _make_cross_period_traces_db(tmp_path)
        _install_fake_db_read(db_path, _TOK_MAP)
        calls: list[int] = []
        _patch_counting_aggregate(mod, monkeypatch, calls)

        week1 = self._by_time(mod, {"days": "7"})
        week2 = self._by_time(mod, {"days": "7"})
        assert len(calls) == 1, "TTL 内同参数二次请求必须命中同一条缓存"
        assert week1 == week2
        day1 = self._by_time(mod, {"days": "1"})
        assert len(calls) == 2, "不同参数不允许共享缓存条目（串数据）"
        assert {r["date"] for r in day1["rows"]} == {dates["today"]}
        assert {r["date"] for r in week1["rows"]} == {dates["today"], dates["3d_ago"]}

    @pytest.mark.parametrize("query", [
        {"days": "0"},
        {"days": "-3"},
        {"days": "91"},
        {"days": "abc"},
        {"days": "7.5"},
        {"since": "2026-13-01"},
        {"until": "not-a-date"},
    ])
    def test_invalid_time_params_get_protocol_400(
        self, tmp_path: Path, monkeypatch, query: dict[str, str]
    ) -> None:
        """非法时间参数 → 协议化 400 信封（success=True 的 HttpHandleResponse
        携 status=400 + 结构化错误体），不是 success=False 桥包 5xx。"""
        mod, _ = _make_module_with_monitor()
        db_path, _dates = _make_cross_period_traces_db(tmp_path)
        _install_fake_db_read(db_path, _TOK_MAP)
        resp = _run(mod.http_handle(
            path="/ext/monitoring/token-usage/by-time",
            method="GET",
            query=query,
            headers=dict(self._HEADERS),
        ))
        assert resp["success"] is True, "协议化 400 走 success=True 信封，非桥包失败"
        assert resp["data"]["status"] == 400
        body = _decode_body(resp)
        assert body["error"]["code"] == "400"
        assert isinstance(body["error"]["message"], str)
        assert body["error"]["message"]


class TestMissingPipelineIdProtocolError:
    """P-160 同刀：缺 pipeline_id 返协议化 400 信封（原 _error 桥包 502）。

    pipeline-state（动态实证项）与 traces（同文件相邻行同款）一起换
    protocol_error 契约：{success:True, data:{status:400, body:{error:{code,message}}}}。
    """

    @pytest.mark.parametrize("path", [
        "/ext/monitoring/pipeline-state",
        "/ext/monitoring/traces",
    ])
    def test_missing_pipeline_id_returns_protocol_400(self, path: str) -> None:
        mod, _ = _make_module_with_monitor()
        resp = _run(mod.http_handle(path=path, method="GET", query={}, headers={}))
        assert resp["success"] is True, "协议化 400 走 success=True 信封，非桥包失败"
        assert resp["data"]["status"] == 400
        body = _decode_body(resp)
        assert body["error"]["code"] == "400"
        assert "pipeline_id" in body["error"]["message"]


# ═══════════════════════════════════════════════════════════
# M3：search/sessions 域租户过滤（fail-closed，与 tool-calls 域同纪律）
# ═══════════════════════════════════════════════════════════


def _make_runs_for_search(tmp_path: Path) -> Path:
    """pipeline_state 两租户各一管道（run_status 簿记键），驱动可见集判定。

    runs 表退役（ADR 2026-09-18）：执行过 = 有 run_status 标量键。
    """
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT, value TEXT,"
        " value_kind TEXT DEFAULT 'str', tenant_id TEXT, updated_at TEXT,"
        " PRIMARY KEY (pipeline_id, field_key, tenant_id))"
    )
    conn.execute(
        "INSERT INTO pipeline_state VALUES ('p-tenant-a', 'run_status', 'completed', 'str', 'tenant-a', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO pipeline_state VALUES ('p-tenant-b', 'run_status', 'completed', 'str', 'tenant-b', '2026-01-02T00:00:00')"
    )
    conn.commit()
    conn.close()
    return db_path


def _make_sessions_for_search(tmp_path: Path) -> Path:
    """sessions 表两租户各一同名会话（真实标题源）+ state 运行键供可见集。"""
    db_path = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT, value TEXT,"
        " value_kind TEXT DEFAULT 'str', tenant_id TEXT, updated_at TEXT,"
        " PRIMARY KEY (pipeline_id, field_key, tenant_id))"
    )
    conn.execute(
        "CREATE TABLE sessions (thread_id TEXT PRIMARY KEY, title TEXT, tenant_id TEXT, "
        "last_active_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO pipeline_state VALUES ('p-tenant-a', 'run_status', 'completed', 'str', 'tenant-a', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO pipeline_state VALUES ('p-tenant-b', 'run_status', 'completed', 'str', 'tenant-b', '2026-01-02T00:00:00')"
    )
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
    """search 域租户过滤（db-read 契约面，C2 刀2）。

    收窄执法本体在内核 Rust 契约测试（read_faces/tests.rs）；本类钉插件侧
    装配契约：可见集/会话搜索走 db-read（face=pipeline_state/sessions）、
    身份经 authorization 原头穿线、缺身份 fail-closed 不发查询、契约失败
    降级空集不阻塞搜索主链路。
    """

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

    def test_tenant_pipeline_ids_scoped_to_tenant(self, tmp_path: Path) -> None:
        mod = self._load_search()
        fake = _install_fake_db_read(
            _make_runs_for_search(tmp_path), {"tok-a": "tenant-a", "tok-b": "tenant-b"}
        )
        assert _run(mod._tenant_pipeline_ids("tenant-a", "tok-a")) == {"p-tenant-a"}
        assert _run(mod._tenant_pipeline_ids("tenant-b", "tok-b")) == {"p-tenant-b"}
        # 本租户身份 + 他租户谓词值：内层谓词过度限制 → 空集（不越界）
        assert _run(mod._tenant_pipeline_ids("tenant-nobody", "tok-a")) == set()
        # 身份穿线：face 与 authorization 原头逐值可达 db-read 参数链
        assert fake.calls[0]["face"] == "pipeline_state"
        assert fake.calls[0]["_authorization"] == "tok-a"

    def test_tenant_pipeline_ids_missing_identity_fails_closed_without_query(self, tmp_path: Path) -> None:
        # fail-closed：tenant_id 缺失不发查询（db-read 一次都不触），返回 None
        mod = self._load_search()
        fake = _install_fake_db_read(_make_runs_for_search(tmp_path), {"tok-a": "tenant-a"})
        assert _run(mod._tenant_pipeline_ids("", "tok-a")) is None
        assert fake.calls == []

    def test_tenant_pipeline_ids_db_read_failure_degrades_none(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # 契约失败（信封 error / 通道异常）→ None（调用方空集降级，不崩 handler）
        mod = self._load_search()
        _install_fake_db_read(
            _make_runs_for_search(tmp_path),
            {"tok-a": "tenant-a"},
            fail=RuntimeError("db-read channel down"),
        )
        assert _run(mod._tenant_pipeline_ids("tenant-a", "tok-a")) is None

    def test_tenant_pipeline_ids_error_envelope_degrades_none(self, tmp_path: Path) -> None:
        # 内核 403 信封（无凭证/档位拒绝）→ DbReadQueryError → None 降级
        mod = self._load_search()
        _install_fake_db_read(_make_runs_for_search(tmp_path), token_tenants={})
        assert _run(mod._tenant_pipeline_ids("tenant-a", "anon-token")) is None

    def test_search_missing_tenant_is_empty_not_full(self, tmp_path: Path) -> None:
        # fail-closed：缺身份头返回空集，绝不退化全量搜索（跨租户消息正文泄露面）
        mod = self._load_search()
        fake = _install_fake_db_read(_make_runs_for_search(tmp_path), {"tok-a": "tenant-a"})
        result = _run(mod.search(q="秘密", type="all", limit=20, tenant_id=""))
        assert result["sessions"] == [] and result["messages"] == []
        assert fake.calls == []  # 身份缺失连可见集查询都不发起

    def test_search_filters_hits_by_tenant_pipelines(self, tmp_path: Path, monkeypatch) -> None:
        mod = self._load_search()
        _install_fake_db_read(
            _make_sessions_for_search(tmp_path), {"tok-a": "tenant-a", "tok-b": "tenant-b"}
        )
        mine = _run(
            mod.search(q="会话", type="session", limit=20, tenant_id="tenant-a", authorization="tok-a")
        )
        assert [s["id"] for s in mine["sessions"]] == ["thread-a"]
        theirs = _run(
            mod.search(q="会话", type="session", limit=20, tenant_id="tenant-b", authorization="tok-b")
        )
        assert [s["id"] for s in theirs["sessions"]] == ["thread-b"]

    def test_session_search_hits_sessions_table_scoped_to_tenant(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """会话标题搜索命中 sessions 表（真实标题源），命中 id=thread_id 且按租户隔离。"""
        mod = self._load_search()
        fake = _install_fake_db_read(
            _make_sessions_for_search(tmp_path), {"tok-a": "tenant-a", "tok-b": "tenant-b"}
        )
        mine = _run(
            mod.search(q="stress2-19", type="session", limit=20, tenant_id="tenant-a", authorization="tok-a")
        )
        assert [s["id"] for s in mine["sessions"]] == ["thread-a"]
        assert "stress2-19" in mine["sessions"][0]["title"]
        theirs = _run(
            mod.search(q="stress2-19", type="session", limit=20, tenant_id="tenant-b", authorization="tok-b")
        )
        assert [s["id"] for s in theirs["sessions"]] == ["thread-b"]
        # 两查询走 sessions 面，身份逐次穿线
        assert {c["face"] for c in fake.calls} == {"pipeline_state", "sessions"}
        assert {c["_authorization"] for c in fake.calls} == {"tok-a", "tok-b"}

    def test_search_session_db_read_failure_degrades_empty(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # 会话搜索契约失败 → 空列表（搜索面降级契约，不报 5xx）
        mod = self._load_search()
        _install_fake_db_read(
            _make_sessions_for_search(tmp_path),
            {"tok-a": "tenant-a"},
            fail=RuntimeError("db-read channel down"),
        )
        result = _run(
            mod.search(q="会话", type="session", limit=20, tenant_id="tenant-a", authorization="tok-a")
        )
        assert result["sessions"] == []

    def test_session_search_title_no_hit_returns_empty(self, tmp_path: Path, monkeypatch) -> None:
        mod = self._load_search()
        _install_fake_db_read(
            _make_sessions_for_search(tmp_path), {"tok-a": "tenant-a"}
        )
        none = _run(
            mod.search(q="zzz-nonexistent", type="session", limit=20, tenant_id="tenant-a", authorization="tok-a")
        )
        assert none["sessions"] == []


class TestDbReadBridge:
    """kernel_reads.db_read_query 装配契约（信封解包/失败上抛/未注入拒绝）。"""

    def test_unwrapped_envelope_returns_rows(self) -> None:
        import kernel_reads  # noqa: PLC0415

        async def provider(face: str, sql: str, params: Any = None, authorization: str = "") -> Any:
            assert face == "pipeline_state"
            assert authorization == "tok-1"
            return {"status": 200, "body": {"columns": ["tenant_id", "pipeline_id"], "rows": [["t", "p"]]}}

        kernel_reads.set_provider("db-read", provider)
        rows = _run(kernel_reads.db_read_query("pipeline_state", "SELECT 1", None, "tok-1"))
        assert rows == [["t", "p"]]

    def test_error_envelope_raises_with_status(self) -> None:
        import kernel_reads  # noqa: PLC0415

        async def provider(face: str, sql: str, params: Any = None, authorization: str = "") -> Any:
            return {"status": 403, "error": {"code": "DB_READ_ROLE_DENIED", "message": "档位拒绝"}}

        kernel_reads.set_provider("db-read", provider)
        with pytest.raises(kernel_reads.DbReadQueryError) as exc_info:
            _run(kernel_reads.db_read_query("pipeline_state", "SELECT 1"))
        assert exc_info.value.status == 403

    def test_provider_missing_raises(self) -> None:
        import kernel_reads  # noqa: PLC0415

        kernel_reads.reset_providers()
        with pytest.raises(kernel_reads.DbReadQueryError):
            _run(kernel_reads.db_read_query("pipeline_state", "SELECT 1"))

    def test_channel_exception_normalized(self) -> None:
        import kernel_reads  # noqa: PLC0415

        async def provider(face: str, sql: str, params: Any = None, authorization: str = "") -> Any:
            raise KeyError("capability not injected: db-read")

        kernel_reads.set_provider("db-read", provider)
        with pytest.raises(kernel_reads.DbReadQueryError):
            _run(kernel_reads.db_read_query("pipeline_state", "SELECT 1"))

    def test_provider_contract_error_reraised_as_is(self) -> None:
        # provider 已归一的契约错误原样透传（不二次包装，status 不丢）
        import kernel_reads  # noqa: PLC0415
        from agentos_plugin_sdk.db_read import DbReadQueryError  # noqa: PLC0415

        async def provider(face: str, sql: str, params: Any = None, authorization: str = "") -> Any:
            raise DbReadQueryError("内核 403", 403)

        kernel_reads.set_provider("db-read", provider)
        with pytest.raises(DbReadQueryError) as exc_info:
            _run(kernel_reads.db_read_query("pipeline_state", "SELECT 1"))
        assert exc_info.value.status == 403

    def test_on_load_injects_db_read_provider(self) -> None:
        # 装配：_on_load 注入 db-read provider（与其他内核读桥同缝），调用
        # 经 get_capability("db-read").call("query", params) 且身份逐值穿线
        import kernel_reads  # noqa: PLC0415

        kernel_reads.reset_providers()
        mod = _load_server()
        captured: dict[str, Any] = {}

        class _FakeHandle:
            async def call(self, method: str, params: dict[str, Any], timeout: Any = None) -> Any:
                captured["method"] = method
                captured["params"] = params
                return {"status": 200, "body": {"columns": [], "rows": [[1]]}}

        mod.plugin._capabilities["db-read"] = _FakeHandle()
        # _on_load 拉起的后台任务绑定当前事件循环，断言与卸载必须同循环执行
        # （跨循环 cancel 抛 RuntimeError: Event loop is closed）
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(mod._on_load({}))
            assert "db-read" in kernel_reads._PROVIDERS
            rows = loop.run_until_complete(
                kernel_reads.db_read_query("traces", "SELECT 1", {"k": "v"}, "tok-x")
            )
            assert rows == [[1]]
            assert captured["method"] == "query"
            assert captured["params"]["face"] == "traces"
            assert captured["params"]["params"] == {"k": "v"}
            assert captured["params"]["_authorization"] == "tok-x"
        finally:
            loop.run_until_complete(mod._on_unload({}))
            loop.close()


# ═══════════════════════════════════════════════════════════
# 插件组健康告警（卡5）：/ext/monitoring/plugin-health
# ═══════════════════════════════════════════════════════════


def _install_fake_metrics_query(
    series: list[dict[str, Any]] | None = None, fail: bool = False
) -> dict[str, Any]:
    """metrics-admin-query provider 替身（记录调用参数，返回给定 series）。"""
    import kernel_reads  # noqa: PLC0415

    captured: dict[str, Any] = {}

    async def provider(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        if fail:
            raise RuntimeError("metrics query down")
        return {"status": 200, "body": {"metrics": series or []}}

    kernel_reads.set_provider("metrics-admin-query", provider)
    return captured


_CRASH_LOOP_SERIES = [
    {
        "plugin_id": "task_submit",
        "name": "process.last_crash_ts",
        "samples": [
            {"ts": 1, "value": 1000.0},
            {"ts": 2, "value": 1010.0},
            {"ts": 3, "value": 1020.0},
            {"ts": 4, "value": 1030.0},
        ],
    }
]


class TestPluginHealthRoute:
    def test_alerts_returned_and_auth_forwarded(self) -> None:
        """事故形态（连续 3 次恢复失败）：两规则齐响；Authorization 原样透传读面。"""
        mod, _ = _make_module_with_monitor()
        captured = _install_fake_metrics_query(_CRASH_LOOP_SERIES)
        resp = _run(
            mod.http_handle(
                path="/ext/monitoring/plugin-health",
                method="GET",
                headers={"authorization": "Bearer tok-x"},
            )
        )
        assert resp["success"] is True
        payload = _decode_body(resp)
        assert {(a["rule"], a["level"]) for a in payload["alerts"]} == {
            ("group_start_failure", "medium"),
            ("respawn_failure", "high"),
        }
        # provider 闭包形参 authorization（混沌演练 2026-10-18：传
        # _authorization 会 TypeError 静默降级空数据——卡9 收口修）
        assert captured["authorization"] == "Bearer tok-x"
        assert captured["metric"] == "process.last_crash_ts"

    def test_no_crashes_empty_alerts(self) -> None:
        """常态（last_crash_ts 恒值重复样本）：零告警。"""
        mod, _ = _make_module_with_monitor()
        _install_fake_metrics_query(
            [
                {
                    "plugin_id": "task_submit",
                    "name": "process.last_crash_ts",
                    "samples": [{"ts": 1, "value": 1000.0}, {"ts": 2, "value": 1000.0}],
                }
            ]
        )
        resp = _run(mod.http_handle(path="/ext/monitoring/plugin-health", method="GET"))
        assert _decode_body(resp)["alerts"] == []

    def test_provider_failure_degrades_empty_not_500(self) -> None:
        """读面故障降级空告警（kernel_reads 契约），不 500。"""
        mod, _ = _make_module_with_monitor()
        _install_fake_metrics_query(fail=True)
        resp = _run(mod.http_handle(path="/ext/monitoring/plugin-health", method="GET"))
        assert resp["success"] is True
        assert _decode_body(resp)["alerts"] == []

    def test_config_threshold_override_via_injected_config(self) -> None:
        """config_files[plugin_health] 阈值覆盖默认——端到端证明阈值非硬编码。"""
        mod, _ = _make_module_with_monitor()

        class _FakePlugin:
            def get_config(self) -> dict[str, Any]:
                return {"plugin_health": {"group_start_failure_threshold": 2}}

        mod.plugin = _FakePlugin()
        _install_fake_metrics_query(
            [
                {
                    "plugin_id": "task_submit",
                    "name": "process.last_crash_ts",
                    "samples": [{"ts": 1, "value": 1000.0}, {"ts": 2, "value": 1010.0}],
                }
            ]
        )
        resp = _run(mod.http_handle(path="/ext/monitoring/plugin-health", method="GET"))
        # 1 次跳变 < 覆盖后的阈值 2 → 不响（默认阈值 1 会响，非硬编码证据）
        assert _decode_body(resp)["alerts"] == []

    def test_kernel_counters_flow_through_route(self) -> None:
        """卡9：内核计数器与崩溃样本同流评估——四条规则可经同一次读面调用齐响。"""
        mod, _ = _make_module_with_monitor()
        _install_fake_metrics_query(
            [
                *_CRASH_LOOP_SERIES,
                {
                    "plugin_id": "kernel",
                    "name": "mcp.sidecar_stdout_eof_total",
                    "samples": [{"ts": i, "value": 1.0} for i in range(50)],
                },
                {
                    "plugin_id": "kernel",
                    "name": "mcp.tools_list_observe_fail_total",
                    "samples": [{"ts": 1, "value": 1.0}],
                },
            ]
        )
        resp = _run(mod.http_handle(path="/ext/monitoring/plugin-health", method="GET"))
        payload = _decode_body(resp)
        rules = {(a["rule"], a["level"]) for a in payload["alerts"]}
        assert rules == {
            ("group_start_failure", "medium"),
            ("respawn_failure", "high"),
            ("sidecar_eof_rate", "high"),
            ("tools_list_invalid", "high"),
        }
        eof_alert = next(a for a in payload["alerts"] if a["rule"] == "sidecar_eof_rate")
        assert eof_alert["plugin_id"] == "kernel"
        assert eof_alert["count"] == 50

    def test_background_loop_survives_read_failure_and_cancels(self) -> None:
        """卡9 后台轮询：单轮读面故障不退出循环；取消干净不悬挂。"""
        mod, _ = _make_module_with_monitor()
        _install_fake_metrics_query(fail=True)

        async def scenario() -> None:
            task = asyncio.create_task(mod._plugin_health_loop())
            await asyncio.sleep(0.05)  # 让至少一轮跑完（读面失败 → 降级续跑）
            assert not task.done(), "单轮失败不得退出轮询循环"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        _run(scenario())
