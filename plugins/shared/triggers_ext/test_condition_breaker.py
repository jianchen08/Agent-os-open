# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可观测可干预 | @ci: python-coverage
"""triggers_ext 条件触发器求值熔断（坏表达式重评防护）单元测试。P1-16 定案。

缺陷：坏表达式触发器经 state 重灌（load_from_state 不做注册期编译校验）进入
注册表后，每轮 5s 轮询重评且逐轮告警/静默吞 False，无任何熔断——重评与告警
永不收敛。

契约：按 trigger id 连续失败计数，达 _COND_FAIL_BREAK_THRESHOLD（3）次熔断——
暂停重评 _COND_BACKOFF_BASE_SECONDS（300s）起的指数退避窗（×2 递增，上限
_COND_BACKOFF_MAX_SECONDS=3600s）；窗过后放行探针重评，失败续计并加倍退避，
成功即恢复清零。熔断态双通道可见：WARNING 日志（触发器/次数/窗宽/错误）+
``trigger.metadata["cond_breaker"]``（既有 REST triggers 读面经 to_state_dict
序列化消费，零新端点）。时序断言用注入 fake clock 显式推进，禁止 sleep/零延迟 mock；
坏表达式经真实 load_from_state 生产路径构造，无任何故障注入。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/triggers_ext/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import triggers.manager as manager_mod  # noqa: E402
from triggers.manager import TriggerManager  # noqa: E402
from triggers.types import TriggerConfig, TriggerStatus, TriggerType  # noqa: E402

# 单个 '=' 是注册期即拒、重灌不校验的坏表达式样本（compile 必抛语法错）。
_BAD_EXPRESSION = "ready = true"
_GOOD_EXPRESSION = "ready == true"
_POLL_INTERVAL = 5.0


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _false_to_true_edges(values: list[bool]) -> int:
    """边沿计数（unknown→true 计一次），与 manager 边沿检测同契约。"""
    previous: bool | None = None
    edges = 0
    for value in values:
        if value and previous is not True:
            edges += 1
        previous = value
    return edges


class FakeClock:
    """可推进单调钟：退避窗不变量经显式推进断言，非 sleep。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class BreakerHarness:
    """主例（state 重灌装载）+ 可选健康对照（注册装载）驱动器。"""

    def __init__(self, main_expression: str = _BAD_EXPRESSION) -> None:
        self.clock = FakeClock()
        self.mgr = TriggerManager(clock=self.clock)
        self.main_id = "t-main"
        rows = [
            {
                "pipeline_id": "pipe-1",
                f"trigger.registry.{self.main_id}": {
                    "trigger_id": self.main_id,
                    "trigger_type": "condition",
                    "status": "active",
                    "condition_expression": main_expression,
                    "max_fires": 0,
                    "metadata": {},
                },
            }
        ]
        self.mgr.set_state_provider(lambda: rows)
        restored = _run(self.mgr.load_from_state())
        assert restored == 1, "前置：触发器经 state 重灌入表（生产真实入口）"

    @property
    def main(self) -> TriggerConfig:
        cfg = self.mgr.get(self.main_id)
        assert cfg is not None
        return cfg

    def register_healthy(self, trigger_id: str = "t-good") -> TriggerConfig:
        cfg = TriggerConfig(
            trigger_id=trigger_id,
            name="健康对照",
            trigger_type=TriggerType.CONDITION,
            condition_expression=_GOOD_EXPRESSION,
            max_fires=0,
            message="到点了",
            pipeline_id="pipe-1",
        )
        self.mgr.register(cfg)
        return cfg

    def round(self, row: dict[str, Any] | None = None) -> list[str]:
        """评估一轮（不推时钟；时序由测试显式 advance 掌控）。"""
        return self.mgr.evaluate_condition_rows([row if row is not None else {"ready": True}])

    def step(self, row: dict[str, Any] | None = None) -> list[str]:
        """推进一个轮询周期（5s）并评估一轮。"""
        self.clock.advance(_POLL_INTERVAL)
        return self.round(row)

    def reload_via_state(self, expression: str) -> None:
        """经 state 重灌替换注册表（单触发器行）。"""
        rows = [
            {
                "pipeline_id": "pipe-1",
                f"trigger.registry.{self.main_id}": {
                    "trigger_id": self.main_id,
                    "trigger_type": "condition",
                    "status": "active",
                    "condition_expression": expression,
                    "max_fires": 0,
                    "metadata": {},
                },
            }
        ]
        self.mgr.set_state_provider(lambda: rows)
        assert _run(self.mgr.load_from_state()) == 1

    def cleanup(self) -> None:
        self.mgr.stop_check_loop()


def _warnings(caplog: pytest.LogCaptureFixture, keyword: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if keyword in r.getMessage()]


class TestBadExpressionBreaker:
    """坏表达式触发器：失败告警 → 达阈熔断 → 窗内静默 → 窗后探针。"""

    def test_below_threshold_each_failed_round_warns_and_keeps_evaluating(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """前两轮连续失败逐轮 WARNING（可见性），未达阈不熔断、不点火。"""
        h = BreakerHarness()
        try:
            with caplog.at_level(logging.WARNING):
                assert h.step() == []
                assert h.step() == []
        finally:
            h.cleanup()

        fails = _warnings(caplog, "条件评估失败")
        assert len(fails) == 2, "每个失败轮各记一条评估失败告警（修复前为静默吞 False）"
        assert h.main_id in fails[0]
        assert h.main.fire_count == 0
        assert h.main.status == TriggerStatus.ACTIVE, "失败不改变触发器状态"

    def test_threshold_trip_logs_breaker_and_exposes_state(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """连续第 3 次失败熔断：WARNING 含熔断与次数，metadata.cond_breaker 可查。"""
        h = BreakerHarness()
        try:
            with caplog.at_level(logging.WARNING):
                h.step()
                h.step()
                h.step()
        finally:
            h.cleanup()

        trips = _warnings(caplog, "连续失败熔断")
        assert len(trips) == 1, "达阈本轮记熔断告警（前两轮为普通失败告警）"
        assert h.main_id in trips[0]
        assert "连续失败 3 次" in trips[0]

        state = h.main.metadata.get("cond_breaker")
        assert isinstance(state, dict)
        assert state.get("tripped") is True
        assert state.get("consecutive_failures") == 3
        assert state.get("last_error"), "熔断态携带末次错误供端点面排查"
        # 端点可见性：REST triggers 读面即 to_state_dict 序列化（http_api._serialize 同源）
        exposed = h.main.to_state_dict()["metadata"]["cond_breaker"]
        assert exposed["tripped"] is True
        assert exposed["backoff_seconds"] == manager_mod._COND_BACKOFF_BASE_SECONDS

    def test_suppressed_window_no_warning_and_healthy_sibling_unaffected(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """熔断窗内重评被跳过（零新增告警），健康触发器边沿照常点火（包含性）。"""
        h = BreakerHarness()
        good = h.register_healthy()
        try:
            h.step({"ready": True})  # 健康例 unknown→true 首边沿
            h.step({"ready": False})
            with caplog.at_level(logging.WARNING):
                h.step()  # 默认行 ready=True：健康例新边沿，坏例第 3 次失败 → 熔断
                assert good.fire_count == 2
                assert len(_warnings(caplog, "连续失败熔断")) == 1
                caplog.clear()
                h.step({"ready": True})
                assert good.fire_count == 2, "电平保持不重复点火"
                h.step({"ready": False})
                h.step({"ready": True})
                assert good.fire_count == 3, "健康例在坏例熔断期间边沿照常（包含性）"
        finally:
            h.cleanup()

        assert _warnings(caplog, "条件评估失败") == [], "窗内不得再评坏表达式（零重评零告警）"
        assert _warnings(caplog, "连续失败熔断") == [], "窗内不得重复熔断告警"

    def test_backoff_sequence_doubling_windows_on_fake_clock(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """退避序列：窗过后探针失败续熔，窗宽 ×2 递增、封顶 3600s。

        时序不变量经 fake clock 显式推进断言：每窗的 window-5s 时刻仍静默，
        恰在窗界时刻放行探针；字面窗宽配性质断言（next == min(prev×2, 上限)）。
        """
        h = BreakerHarness()
        base = manager_mod._COND_BACKOFF_BASE_SECONDS
        cap_total = manager_mod._COND_BACKOFF_MAX_SECONDS
        try:
            with caplog.at_level(logging.WARNING):
                for _ in range(3):
                    h.step()
                windows: list[float] = []
                for _ in range(6):
                    trips_before = len(_warnings(caplog, "连续失败熔断"))
                    window = h.main.metadata["cond_breaker"]["backoff_seconds"]
                    h.clock.advance(window - 2 * _POLL_INTERVAL)
                    h.step()  # 落在窗界前一个周期：必须仍被熔断
                    assert len(_warnings(caplog, "连续失败熔断")) == trips_before, "窗界前不得放行重评"
                    h.clock.advance(_POLL_INTERVAL)
                    h.step()  # 恰越窗界：探针重评，失败续熔
                    state = h.main.metadata["cond_breaker"]
                    assert state["tripped"] is True
                    windows.append(window)  # 记被消费的窗宽（探针已生成下一窗）
        finally:
            h.cleanup()

        assert windows[0] == base, "首窗为基础退避窗"
        assert windows == [base, base * 2, base * 4, base * 8, cap_total, cap_total], (
            "退避序列 ×2 递增且封顶（不再增长）"
        )
        # 性质断言：逐项满足 min(前窗×2, 上限)，且单调不减
        assert all(
            windows[i + 1] == min(windows[i] * 2, cap_total) for i in range(len(windows) - 1)
        )
        assert windows == sorted(windows)

    def test_recovery_fixed_expression_fires_after_window_and_clears_state(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """恢复正常表达式触发器：窗内修好不点火（熔断不旁路），窗后探针点火+清态。"""
        h = BreakerHarness()
        try:
            with caplog.at_level(logging.WARNING):
                for _ in range(3):
                    h.step()
                assert h.main.metadata["cond_breaker"]["tripped"] is True

            # 用户修复路径：同 id 重注册（注册写面，注册期编译校验放行合法表达式）
            fixed = TriggerConfig(
                trigger_id=h.main_id,
                name="已修复",
                trigger_type=TriggerType.CONDITION,
                condition_expression=_GOOD_EXPRESSION,
                max_fires=0,
                message="到点了",
                pipeline_id="pipe-1",
            )
            h.mgr.register(fixed)
            assert h.step({"ready": True}) == []
            assert h.main.fire_count == 0, "熔断窗内即使表达式已修好也不放行（不旁路）"

            h.clock.advance(manager_mod._COND_BACKOFF_BASE_SECONDS + _POLL_INTERVAL)
            with caplog.at_level(logging.INFO):
                assert h.step({"ready": True}) == [h.main_id], "窗后探针成功：恢复点火"
        finally:
            h.cleanup()

        recoveries = [r.getMessage() for r in caplog.records if "恢复" in r.getMessage()]
        assert recoveries, "恢复留 INFO 痕迹"
        assert h.main_id in recoveries[0]
        assert "cond_breaker" not in h.main.metadata, "恢复即清熔断态（端点面回归健康态）"
        assert h.step({"ready": False}) == []
        assert h.step({"ready": True}) == [h.main_id], "恢复后边沿检测照常（非一次性放行）"

    def test_unregister_drops_breaker_state(self, caplog: pytest.LogCaptureFixture) -> None:
        """注销即清熔断运行态：同 id 重灌不继承旧熔断窗（防串账）。"""
        h = BreakerHarness()
        try:
            with caplog.at_level(logging.WARNING):
                for _ in range(3):
                    h.step()
                assert h.main.metadata["cond_breaker"]["tripped"] is True
                h.mgr.unregister(h.main_id)
                assert h.mgr.get(h.main_id) is None

            h.reload_via_state(_BAD_EXPRESSION)
            h.clock.advance(1.0)  # 旧窗远未过（300s 基窗），若串账则此处被熔断
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                h.step()
        finally:
            h.cleanup()

        assert len(_warnings(caplog, "条件评估失败")) == 1, "重灌后从第 1 次失败重新计数"


class TestHealthyTriggerNeverTrips:
    """区分度对照组：健康表达式触发器长期轮询不误伤、边沿照常。"""

    @pytest.mark.parametrize("expression", [_GOOD_EXPRESSION, "ready != false"])
    def test_long_run_no_breaker_no_warning(
        self, caplog: pytest.LogCaptureFixture, expression: str
    ) -> None:
        """多轮翻转行：恰在每个 false→true 边沿各点火一次，零熔断零失败告警。"""
        cycles = 4
        flips = [
            {"ready": True},
            {"ready": False},
            {"ready": True},
            {"ready": False},
            {"ready": True},
        ]
        h = BreakerHarness(main_expression=expression)  # 重灌例本身健康
        cfg = h.main
        flat = [bool(row["ready"]) for row in flips] * cycles
        expected_fires = _false_to_true_edges(flat)

        fired_total = 0
        try:
            with caplog.at_level(logging.WARNING):
                for _ in range(cycles):
                    for row in flips:
                        fired_total += len(h.step(row))
        finally:
            h.cleanup()

        assert cfg.fire_count == fired_total == expected_fires, "点火数恰为边沿数（性质断言）"
        assert _warnings(caplog, "条件评估失败") == []
        assert _warnings(caplog, "连续失败熔断") == []
        assert "cond_breaker" not in cfg.metadata
