"""ToolContextPlugin 单元测试。

覆盖场景：
- 正常路径：tool_registry 可用，Electron 窗口信息存在
- 降级路径：tool_registry 不可用，跳过不中断
- 边界路径：tool_registry 返回空列表
- 边界路径：Electron 窗口信息为空
- 边界路径：插件禁用
- 属性验证：name、priority、error_policy
- 适配器状态：adapter_status 字段验证
- 窗口信息规范化：标准格式和旧格式兼容
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from pipeline.plugin import PluginContext, PluginResult
from pipeline.types import ErrorPolicy
from plugins.input.tool_context import ToolContextPlugin


# ── Fixture ─────────────────────────────────────────────


@pytest.fixture
def plugin() -> ToolContextPlugin:
    """创建默认配置的 ToolContextPlugin 实例。"""
    return ToolContextPlugin(config={})


@pytest.fixture
def plugin_disabled() -> ToolContextPlugin:
    """创建禁用配置的 ToolContextPlugin 实例。"""
    return ToolContextPlugin(config={"enabled": False})


def _make_ctx(
    state: dict[str, Any] | None = None,
    services: dict[str, Any] | None = None,
) -> PluginContext:
    """构建测试用 PluginContext。

    Args:
        state: 初始状态字典
        services: 服务注册表

    Returns:
        配置好的 PluginContext 实例
    """
    if state is None:
        state = {}
    if services is None:
        services = {}
    return PluginContext(state=state, _services=services)


def _make_mock_registry(
    tool_names: list[str] | None = None,
    has_handlers: dict[str, bool] | None = None,
) -> MagicMock:
    """构建 mock ToolRegistry。

    Args:
        tool_names: 注册的工具名称列表
        has_handlers: 工具是否有 handler 的映射

    Returns:
        MagicMock 实例，模拟 ToolRegistry 行为
    """
    registry = MagicMock()
    mock_tools = []
    for name in tool_names or []:
        tool = MagicMock()
        tool.name = name
        mock_tools.append(tool)

    registry.list_all.return_value = mock_tools
    registry.get_dynamic_tool_names.return_value = set(tool_names or [])

    if has_handlers:
        registry.has_handler.side_effect = lambda n: has_handlers.get(n, False)
    else:
        # 默认所有工具都有 handler（在线）
        registry.has_handler.return_value = True

    return registry


# ── 属性测试 ─────────────────────────────────────────────


class TestToolContextPluginProperties:
    """插件基本属性测试。"""

    def test_name_returns_tool_context(self, plugin: ToolContextPlugin) -> None:
        """name 属性应返回 'tool_context'。"""
        assert plugin.name == "tool_context"

    def test_default_priority_is_40(self, plugin: ToolContextPlugin) -> None:
        """默认优先级应为 40。"""
        assert plugin.priority == 40

    def test_custom_priority_from_config(self) -> None:
        """优先级应可通过 config 覆盖。"""
        p = ToolContextPlugin(config={"priority": 30})
        assert p.priority == 30

    def test_error_policy_is_fallback(self, plugin: ToolContextPlugin) -> None:
        """错误策略应为 FALLBACK。"""
        assert plugin.error_policy == ErrorPolicy.FALLBACK

    def test_fallback_state_has_tool_context(self, plugin: ToolContextPlugin) -> None:
        """fallback_state 应包含 tool_context 默认值。"""
        assert "tool_context" in plugin.fallback_state
        assert plugin.fallback_state["tool_context"]["online_tools"] == []
        assert "adapter_status" in plugin.fallback_state["tool_context"]


# ── 正常路径 ─────────────────────────────────────────────


class TestToolContextPluginNormalPath:
    """正常路径测试：tool_registry 可用，Electron 窗口信息存在。"""

    @pytest.mark.asyncio
    async def test_execute_with_registry_and_window_info(
        self, plugin: ToolContextPlugin
    ) -> None:
        """有 registry 和窗口信息时，应构建完整的 tool_context。"""
        registry = _make_mock_registry(tool_names=["bash_execute", "file_read"])
        # 使用标准 Electron WindowInfo 格式
        window_info = {
            "title": "test.py - VSCode",
            "processName": "Code",
            "x": 0,
            "y": 0,
            "width": 1920,
            "height": 1080,
        }
        ctx = _make_ctx(
            state={"electron_window": window_info},
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)

        assert isinstance(result, PluginResult)
        assert "tool_context" in result.state_updates

        tool_ctx = result.state_updates["tool_context"]
        assert "online_tools" in tool_ctx
        assert "active_window" in tool_ctx
        assert "timestamp" in tool_ctx
        assert "adapter_status" in tool_ctx

        # 验证 online_tools 包含注册的工具名
        assert "bash_execute" in tool_ctx["online_tools"]
        assert "file_read" in tool_ctx["online_tools"]

        # 验证 active_window 包含规范化后的窗口信息
        assert tool_ctx["active_window"] is not None
        assert tool_ctx["active_window"]["title"] == "test.py - VSCode"
        assert tool_ctx["active_window"]["processName"] == "Code"

        # 验证 timestamp 是合理的
        assert isinstance(tool_ctx["timestamp"], float)
        assert tool_ctx["timestamp"] <= time.time()

    @pytest.mark.asyncio
    async def test_execute_filters_online_tools_only(
        self, plugin: ToolContextPlugin
    ) -> None:
        """只应收集在线（has_handler=True）的工具名。"""
        registry = _make_mock_registry(
            tool_names=["tool_a", "tool_b", "tool_c"],
            has_handlers={"tool_a": True, "tool_b": False, "tool_c": True},
        )
        ctx = _make_ctx(
            state={},
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        tool_ctx = result.state_updates["tool_context"]

        # tool_b 没有 handler，不应出现在 online_tools
        assert "tool_a" in tool_ctx["online_tools"]
        assert "tool_b" not in tool_ctx["online_tools"]
        assert "tool_c" in tool_ctx["online_tools"]

    @pytest.mark.asyncio
    async def test_execute_with_legacy_window_info(
        self, plugin: ToolContextPlugin
    ) -> None:
        """旧格式窗口信息（app/bounds）应被兼容处理。"""
        registry = _make_mock_registry(tool_names=["bash_execute"])
        window_info = {
            "title": "test.py - VSCode",
            "app": "VSCode",
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
        }
        ctx = _make_ctx(
            state={"electron_window": window_info},
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        tool_ctx = result.state_updates["tool_context"]

        assert tool_ctx["active_window"] is not None
        assert tool_ctx["active_window"]["processName"] == "VSCode"
        assert tool_ctx["active_window"]["width"] == 800


# ── 降级路径 ─────────────────────────────────────────────


class TestToolContextPluginDegradation:
    """降级路径测试。"""

    @pytest.mark.asyncio
    async def test_registry_not_available_skips_gracefully(
        self, plugin: ToolContextPlugin
    ) -> None:
        """ToolRegistry 不可用时应跳过，不中断管道。"""
        ctx = _make_ctx(state={}, services={})

        result = await plugin.execute(ctx)

        assert isinstance(result, PluginResult)
        assert result.error is None
        # tool_context 应仍有 online_tools（为空列表）
        tool_ctx = result.state_updates["tool_context"]
        assert tool_ctx["online_tools"] == []

    @pytest.mark.asyncio
    async def test_registry_raises_exception(
        self, plugin: ToolContextPlugin
    ) -> None:
        """ToolRegistry 调用抛异常时应跳过，不中断管道。"""
        registry = MagicMock()
        registry.list_all.side_effect = RuntimeError("Registry connection failed")

        ctx = _make_ctx(state={}, services={"tool_registry": registry})

        result = await plugin.execute(ctx)

        assert isinstance(result, PluginResult)
        assert result.error is None
        tool_ctx = result.state_updates["tool_context"]
        assert tool_ctx["online_tools"] == []


# ── 边界路径 ─────────────────────────────────────────────


class TestToolContextPluginEdgeCases:
    """边界路径测试。"""

    @pytest.mark.asyncio
    async def test_empty_registry_returns_empty_online_tools(
        self, plugin: ToolContextPlugin
    ) -> None:
        """空注册表应返回空的 online_tools。"""
        registry = _make_mock_registry(tool_names=[])
        ctx = _make_ctx(state={}, services={"tool_registry": registry})

        result = await plugin.execute(ctx)
        tool_ctx = result.state_updates["tool_context"]
        assert tool_ctx["online_tools"] == []

    @pytest.mark.asyncio
    async def test_no_window_info_returns_none_active_window(
        self, plugin: ToolContextPlugin
    ) -> None:
        """无窗口信息时 active_window 应为 None。"""
        registry = _make_mock_registry(tool_names=["bash_execute"])
        ctx = _make_ctx(state={}, services={"tool_registry": registry})

        result = await plugin.execute(ctx)
        tool_ctx = result.state_updates["tool_context"]
        assert tool_ctx["active_window"] is None

    @pytest.mark.asyncio
    async def test_window_info_key_electron_window(
        self, plugin: ToolContextPlugin
    ) -> None:
        """窗口信息应从 ctx.state['electron_window'] 读取。"""
        registry = _make_mock_registry(tool_names=["bash_execute"])
        window_info = {"title": "test", "processName": "browser"}
        ctx = _make_ctx(
            state={"electron_window": window_info},
            services={"tool_registry": registry},
        )

        result = await plugin.execute(ctx)
        tool_ctx = result.state_updates["tool_context"]
        assert tool_ctx["active_window"]["title"] == "test"

    @pytest.mark.asyncio
    async def test_disabled_plugin_returns_empty_result(
        self, plugin_disabled: ToolContextPlugin
    ) -> None:
        """禁用时应返回空 tool_context。"""
        ctx = _make_ctx(state={}, services={})
        result = await plugin_disabled.execute(ctx)

        assert isinstance(result, PluginResult)
        tool_ctx = result.state_updates["tool_context"]
        assert tool_ctx["online_tools"] == []
        assert tool_ctx["active_window"] is None

    @pytest.mark.asyncio
    async def test_result_does_not_modify_original_state(
        self, plugin: ToolContextPlugin
    ) -> None:
        """执行结果应通过 PluginResult 返回，不应直接修改 ctx.state。"""
        registry = _make_mock_registry(tool_names=["file_read"])
        original_state: dict[str, Any] = {}
        ctx = _make_ctx(state=original_state, services={"tool_registry": registry})

        await plugin.execute(ctx)

        # 原始 state 不应被直接修改
        assert "tool_context" not in original_state

    @pytest.mark.asyncio
    async def test_timestamp_is_recent(
        self, plugin: ToolContextPlugin
    ) -> None:
        """timestamp 应为当前时间附近的值。"""
        registry = _make_mock_registry(tool_names=["file_read"])
        ctx = _make_ctx(state={}, services={"tool_registry": registry})

        before = time.time()
        result = await plugin.execute(ctx)
        after = time.time()

        ts = result.state_updates["tool_context"]["timestamp"]
        assert before <= ts <= after

    @pytest.mark.asyncio
    async def test_large_number_of_tools(
        self, plugin: ToolContextPlugin
    ) -> None:
        """大量工具注册时应正常工作。"""
        many_tools = [f"tool_{i:03d}" for i in range(100)]
        registry = _make_mock_registry(tool_names=many_tools)
        ctx = _make_ctx(state={}, services={"tool_registry": registry})

        result = await plugin.execute(ctx)
        tool_ctx = result.state_updates["tool_context"]
        assert len(tool_ctx["online_tools"]) == 100

    @pytest.mark.asyncio
    async def test_adapter_status_always_present(
        self, plugin: ToolContextPlugin
    ) -> None:
        """adapter_status 字段应始终存在。"""
        ctx = _make_ctx(state={}, services={})
        result = await plugin.execute(ctx)
        tool_ctx = result.state_updates["tool_context"]

        assert "adapter_status" in tool_ctx
        assert isinstance(tool_ctx["adapter_status"], dict)
