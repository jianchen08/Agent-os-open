"""委派等待策略插件测试。

覆盖 FireAndForgetPlugin / EventCallbackPlugin。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.output.fire_and_forget import FireAndForgetPlugin
from plugins.output.event_callback import EventCallbackPlugin


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
