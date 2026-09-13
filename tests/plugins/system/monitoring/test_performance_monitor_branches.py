# @feature: FP-0.2.二 monitoring performance 缺口分支补测 | @ci: python-coverage
# @ci: python-coverage
"""PerformanceMonitor 缺口分支补测——监控循环退出族/网络速率零间隔/健康状态分级。

覆盖面（批五覆盖率冲刺，接 test_performance_monitor_unit.py 既有单测）：
- _monitor_loop 三退出路径：取消（CancelledError → break）、指标异常
  （记日志继续）、wait 超时（continue 回环）
- get_system_metrics 的零时间间隔防除零（network 速率归零）
- record_llm_response 的配对递减与下限 0
- get_current_metrics 聚合五维；get_current_stats 三环境分支
  （运行中 loop → 调度异步、无 loop → RuntimeError 兜底、非运行 loop →
  run_until_complete）与 _get_stats_async 部分降级
- get_health_status 四档分级（healthy/warning×2/critical×2）× 三环境分支

时间与 psutil 属外部系统探针，按测试纪律用 monkeypatch 替身；统计纯逻辑
全真实。循环测试用真实事件循环 + 短窗口，不冻结全局时钟（asyncio 定时器
依赖真实单调钟）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_MON_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "monitoring"


def _load_pm():
    """唯一模块名装载 performance_monitor（不裸名 import，防同车道 fake 注入守卫冲突）。"""
    mod_name = "performance_monitor_branch_test"
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


def _patch_psutil(monkeypatch: Any, cpu: float, mem: float) -> None:
    """把系统探针替换为定值（psutil 是外部系统依赖）。"""
    monkeypatch.setattr(pm.psutil, "cpu_percent", lambda interval=0.1: cpu)
    monkeypatch.setattr(pm.psutil, "virtual_memory", lambda: types.SimpleNamespace(percent=mem))


class _NonRunningLoop:
    """伪装非运行 loop：run_until_complete 用临时真实 loop 执行协程后关闭。"""

    def is_running(self) -> bool:
        return False

    @staticmethod
    def get_event_loop() -> "_NonRunningLoop":
        return _NonRunningLoop()

    def run_until_complete(self, coro: Any) -> Any:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


# ═══════════════════════════════════════════════════════════
# _monitor_loop 退出路径
# ═══════════════════════════════════════════════════════════


class TestMonitorLoop:
    async def test_cancellation_breaks_loop(self, mon: pm.PerformanceMonitor) -> None:
        """循环内取消 → CancelledError 捕获后 break（任务以取消终态收场）。"""
        await mon.start()
        await asyncio.sleep(0.05)
        task = mon._monitor_task
        assert task is not None
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task
        assert task.cancelled() or task.done()

    async def test_cancellation_inside_metric_collection_breaks_loop(
        self, mon: pm.PerformanceMonitor, monkeypatch: Any
    ) -> None:
        """取消落在指标采集的挂起点内 → 被循环的 CancelledError 分支捕获退出。

        循环体唯一的常态挂起点是尾部的停机 wait（第二个 try 只捕
        TimeoutError，取消从那里逃逸属现状契约）；要让第一个 try 的
        CancelledError 分支可达，须让采集自身真实挂起。
        """
        entered = asyncio.Event()

        async def _suspendable_metrics() -> pm.SystemMetrics:
            entered.set()
            await asyncio.sleep(10)
            raise AssertionError("取消后不应执行到这里")

        monkeypatch.setattr(mon, "get_system_metrics", _suspendable_metrics)
        await mon.start()
        task = mon._monitor_task
        assert task is not None
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task
        # 取消被分支捕获后循环 break 退出：任务正常完成（result=None）
        assert task.done()
        assert task.result() is None

    async def test_metric_error_logged_and_loop_drains(
        self, mon: pm.PerformanceMonitor, monkeypatch: Any
    ) -> None:
        """单轮指标采集异常 → 记日志继续（不终止监控循环）。

        停机等待用假 asyncio.wait_for 秒超时（真实实现要等 5s），让循环
        快速进入下一轮。
        """

        class _FakeAsyncio:
            CancelledError = asyncio.CancelledError

            @staticmethod
            async def wait_for(_fut: Any, timeout: float | None = None) -> None:
                await asyncio.sleep(0)
                raise TimeoutError

            @staticmethod
            def create_task(coro: Any) -> Any:
                return asyncio.create_task(coro)

        monkeypatch.setattr(pm, "asyncio", _FakeAsyncio)
        calls = {"n": 0}

        async def _flaky() -> pm.SystemMetrics:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("首轮采集失败")
            return await pm.PerformanceMonitor.get_system_metrics(mon)

        monkeypatch.setattr(mon, "get_system_metrics", _flaky)
        await mon.start()
        await asyncio.sleep(0.5)
        await mon.stop()
        assert calls["n"] >= 2, "异常后循环应继续下一轮采集"

    async def test_wait_timeout_continues_loop(
        self, mon: pm.PerformanceMonitor, monkeypatch: Any
    ) -> None:
        """停机等待超时 → continue 回环（用假 asyncio.wait_for 秒超时，不实等 5s）。"""

        class _FakeAsyncio:
            CancelledError = asyncio.CancelledError

            @staticmethod
            async def wait_for(_fut: Any, timeout: float | None = None) -> None:
                await asyncio.sleep(0)
                raise TimeoutError

            @staticmethod
            def create_task(coro: Any) -> Any:
                return asyncio.create_task(coro)

        monkeypatch.setattr(pm, "asyncio", _FakeAsyncio)

        async def _set_shutdown() -> pm.SystemMetrics:
            mon._shutdown_event.set()
            return await pm.PerformanceMonitor.get_system_metrics(mon)

        monkeypatch.setattr(mon, "get_system_metrics", _set_shutdown)
        await mon.start()
        task = mon._monitor_task
        assert task is not None
        await asyncio.wait_for(task, timeout=5.0)
        assert not task.cancelled()


# ═══════════════════════════════════════════════════════════
# 网络速率零间隔防除零
# ═══════════════════════════════════════════════════════════


class TestNetworkRateZeroInterval:
    async def test_zero_time_diff_yields_zero_rates(
        self, mon: pm.PerformanceMonitor, monkeypatch: Any
    ) -> None:
        """采样间隔为 0（时钟未前进）→ 速率归零而非除零崩溃。"""

        class _FrozenTime:
            @staticmethod
            def time() -> float:
                return 1000.0

        monkeypatch.setattr(pm, "time", _FrozenTime)
        metrics = await mon.get_system_metrics()
        assert metrics.network_sent == 0
        assert metrics.network_recv == 0

    async def test_positive_time_diff_yields_nonnegative_rates(
        self, mon: pm.PerformanceMonitor
    ) -> None:
        """正常采样间隔 → 速率为非负有限值（性质断言）。"""
        metrics = await mon.get_system_metrics()
        assert metrics.network_sent >= 0
        assert metrics.network_recv >= 0


# ═══════════════════════════════════════════════════════════
# record_llm_response 配对递减
# ═══════════════════════════════════════════════════════════


class TestRecordLlmResponse:
    async def test_decrement_paired_with_start(self, mon: pm.PerformanceMonitor) -> None:
        """start +1 → response -1 回到 0（经公共指标面观察）。"""
        mon.record_llm_request_start()
        mon.record_llm_request_start()
        mon.record_llm_response()
        metrics = await mon.get_llm_metrics()
        assert metrics.active_requests == 1

    async def test_floor_at_zero(self, mon: pm.PerformanceMonitor) -> None:
        """无配对 start 时递减不下穿 0。"""
        mon.record_llm_response()
        metrics = await mon.get_llm_metrics()
        assert metrics.active_requests == 0


# ═══════════════════════════════════════════════════════════
# get_current_metrics / get_current_stats 环境分支
# ═══════════════════════════════════════════════════════════


class TestCurrentMetrics:
    async def test_get_current_metrics_aggregates_five_dimensions(
        self, mon: pm.PerformanceMonitor
    ) -> None:
        snapshot = await mon.get_current_metrics()
        assert set(snapshot) == {"system", "database", "llm", "tool", "task"}
        assert "cpu_usage" in snapshot["system"]
        assert "active_connections" in snapshot["database"]

    async def test_get_current_stats_running_loop_schedules_async(
        self, mon: pm.PerformanceMonitor, monkeypatch: Any
    ) -> None:
        """运行中 loop → 调度异步任务并立即返回空统计（同步兼容语义）。

        get_current_stats 内部局部 `import asyncio`（真实模块），故 patch
        打在 asyncio 模块本体上（monkeypatch 测试后还原）。
        """
        closed = {"n": 0}
        real_get_event_loop = asyncio.get_event_loop

        def _fake_ensure_future(coro: Any) -> None:
            coro.close()  # 测试不求值统计协程，关闭防悬挂任务告警
            closed["n"] += 1

        monkeypatch.setattr(asyncio, "get_event_loop", real_get_event_loop)
        monkeypatch.setattr(asyncio, "ensure_future", _fake_ensure_future)
        stats = mon.get_current_stats()
        assert stats == {}
        assert closed["n"] == 1

    async def test_get_current_stats_no_loop_returns_empty(
        self, mon: pm.PerformanceMonitor, monkeypatch: Any
    ) -> None:
        """无可用事件循环（RuntimeError）→ 空统计兜底。"""

        def _raise() -> Any:
            raise RuntimeError("no running event loop")

        monkeypatch.setattr(asyncio, "get_event_loop", _raise)
        assert mon.get_current_stats() == {}

    def test_get_current_stats_nonrunning_loop_runs_coroutine(
        self, mon: pm.PerformanceMonitor, monkeypatch: Any
    ) -> None:
        """非运行 loop → run_until_complete 真正执行统计协程。

        同步测试（无运行 loop）——运行中 loop 的线程里 run_until_complete
        必然 RuntimeError，该分支不可达。
        """
        monkeypatch.setattr(asyncio, "get_event_loop", _NonRunningLoop.get_event_loop)
        stats = mon.get_current_stats()
        assert "cpu" in stats
        assert "memory" in stats

    def test_get_stats_partial_on_system_failure(
        self, mon: pm.PerformanceMonitor, monkeypatch: Any
    ) -> None:
        """system 维度采集失败 → 返回部分统计（其余维度缺省不崩）。"""

        async def _boom() -> pm.SystemMetrics:
            raise RuntimeError("system 探针失败")

        monkeypatch.setattr(mon, "get_system_metrics", _boom)
        monkeypatch.setattr(asyncio, "get_event_loop", _NonRunningLoop.get_event_loop)
        stats = mon.get_current_stats()
        assert "cpu" not in stats
        assert "memory" not in stats


# ═══════════════════════════════════════════════════════════
# get_health_status 四档分级 × 环境分支
# ═══════════════════════════════════════════════════════════


class TestGetHealthStatus:
    @pytest.mark.parametrize(
        "cpu, mem, expected_status, issue_fragments",
        [
            (50.0, 50.0, "healthy", []),
            (65.0, 50.0, "warning", ["CPU使用率偏高"]),
            (50.0, 65.0, "warning", ["内存使用率偏高"]),
            (85.0, 85.0, "critical", ["CPU使用率过高", "内存使用率过高"]),
            (85.0, 50.0, "critical", ["CPU使用率过高"]),
        ],
    )
    async def test_status_grading(
        self,
        mon: pm.PerformanceMonitor,
        monkeypatch: Any,
        cpu: float,
        mem: float,
        expected_status: str,
        issue_fragments: list[str],
    ) -> None:
        _patch_psutil(monkeypatch, cpu, mem)
        report = mon.get_health_status()
        assert report["status"] == expected_status
        assert len(report["issues"]) == len(issue_fragments)
        for fragment in issue_fragments:
            assert any(fragment in issue for issue in report["issues"])
        assert report["metrics"]["cpu"] == cpu
        assert report["metrics"]["memory"] == mem

    async def test_no_event_loop_falls_back_to_direct_probe(
        self, mon: pm.PerformanceMonitor, monkeypatch: Any
    ) -> None:
        """get_event_loop 抛 RuntimeError → 直接 psutil 探针兜底。"""

        class _NoLoopAsyncio:
            @staticmethod
            def get_event_loop() -> Any:
                raise RuntimeError("no running event loop")

        monkeypatch.setattr(pm, "asyncio", _NoLoopAsyncio)
        _patch_psutil(monkeypatch, 90.0, 40.0)
        report = mon.get_health_status()
        assert report["status"] == "critical"
        assert report["issues"] == ["CPU使用率过高: 90.0%"]

    def test_nonrunning_loop_uses_health_metrics_coroutine(
        self, mon: pm.PerformanceMonitor, monkeypatch: Any
    ) -> None:
        """非运行 loop → 经 _get_health_metrics 协程取指标（同步测试使分支可达）。"""
        monkeypatch.setattr(asyncio, "get_event_loop", _NonRunningLoop.get_event_loop)
        _patch_psutil(monkeypatch, 70.0, 70.0)
        report = mon.get_health_status()
        assert report["status"] == "warning"
        assert report["metrics"]["cpu"] == 70.0
