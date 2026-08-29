# @feature: FP-0.2.可观测性 | @ci: python-coverage
"""CostControlPlugin（管道 Input 插件）预算检查行为测试。

钉死语义（task_observability 1c：预算管控默认只做可观测）：
- 每轮写 cost_control.budget / usage_percent / exceeded；
- usage_percent 与 track.total_tokens 成比例（used/budget*100，一位小数）；
- 超预算 → should_stop=True（终止标志，由 stop_check 消费）；
- 预算解析优先级：任务 metadata.token_budget > state cost_control.budget >
  插件 default_budget；task_service 未接线回退后续来源。
"""

from __future__ import annotations

from typing import Any

import pytest
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys

pytestmark = pytest.mark.unit


def _ctx(state: dict[str, Any], services: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=state, config={}, _services=services or {})


def _plugin(config: dict[str, Any] | None = None):
    from plugin import CostControlPlugin  # noqa: PLC0415

    return CostControlPlugin(config=config)


class TestBudgetResolution:
    def test_state_budget_used_when_no_task_service(self) -> None:
        """task_service 未接线：回退 state cost_control.budget。"""
        plugin = _plugin({"default_budget": 1_000_000})
        ctx = _ctx({"cost_control.budget": 500, "track.total_tokens": 250})
        updates = _run(plugin.execute(ctx)).state_updates
        assert updates["cost_control.budget"] == 500
        assert updates["cost_control.usage_percent"] == 50.0

    def test_default_budget_fallback(self) -> None:
        """state 无预算配置：取插件 default_budget。"""
        plugin = _plugin({"default_budget": 200})
        ctx = _ctx({"track.total_tokens": 40})
        updates = _run(plugin.execute(ctx)).state_updates
        assert updates["cost_control.budget"] == 200
        assert updates["cost_control.usage_percent"] == 20.0

    def test_task_metadata_budget_highest_priority(self) -> None:
        """task_service 已接线且 metadata.token_budget 存在：优先于 state/default。"""

        class _FakeTask:
            metadata = {"token_budget": 1000}

        class _FakeTaskService:
            def get_task(self, task_id: str) -> Any:
                return _FakeTask()

        plugin = _plugin({"default_budget": 999_999})
        ctx = _ctx(
            {StateKeys.TASK_ID: "t1", "cost_control.budget": 500, "track.total_tokens": 100},
            {"task_service": _FakeTaskService()},
        )
        updates = _run(plugin.execute(ctx)).state_updates
        assert updates["cost_control.budget"] == 1000
        assert updates["cost_control.usage_percent"] == 10.0


class TestBudgetCheck:
    def test_under_budget_no_stop(self) -> None:
        """未超预算：exceeded=False，不写 should_stop。"""
        plugin = _plugin({"default_budget": 1_000_000})
        ctx = _ctx({"track.total_tokens": 12_345})
        updates = _run(plugin.execute(ctx)).state_updates
        assert updates["cost_control.exceeded"] is False
        assert StateKeys.SHOULD_STOP not in updates

    def test_over_budget_stops(self) -> None:
        """超预算：exceeded=True 且 should_stop=True。"""
        plugin = _plugin({"default_budget": 100})
        ctx = _ctx({"track.total_tokens": 101})
        updates = _run(plugin.execute(ctx)).state_updates
        assert updates["cost_control.exceeded"] is True
        assert updates[StateKeys.SHOULD_STOP] is True

    def test_usage_percent_proportional(self) -> None:
        """性质断言：usage_percent = used/budget*100（一位小数），不同输入成立。"""
        plugin = _plugin({"default_budget": 8000})
        for used, expected in ((0, 0.0), (800, 10.0), (4321, 54.0), (8000, 100.0)):
            ctx = _ctx({"track.total_tokens": used})
            updates = _run(plugin.execute(ctx)).state_updates
            assert updates["cost_control.usage_percent"] == expected

    def test_disabled_writes_disabled_markers(self) -> None:
        """enabled=False：不评估，仅落预算哨兵值。"""
        plugin = _plugin({"enabled": False, "default_budget": 777})
        ctx = _ctx({"track.total_tokens": 999_999})
        updates = _run(plugin.execute(ctx)).state_updates
        assert updates == {"cost_control.budget": 777, "cost_control.exceeded": False}


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
