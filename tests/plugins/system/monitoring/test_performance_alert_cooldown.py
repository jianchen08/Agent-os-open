# @feature: FP-0.2.二 内部模块manifest | @ci: python-coverage
"""LOW 级性能告警冷却窗（P0-2 降噪，方案 20261010）。

事故面：performance_monitor 5s 采样 × `cache_hit_rate < 0.5` 恒真 → 同一条
LOW 告警 5 秒一刷刷满日志（装机单日数千条）。修复=同消息 300s 冷却窗；
medium/high 真异常不冷却。时间经 time.monotonic 注入（假时钟断言时序不变量）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_MON_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "monitoring"


def _load_pm():
    """唯一模块名装载（同目录既有测试惯例，防 fake 注入守卫互踩）。"""
    mod_name = "performance_monitor_cooldown_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _MON_DIR / "performance_monitor.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


pm = _load_pm()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    """假时钟 + 告警回调探针。"""
    cb = AsyncMock()
    monitor = pm.PerformanceMonitor(alert_callback=cb)
    clock = {"t": 1000.0}
    monkeypatch.setattr(pm.time, "monotonic", lambda: clock["t"])
    return monitor, cb, clock


class TestLowAlertCooldown:
    def test_same_message_suppressed_within_cooldown(self, harness) -> None:
        monitor, cb, clock = harness
        msg = "工具缓存命中率过低: 0.0%"
        asyncio.run(monitor._trigger_alert("low", msg))
        for _ in range(3):
            clock["t"] += 5  # 采样间隔 5s
            asyncio.run(monitor._trigger_alert("low", msg))
        assert cb.await_count == 1, "冷却窗（300s）内同消息只发一次"

    def test_cooldown_expires_and_realerts(self, harness) -> None:
        monitor, cb, clock = harness
        msg = "工具缓存命中率过低: 0.0%"
        asyncio.run(monitor._trigger_alert("low", msg))
        clock["t"] += 301
        asyncio.run(monitor._trigger_alert("low", msg))
        assert cb.await_count == 2, "冷却窗过期后重新告警"

    def test_different_message_not_suppressed(self, harness) -> None:
        monitor, cb, _ = harness
        asyncio.run(monitor._trigger_alert("low", "A 告警"))
        asyncio.run(monitor._trigger_alert("low", "B 告警"))
        assert cb.await_count == 2, "不同消息互不影响"

    def test_medium_high_never_suppressed(self, harness) -> None:
        monitor, cb, clock = harness
        asyncio.run(monitor._trigger_alert("medium", "M 告警"))
        asyncio.run(monitor._trigger_alert("medium", "M 告警"))
        asyncio.run(monitor._trigger_alert("high", "H 告警"))
        asyncio.run(monitor._trigger_alert("high", "H 告警"))
        assert cb.await_count == 4, "medium/high 真异常不冷却"


class TestCacheHitRateAlertGuard:
    """缓存命中率告警须有观测样本（装机 11-05 F2 止血）。

    record_tool_execution 全仓零调用 → cache_hits/cache_misses 恒 0 →
    cache_hit_rate 恒 0 → `cache_hit_rate < 0.5` 恒真 → 每 5 分钟一条
    （×145/天）。止血=total_cache>0 前置：零样本不判命中率。
    """

    @staticmethod
    async def _zero_system_metrics() -> pm.SystemMetrics:
        return pm.SystemMetrics(
            cpu_usage=0, memory_usage=0, disk_usage=0, network_sent=0, network_recv=0
        )

    def _run_detect(self, monitor: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """detect_bottlenecks 直跑（psutil 系统探针为外部依赖，打零值桩）。"""
        monkeypatch.setattr(monitor, "get_system_metrics", self._zero_system_metrics)
        asyncio.run(monitor.detect_bottlenecks())

    def test_zero_cache_samples_no_alert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """零观测样本（埋点未接线形态）→ 不告警（恒真噪音根因）。"""
        cb = AsyncMock()
        monitor = pm.PerformanceMonitor(alert_callback=cb)
        self._run_detect(monitor, monkeypatch)
        assert cb.await_count == 0, "零样本不得产生缓存命中率告警"

    def test_low_hit_rate_with_samples_alerts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """有样本且命中率 25%（<0.5）→ 照常告警（真低命中不漏报）。"""
        cb = AsyncMock()
        monitor = pm.PerformanceMonitor(alert_callback=cb)
        monitor.record_tool_execution(0.1, cache_hit=False)
        monitor.record_tool_execution(0.1, cache_hit=False)
        monitor.record_tool_execution(0.1, cache_hit=False)
        monitor.record_tool_execution(0.1, cache_hit=True)
        self._run_detect(monitor, monkeypatch)
        assert cb.await_count == 1
        assert "工具缓存命中率过低" in cb.await_args.args[0].message

    def test_high_hit_rate_with_samples_no_alert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """有样本且命中率 100%（≥0.5）→ 不告警。"""
        cb = AsyncMock()
        monitor = pm.PerformanceMonitor(alert_callback=cb)
        for _ in range(4):
            monitor.record_tool_execution(0.1, cache_hit=True)
        self._run_detect(monitor, monkeypatch)
        assert cb.await_count == 0
