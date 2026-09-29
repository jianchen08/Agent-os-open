# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""triggers_ext 自动父通知熔断（自激防护）单元测试。

缺陷实证（2026-09-29/30 夜真机）：task=9677f0f1c58a 的失败通知以精确 18 秒
周期被重复注入 23 次（parent=bf5be46414ce）——子任务失败 → 自动父通知 →
父层自动重派 → 子任务再失败（如 LLM 429 秒败）→ 再通知的闭环无熔断。

契约：同键 (parent_pipeline_id, task_id, event) 滑动窗（10 分钟）内
第 1 次正常注入、第 2 次注入附重复计数提示、第 3 次起熔断不再注入
（WARNING 日志含熔断原因与计数），窗滚过后重新计数；键 LRU 上限 500。
时序断言用注入 fake clock 显式推进，禁止 sleep。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/tools/triggers_ext/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from triggers.manager import TriggerManager  # noqa: E402


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeClock:
    """可推进单调钟：时序不变量经显式推进断言，非 sleep。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class BreakerHarness:
    """同键连发驱动器：fire N 次同键域事件，收集父管道收到的注入。"""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.clock = clock or FakeClock()
        self.received: list[tuple[str, str, str]] = []
        self.mgr = TriggerManager(clock=self.clock)

        def fake_injector(pipeline_id: str, message: str, user_id: str) -> str:
            self.received.append((pipeline_id, message, user_id))
            return "ok"

        self.mgr.set_injector(fake_injector)

    def fire(self, times: int, event: str = "task_failed", **overrides: Any) -> None:
        for _ in range(times):
            data = {
                "pipeline_id": "child_pipe",
                "task_id": "t_loop",
                "parent_pipeline_id": "parent_pipe",
                "user_id": "u_admin",
                "title": "写周报",
                "error": "LLM 429 rate limited",
            }
            data.update(overrides)
            _run(self.mgr.handle_domain_event(event, data))


class TestAutoNotifyBreaker:
    """失败通知熔断：首报正常 / 连发熔断 / 窗口滚过恢复。"""

    def test_first_notification_injected_without_breaker_hint(self) -> None:
        """首报正常注入，无熔断提示。"""
        h = BreakerHarness()
        h.fire(1)
        assert len(h.received) == 1
        assert "[系统通知]" in h.received[0][1]
        assert "熔断" not in h.received[0][1]

    def test_second_notification_injected_with_repeat_count_hint(self) -> None:
        """窗内第 2 次仍注入，但附重复计数提示；首报无提示。"""
        h = BreakerHarness()
        h.fire(2)
        assert len(h.received) == 2
        assert "第 2 次" in h.received[1][1], "第 2 次注入应带重复计数提示"
        assert "第 2 次" not in h.received[0][1], "首报不带提示"

    def test_third_onward_breaks_with_warning_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """窗内第 3 次起熔断：不再注入，每次记 WARNING（含原因与计数）。"""
        h = BreakerHarness()
        with caplog.at_level(logging.WARNING):
            h.fire(5)
        assert len(h.received) == 2, "窗内第 3 次起不再注入"
        breakers = [r for r in caplog.records if "熔断" in r.getMessage()]
        assert len(breakers) == 3, "第 3/4/5 次各记一条熔断日志"
        assert "第 3 次" in breakers[0].getMessage(), "熔断日志含触发时的计数"

    def test_window_rollover_restarts_count(self) -> None:
        """窗滚过（>10 分钟）后重新计数：再注入为首报，无重复提示。"""
        h = BreakerHarness()
        h.fire(3)  # 第 3 次已熔断
        assert len(h.received) == 2
        h.clock.advance(601.0)
        h.fire(1)
        assert len(h.received) == 3, "窗滚过后恢复注入"
        assert "第 2 次" not in h.received[2][1], "重新计数后为首报"

    def test_window_rollover_boundary_exact_width_restarts(self) -> None:
        """边界：老化条件为年龄 ≥ 窗宽——599s 仍在窗内熔断，600s 整点重启。"""
        h = BreakerHarness()
        h.fire(2)
        h.clock.advance(599.0)
        h.fire(1)
        assert len(h.received) == 2, "窗宽内旧注入未老化，仍熔断"
        h.clock.advance(1.0)
        h.fire(1)
        assert len(h.received) == 3, "年龄恰达窗宽即老化，重新计数"
        assert "第 2 次" not in h.received[2][1], "重启后为首报"

    def test_keys_are_independent_dimensions(self) -> None:
        """(parent, task, event) 三维任一不同即为新键，各自从首报起算。"""
        h = BreakerHarness()
        h.fire(2)  # (parent_pipe, t_loop, task_failed) 达 2 次
        h.fire(1, task_id="t_other")
        h.fire(1, parent_pipeline_id="parent2")
        _run(h.mgr.handle_domain_event("task_completed", {"parent_pipeline_id": "parent_pipe", "task_id": "t_loop", "user_id": "u_admin", "pipeline_id": "child_pipe"}))
        later = [msg for _, msg, _ in h.received[2:]]
        assert len(later) == 3, "换 task / 换 parent / 换 event 各首报一次"
        for msg in later:
            assert "第 2 次" not in msg, "新键首报不带重复提示"

    def test_property_burst_injections_monotone_non_increasing(self) -> None:
        """性质断言：连发 20 次同键事件，注入数停在阈值-1（字面 2）不再增长。"""
        h = BreakerHarness()
        total_events = 20
        h.fire(total_events)
        assert len(h.received) == 2
        assert len(h.received) < total_events, "熔断后注入次数不随事件连发增长"

    def test_empty_task_id_bypasses_gate(self) -> None:
        """事件缺 task_id 无法定熔断维度 → 退回无熔断行为，连发次次注入。"""
        h = BreakerHarness()
        for _ in range(4):
            _run(
                h.mgr.handle_domain_event(
                    "task_failed",
                    {"pipeline_id": "child_pipe", "parent_pipeline_id": "parent_pipe", "user_id": "u"},
                )
            )
        assert len(h.received) == 4, "无 task_id 不熔断（无法归键，与修复前行为一致）"

    def test_lru_cap_evicted_key_restarts_fresh(self) -> None:
        """键数超 LRU 上限（500）后最旧键被逐出：其熔断状态丢失、重新首报。"""
        h = BreakerHarness()
        h.fire(2, task_id="t_first")
        assert len(h.received) == 2
        for i in range(500):
            h.fire(2, task_id=f"t_flood_{i:03d}")
        h.fire(1, task_id="t_first")
        assert len(h.received) == 2 + 1000 + 1
        assert "第 2 次" not in h.received[-1][1], "被逐出键重新从首报起算"
