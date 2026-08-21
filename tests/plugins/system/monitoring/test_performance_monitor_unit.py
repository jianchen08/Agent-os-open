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
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MON_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "monitoring"
if str(_MON_DIR) not in sys.path:
    sys.path.insert(0, str(_MON_DIR))

import performance_monitor as pm  # noqa: E402


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
        self._craft(mon)
        received: list[pm.PerformanceAlert] = []

        async def cb(alert: pm.PerformanceAlert) -> None:
            received.append(alert)

        mon._alert_callback = cb
        asyncio.run(mon.detect_bottlenecks())
        assert received, "高指标应触发至少一条告警"

    def test_callback_exception_swallowed(self, mon: pm.PerformanceMonitor, caplog: pytest.LogCaptureFixture) -> None:
        self._craft(mon)

        async def bad_cb(alert: pm.PerformanceAlert) -> None:
            raise RuntimeError("callback boom")

        mon._alert_callback = bad_cb
        asyncio.run(mon.detect_bottlenecks())  # 不得抛出
        assert any("告警回调执行失败" in r.message for r in caplog.records)


class TestResponseTimeContext:
    def test_records_elapsed(self, mon: pm.PerformanceMonitor) -> None:
        async def scenario() -> None:
            async with mon.measure_response_time():
                await asyncio.sleep(0.01)

        asyncio.run(scenario())
        assert mon._response_times and mon._response_times[-1] >= 0

    def test_trim_to_1000(self, mon: pm.PerformanceMonitor) -> None:
        mon._response_times = [float(i) for i in range(1005)]
        ctx = pm.ResponseTimeContext(mon)
        ctx.start_time = 0.0

        async def scenario() -> None:
            await ctx.__aenter__()
            await ctx.__aexit__(None, None, None)

        asyncio.run(scenario())
        assert len(mon._response_times) == 1000


class TestStartStop:
    def test_start_monitoring_then_stop(self, mon: pm.PerformanceMonitor) -> None:
        async def scenario() -> None:
            await mon.start_monitoring(interval=5)
            assert mon._monitor_task is not None
            assert mon._response_times == []
            await asyncio.sleep(0.15)  # 允许监控循环跑一轮
            await mon.stop_monitoring()
            assert mon._monitor_task is None

        asyncio.run(scenario())
