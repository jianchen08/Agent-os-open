# @feature: FP-0.2.可观测性 | @ci: python-coverage
"""TerminationAdvisorPlugin 主动式终止判断测试（task_observability 1c）。

钉死语义：
- 整合既有信号做主动出口（不替换它们）：
  * cost_control.exceeded / usage_percent ≥ 100 → 预算耗尽 → SHOULD_STOP
  * stuck_detector.stuck_detected → 卡死 → SHOULD_STOP
  * track.execution_stats.iteration ≥ max_iterations → 步数上限 → SHOULD_STOP
  * track.execution_stats.elapsed_total ≥ max_elapsed_s → 耗时上限 → SHOULD_STOP
- 每轮写 state["termination_advisor.status"]：
  convergence（converging/stalled/budget_critical）、remaining_budget_percent、
  iteration、elapsed_s
- 每轮经 frontend.emit 推 termination_status 事件（前端「剩余预算」+
  「收敛信号」指示器数据源）；frontend 服务缺失时静默跳过
- 正常收敛轮不写 SHOULD_STOP
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pipeline.plugin import PluginContext

pytestmark = pytest.mark.unit

PIPELINE_ID = "pipeline_term_001"


class _FakeFrontend:
    """假 frontend emitter：记录 emit(event, payload) 调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event: str, payload: dict[str, Any]) -> None:
        self.calls.append((event, payload))


def _make_plugin(config: dict[str, Any] | None = None):
    """构造插件（惰性导入：裸名 plugin 需在 setup 期 sys.path 就绪后解析）。"""
    from plugin import TerminationAdvisorPlugin  # noqa: PLC0415

    return TerminationAdvisorPlugin(config=config)


def _ctx(state: dict[str, Any], services: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=state, config={}, _services=services or {})


def _base_state(**overrides: Any) -> dict[str, Any]:
    """正常收敛轮的 state：预算充裕、无卡死、步数/耗时空闲。"""
    state = {
        "core_type": "llm_call",
        "session_id": "session_term",
        "pipeline_id": PIPELINE_ID,
        "iteration": 3,
        "cost_control.budget": 1000000,
        "cost_control.usage_percent": 12.5,
        "cost_control.exceeded": False,
        "stuck_detected": False,
        "stuck_reason": "",
        "track.execution_stats": {"iteration": 3, "elapsed_total": 45.0},
    }
    state.update(overrides)
    return state


def _status(updates: dict[str, Any]) -> dict[str, Any]:
    return updates.get("termination_advisor.status", {})


def test_cost_control_exceeded_stops() -> None:
    plugin = _make_plugin()
    updates = asyncio.run(plugin._do_work(_ctx(_base_state(**{"cost_control.exceeded": True}))))
    assert updates["should_stop"] is True
    assert "budget" in _status(updates)["stop_reason"]


def test_usage_percent_over_100_stops() -> None:
    plugin = _make_plugin()
    updates = asyncio.run(
        plugin._do_work(_ctx(_base_state(**{"cost_control.usage_percent": 100.0})))
    )
    assert updates["should_stop"] is True
    assert _status(updates)["convergence"] == "budget_critical"


def test_stuck_detected_stops() -> None:
    plugin = _make_plugin()
    updates = asyncio.run(
        plugin._do_work(_ctx(_base_state(stuck_detected=True, stuck_reason="tool repeat x3")))
    )
    assert updates["should_stop"] is True
    status = _status(updates)
    assert status["convergence"] == "stalled"
    assert "tool repeat x3" in status["stop_reason"]


def test_iteration_over_cap_stops() -> None:
    plugin = _make_plugin()
    updates = asyncio.run(
        plugin._do_work(_ctx(_base_state(**{"track.execution_stats": {"iteration": 51, "elapsed_total": 45.0}})))
    )
    assert updates["should_stop"] is True
    assert "iteration" in _status(updates)["stop_reason"]


def test_config_overrides_max_iterations() -> None:
    plugin = _make_plugin({"max_iterations": 10})
    updates = asyncio.run(
        plugin._do_work(_ctx(_base_state(**{"track.execution_stats": {"iteration": 11, "elapsed_total": 45.0}})))
    )
    assert updates["should_stop"] is True


def test_elapsed_over_cap_stops() -> None:
    plugin = _make_plugin()
    updates = asyncio.run(
        plugin._do_work(_ctx(_base_state(**{"track.execution_stats": {"iteration": 3, "elapsed_total": 3601.0}})))
    )
    assert updates["should_stop"] is True
    assert "elapsed" in _status(updates)["stop_reason"]


def test_normal_round_no_stop_and_status_written() -> None:
    plugin = _make_plugin()
    updates = asyncio.run(plugin._do_work(_ctx(_base_state())))
    assert "should_stop" not in updates
    status = _status(updates)
    assert status["convergence"] == "converging"
    assert status["should_stop"] is False
    assert status["remaining_budget_percent"] == 87.5
    assert status["iteration"] == 3
    assert status["elapsed_s"] == 45.0


def test_status_pushed_via_frontend() -> None:
    plugin = _make_plugin()
    frontend = _FakeFrontend()
    asyncio.run(plugin._do_work(_ctx(_base_state(), {"frontend": frontend})))
    events = [payload for event, payload in frontend.calls if event == "termination_status"]
    assert len(events) == 1
    assert events[0]["pipeline_id"] == PIPELINE_ID
    assert events[0]["thread_id"] == "session_term"
    assert events[0]["convergence"] == "converging"
    assert events[0]["remaining_budget_percent"] == 87.5


def test_silent_without_frontend() -> None:
    """无 frontend 服务：不推送事件，但终止判定逻辑照常产出。"""
    plugin = _make_plugin()
    updates = asyncio.run(plugin._do_work(_ctx(_base_state())))
    assert "should_stop" not in updates
    status = _status(updates)
    assert status["convergence"] == "converging"
    assert status["should_stop"] is False


def test_disabled_returns_empty() -> None:
    plugin = _make_plugin({"enabled": False})
    updates = asyncio.run(plugin._do_work(_ctx(_base_state())))
    assert updates == {}


def test_no_cost_control_state_is_tolerated() -> None:
    """cost_control 未接入管道时不误判（预算信号缺失 → 剩余预算 None）。"""
    plugin = _make_plugin()
    state = _base_state()
    for key in ("cost_control.budget", "cost_control.usage_percent", "cost_control.exceeded"):
        state.pop(key)
    updates = asyncio.run(plugin._do_work(_ctx(state)))
    assert "should_stop" not in updates
    assert _status(updates)["remaining_budget_percent"] is None
    assert _status(updates)["convergence"] == "converging"
