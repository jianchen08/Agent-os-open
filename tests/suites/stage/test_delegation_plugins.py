"""委派等待策略插件测试。

覆盖 WaitForResultPlugin / FireAndForgetPlugin / EventCallbackPlugin。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.output.wait_for_result import WaitForResultPlugin
from plugins.output.fire_and_forget import FireAndForgetPlugin
from plugins.output.event_callback import EventCallbackPlugin


# ---------------------------------------------------------------------------
# WaitForResultPlugin
# ---------------------------------------------------------------------------


class TestWaitForResultPlugin:
    """WaitForResultPlugin 测试。"""

    def test_name_and_priority(self) -> None:
        """基本属性测试。"""
        registry = MagicMock()
        plugin = WaitForResultPlugin(registry=registry)
        assert plugin.name == "wait_for_result"
        assert plugin.priority == 5
        assert plugin.route_signals == []

    @pytest.mark.asyncio
    async def test_no_routed_to(self) -> None:
        """无 ROUTED_TO 时直接返回空结果。"""
        registry = MagicMock()
        plugin = WaitForResultPlugin(registry=registry)
        ctx = PluginContext(state={}, config={})

        result = await plugin.execute(ctx)
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_immediate_result_available(self) -> None:
        """结果立即可用时直接返回。"""
        registry = MagicMock()
        registry.get_result.return_value = {"output": "done", "score": 0.95}
        plugin = WaitForResultPlugin(registry=registry, poll_interval=0.01, timeout=1.0)
        ctx = PluginContext(state={StateKeys.ROUTED_TO: "pipeline-1"}, config={})

        result = await plugin.execute(ctx)
        assert StateKeys.DELEGATION_RESULT in result.state_updates
        assert result.state_updates[StateKeys.DELEGATION_RESULT]["output"] == "done"
        assert StateKeys.DELEGATION_SCORE in result.state_updates
        assert result.state_updates[StateKeys.DELEGATION_SCORE] == 0.95

    @pytest.mark.asyncio
    async def test_poll_until_result_available(self) -> None:
        """轮询直到结果可用。"""
        registry = MagicMock()
        # 前两次返回 None，第三次返回结果
        registry.get_result.side_effect = [None, None, {"output": "late"}]
        plugin = WaitForResultPlugin(registry=registry, poll_interval=0.01, timeout=5.0)
        ctx = PluginContext(state={StateKeys.ROUTED_TO: "pipeline-2"}, config={})

        result = await plugin.execute(ctx)
        assert StateKeys.DELEGATION_RESULT in result.state_updates
        assert result.state_updates[StateKeys.DELEGATION_RESULT]["output"] == "late"

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        """超时后设 DELEGATION_ERROR。"""
        registry = MagicMock()
        registry.get_result.return_value = None  # 始终无结果
        plugin = WaitForResultPlugin(registry=registry, poll_interval=0.01, timeout=0.05)
        ctx = PluginContext(state={StateKeys.ROUTED_TO: "pipeline-3"}, config={})

        result = await plugin.execute(ctx)
        assert StateKeys.DELEGATION_ERROR in result.state_updates
        assert "timeout" in result.state_updates[StateKeys.DELEGATION_ERROR].lower()

    @pytest.mark.asyncio
    async def test_result_without_score(self) -> None:
        """结果中没有评分时不设 DELEGATION_SCORE。"""
        registry = MagicMock()
        registry.get_result.return_value = {"output": "no_score"}
        plugin = WaitForResultPlugin(registry=registry, poll_interval=0.01, timeout=1.0)
        ctx = PluginContext(state={StateKeys.ROUTED_TO: "pipeline-4"}, config={})

        result = await plugin.execute(ctx)
        assert StateKeys.DELEGATION_RESULT in result.state_updates
        assert StateKeys.DELEGATION_SCORE not in result.state_updates


# ---------------------------------------------------------------------------
# FireAndForgetPlugin
# ---------------------------------------------------------------------------


class TestFireAndForgetPlugin:
    """FireAndForgetPlugin 测试。"""

    def test_name_and_priority(self) -> None:
        """基本属性测试。"""
        plugin = FireAndForgetPlugin()
        assert plugin.name == "fire_and_forget"
        assert plugin.priority == 5
        assert plugin.route_signals == []

    @pytest.mark.asyncio
    async def test_execute_returns_empty(self) -> None:
        """execute 始终返回空 OutputResult。"""
        plugin = FireAndForgetPlugin()
        ctx = PluginContext(state={StateKeys.ROUTED_TO: "pipeline-1"}, config={})

        result = await plugin.execute(ctx)
        assert result.state_updates == {}
        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_execute_no_routed_to(self) -> None:
        """无 ROUTED_TO 也正常返回。"""
        plugin = FireAndForgetPlugin()
        ctx = PluginContext(state={}, config={})

        result = await plugin.execute(ctx)
        assert result.state_updates == {}


# ---------------------------------------------------------------------------
# EventCallbackPlugin
# ---------------------------------------------------------------------------


class TestEventCallbackPlugin:
    """EventCallbackPlugin 测试。"""

    def test_name_and_priority(self) -> None:
        """基本属性测试。"""
        event_bus = MagicMock()
        plugin = EventCallbackPlugin(event_bus=event_bus)
        assert plugin.name == "event_callback"
        assert plugin.priority == 5
        assert plugin.route_signals == []

    @pytest.mark.asyncio
    async def test_no_routed_to(self) -> None:
        """无 ROUTED_TO 时直接返回空结果。"""
        event_bus = MagicMock()
        plugin = EventCallbackPlugin(event_bus=event_bus)
        ctx = PluginContext(state={}, config={})

        result = await plugin.execute(ctx)
        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_with_routed_to(self) -> None:
        """有 ROUTED_TO 时设 ENDED=True 和 WAIT_FOR。"""
        event_bus = MagicMock()
        plugin = EventCallbackPlugin(event_bus=event_bus)
        ctx = PluginContext(
            state={StateKeys.ROUTED_TO: "pipeline-5"},
            config={},
        )

        result = await plugin.execute(ctx)
        assert result.state_updates[StateKeys.ENDED] is True
        assert result.state_updates[StateKeys.WAIT_FOR] == "pipeline-5"

    @pytest.mark.asyncio
    async def test_suspends_pipeline(self) -> None:
        """验证管道挂起行为：ENDED=True + WAIT_FOR 联合使用。"""
        event_bus = MagicMock()
        plugin = EventCallbackPlugin(event_bus=event_bus)
        ctx = PluginContext(
            state={StateKeys.ROUTED_TO: "child-pipeline"},
            config={},
        )

        result = await plugin.execute(ctx)
        # ENDED + WAIT_FOR 组合表示管道挂起等待事件
        assert result.state_updates[StateKeys.ENDED] is True
        assert result.state_updates[StateKeys.WAIT_FOR] == "child-pipeline"
