# @feature: FP-0.2.二 内部模块manifest | @ci: python-coverage
"""PerformanceMonitor 纯逻辑单测——mypy 收紧批配套 + 覆盖缺口补齐。

意图（WHY）：
- 2026-08-21 治理批次：performance_monitor 计数器 int/float 类型收敛
  （dict[str, float] + pydantic int 字段显式 int()）后，这些行进入
  diff-coverage 度量面，需进程内测试锁定行为：
- record_*/update_*/get_*_metrics 的计数与派生指标正确性；
- 告警回调链（_trigger_alert 成功/异常两分支）；
- 指标历史环形裁剪与未知类型空列表；
- ResponseTimeContext 计时入账（无 start_monitoring 前提下自建容器）。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MON_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "monitoring"


def _load_pm():
    """唯一模块名装载 performance_monitor。

    不裸名 `import performance_monitor`：那会在收集期把真模块灌进 sys.modules，
    破坏同车道 test_monitoring.py 的 fake 注入守卫
    （`if "performance_monitor" not in sys.modules`）——与本目录其他测试的
    _load_* 惯例一致。
    """
    import importlib.util

    mod_name = "performance_monitor_unit_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _MON_DIR / "performance_monitor.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


pm = _load_pm()


@pytest.fixture
def mon() -> pm.PerformanceMonitor:
    return pm.PerformanceMonitor()


class TestDatabaseMetrics:
    def test_record_and_update(self, mon: pm.PerformanceMonitor) -> None:
        mon.record_database_connection(0.5)
        mon.record_query_execution(1.25)
        mon.update_database_connections(3, 10)
        m = asyncio.run(mon.get_database_metrics())
        assert m.active_connections == 3
        assert m.connection_pool_size == 10
        assert m.connection_wait_time == 0.5
        assert m.query_execution_time == 1.25


class TestLLMMetrics:
    def test_start_request_pairing(self, mon: pm.PerformanceMonitor) -> None:
        mon.record_llm_request_start()
        mon.record_llm_request_start()
        assert mon._llm_stats["active_requests"] == 2
        mon.record_llm_request(1.5)
        assert mon._llm_stats["active_requests"] == 1
        mon.record_llm_request(0.5, error=True)
        assert mon._llm_stats["active_requests"] == 0

    def test_end_without_start_floor_zero(self, mon: pm.PerformanceMonitor) -> None:
        mon.record_llm_request(1.0)  # 未 start 直接 end：下限 0
        assert mon._llm_stats["active_requests"] == 0

    def test_get_llm_metrics(self, mon: pm.PerformanceMonitor) -> None:
        mon.record_llm_request_start()
        mon.record_llm_request(2.0)
        m = asyncio.run(mon.get_llm_metrics())
        assert m.active_requests == 0
        assert m.error_rate == 0.0


class TestToolMetrics:
    def test_hits_misses_errors(self, mon: pm.PerformanceMonitor) -> None:
        mon.record_tool_execution(1.0, cache_hit=True)
        mon.record_tool_execution(3.0, cache_hit=False, error=True)
        m = asyncio.run(mon.get_tool_metrics())
        assert m.execution_count == 2
        assert m.error_count == 1
        assert m.cache_hit_rate == 0.5
        assert m.average_execution_time == 2.0


class TestTaskMetrics:
    def test_update_and_average(self, mon: pm.PerformanceMonitor) -> None:
        mon.update_task_status(2, 1, 3, task_time=9.0)
        m = asyncio.run(mon.get_task_metrics())
        assert m.pending_tasks == 2
        assert m.running_tasks == 1
        assert m.completed_tasks == 3
        assert m.average_task_time == 3.0

    def test_zero_completed_no_division(self, mon: pm.PerformanceMonitor) -> None:
        m = asyncio.run(mon.get_task_metrics())
        assert m.average_task_time == 0


class TestHistory:
    def test_unknown_type_empty(self, mon: pm.PerformanceMonitor) -> None:
        assert mon.get_metrics_history("nope") == []

    def test_record_and_trim(self, mon: pm.PerformanceMonitor) -> None:
        # _record_metrics 是各监控类型唯一的写面（公开 record_* 只覆盖计数器），
        # 容量阈值亦无公开配置口——以最小注入面锁定环形裁剪行为。
        mon._max_history_size = 2
        for i in range(4):
            metrics = pm.SystemMetrics(cpu_usage=i, memory_usage=0, disk_usage=0, network_sent=0, network_recv=0)
            mon._record_metrics("system", metrics)
        rows = mon.get_metrics_history("system", limit=10)
        assert len(rows) == 2
        assert all("timestamp" in r and "metrics" in r for r in rows)


class TestAlerts:
    def _craft(self, mon: pm.PerformanceMonitor) -> None:
        async def _sys() -> pm.SystemMetrics:
            return pm.SystemMetrics(cpu_usage=99, memory_usage=99, disk_usage=99, network_sent=0, network_recv=0)

        async def _db() -> pm.DatabaseMetrics:
            return pm.DatabaseMetrics(
                active_connections=9, connection_pool_size=10, connection_wait_time=0, query_execution_time=2.0
            )

        async def _llm() -> pm.LLMMetrics:
            return pm.LLMMetrics(active_requests=20, request_rate=0, average_response_time=9.0, error_rate=0.5)

        async def _tool() -> pm.ToolMetrics:
            return pm.ToolMetrics(execution_count=1, average_execution_time=9.0, cache_hit_rate=0.1, error_count=0)

        async def _task() -> pm.TaskMetrics:
            return pm.TaskMetrics(pending_tasks=99, running_tasks=0, completed_tasks=0, average_task_time=99.0)

        mon.get_system_metrics = _sys  # type: ignore[method-assign]
        mon.get_database_metrics = _db  # type: ignore[method-assign]
        mon.get_llm_metrics = _llm  # type: ignore[method-assign]
        mon.get_tool_metrics = _tool  # type: ignore[method-assign]
        mon.get_task_metrics = _task  # type: ignore[method-assign]

    def test_thresholds_fire_callback(self, mon: pm.PerformanceMonitor) -> None:
        received: list[pm.PerformanceAlert] = []

        async def cb(alert: pm.PerformanceAlert) -> None:
            received.append(alert)

        # 告警回调走公开构造参数注入
        target = pm.PerformanceMonitor(alert_callback=cb)
        self._craft(target)
        asyncio.run(target.detect_bottlenecks())
        assert received, "高指标应触发至少一条告警"

    def test_callback_exception_swallowed(self, mon: pm.PerformanceMonitor, caplog: pytest.LogCaptureFixture) -> None:
        async def bad_cb(alert: pm.PerformanceAlert) -> None:
            raise RuntimeError("callback boom")

        target = pm.PerformanceMonitor(alert_callback=bad_cb)
        self._craft(target)
        asyncio.run(target.detect_bottlenecks())  # 不得抛出
        assert any("告警回调执行失败" in r.message for r in caplog.records)


class TestResponseTimeContext:
    @pytest.mark.timing
    def test_records_elapsed(self, mon: pm.PerformanceMonitor) -> None:
        """计时上下文经公开统计面入账：恰记 1 条且耗时非负。"""

        async def scenario() -> None:
            async with mon.measure_response_time():
                await asyncio.sleep(0.01)

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(scenario())
            stats = mon.get_current_stats()
        finally:
            asyncio.set_event_loop(None)
            loop.close()
        assert stats["response_time"]["count"] == 1
        assert stats["response_time"]["min"] >= 0

    def test_trim_to_1000(self, mon: pm.PerformanceMonitor) -> None:
        """环形裁剪公开面：>1000 次计时后统计 count 封顶 1000，且值域非负。"""

        async def scenario() -> None:
            for _ in range(1005):
                async with mon.measure_response_time():
                    pass

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(scenario())
            stats = mon.get_current_stats()
        finally:
            asyncio.set_event_loop(None)
            loop.close()
        assert stats["response_time"]["count"] == 1000
        assert stats["response_time"]["min"] >= 0


class TestStartStop:
    @pytest.mark.timing
    def test_start_monitoring_then_stop(self) -> None:
        """监控循环生命周期（公开面）：运行期告警链路真实评估触发，停止后归于安静。"""
        alerts: list[pm.PerformanceAlert] = []
        mon = pm.PerformanceMonitor(alert_callback=alerts.append)
        # 公开 record 通道灌注高耗时/高错误率 → 循环一旦评估必触发告警
        mon.record_llm_request_start()
        for _ in range(10):
            mon.record_llm_request(response_time=9.0, error=True)

        async def scenario() -> None:
            await mon.start_monitoring(interval=5)
            deadline = time.monotonic() + 5
            while not alerts and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert alerts, "监控循环运行期应真实评估并触发告警"
            await mon.stop_monitoring()
            stopped_at = len(alerts)
            await asyncio.sleep(0.2)  # 宽限窗口：证明停止后不再有新告警（有界）
            assert len(alerts) == stopped_at

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(scenario())
        finally:
            asyncio.set_event_loop(None)
            loop.close()
