"""M6c Output 插件测试 — result_format, track。

验证两个 Output 插件的独立功能。
PersistPlugin 已移除（其功能合并到 TrackPlugin）。
MemoryWritePlugin 已废弃移除。
"""

from __future__ import annotations


import pytest

from pipeline.plugin import PluginContext
from pipeline.types import ErrorPolicy, StateKeys, create_initial_state
from plugins.output.result_format import ResultFormatPlugin
from plugins.output.track import TrackPlugin


# ── Fixtures ──


@pytest.fixture
def base_state() -> dict:
    """创建基础测试状态。"""
    return create_initial_state(
        session_id="test-session",
        task_id="test-task",
    )


@pytest.fixture
def ctx(base_state) -> PluginContext:
    """创建基础测试上下文。"""
    return PluginContext(state=base_state)


# ── ResultFormatPlugin Tests ──


class TestResultFormatPlugin:
    """结果格式化插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = ResultFormatPlugin()
        assert plugin.name == "result_format"
        assert plugin.priority == 20
        assert plugin.error_policy == ErrorPolicy.SKIP

    @pytest.mark.asyncio
    async def test_llm_call_skips_formatting(self, ctx, base_state):
        """测试 LLM 调用跳过格式化。"""
        base_state[StateKeys.CORE_TYPE] = "llm_call"
        plugin = ResultFormatPlugin()
        result = await plugin.execute(ctx)

        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_formats_successful_result(self, ctx, base_state):
        """测试格式化成功的工具结果。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.TOOL_RESULTS] = [
            {"name": "read_file", "success": True, "result": "file content here"},
        ]
        plugin = ResultFormatPlugin()
        result = await plugin.execute(ctx)

        formatted = result.state_updates["tool.formatted_results"]
        assert len(formatted) == 1
        assert formatted[0]["role"] == "tool"
        assert formatted[0]["name"] == "read_file"
        assert "file content here" in formatted[0]["content"]

    @pytest.mark.asyncio
    async def test_formats_error_result(self, ctx, base_state):
        """测试格式化错误的工具结果。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.TOOL_RESULTS] = [
            {"name": "write_file", "success": False, "error": "Permission denied"},
        ]
        plugin = ResultFormatPlugin()
        result = await plugin.execute(ctx)

        formatted = result.state_updates["tool.formatted_results"]
        assert "Error" in formatted[0]["content"]
        assert "Permission denied" in formatted[0]["content"]

    @pytest.mark.asyncio
    async def test_truncates_long_result(self, ctx, base_state):
        """测试截断过长结果。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.TOOL_RESULTS] = [
            {"name": "read_file", "success": True, "result": "x" * 5000},
        ]
        plugin = ResultFormatPlugin({"max_result_length": 100})
        result = await plugin.execute(ctx)

        formatted = result.state_updates["tool.formatted_results"]
        assert len(formatted[0]["content"]) < 200

    @pytest.mark.asyncio
    async def test_no_tool_results(self, ctx, base_state):
        """测试无工具结果。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.TOOL_RESULTS] = []
        plugin = ResultFormatPlugin()
        result = await plugin.execute(ctx)

        assert result.state_updates == {}


# ── TrackPlugin Tests ──


class TestTrackPlugin:
    """追踪统计插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = TrackPlugin()
        assert plugin.name == "track"
        assert plugin.priority == 15
        assert plugin.error_policy == ErrorPolicy.SKIP

    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self, ctx):
        """测试禁用时返回空。"""
        plugin = TrackPlugin({"enabled": False})
        result = await plugin.execute(ctx)

        assert result.state_updates == {}

    @pytest.mark.asyncio
    async def test_tracks_execution_time(self, ctx, base_state):
        """测试追踪执行时间。"""
        base_state[StateKeys.ITERATION] = 3
        base_state[StateKeys.CORE_TYPE] = "llm_call"
        base_state[StateKeys.EXECUTION_STATUS] = "success"
        plugin = TrackPlugin()
        result = await plugin.execute(ctx)

        stats = result.state_updates["track.execution_stats"]
        assert stats["iteration"] == 3
        assert stats["elapsed_total"] >= 0  # 可能非常快导致为 0
        assert stats["elapsed_per_iteration"] >= 0
        assert stats["core_type"] == "llm_call"

    @pytest.mark.asyncio
    async def test_tracks_token_usage(self, ctx, base_state):
        """测试追踪 token 用量。"""
        base_state["llm_usage"] = {"input_tokens": 100, "output_tokens": 50}
        plugin = TrackPlugin()
        result = await plugin.execute(ctx)

        usage = result.state_updates["track.llm_usage"]
        assert usage["total_input_tokens"] == 100
        assert usage["total_output_tokens"] == 50
        assert usage["total_tokens"] == 150

    @pytest.mark.asyncio
    async def test_accumulates_token_usage(self, ctx, base_state):
        """测试累加 token 用量。"""
        base_state["llm_usage"] = {"input_tokens": 50, "output_tokens": 25}
        base_state["track.llm_usage"] = {"total_input_tokens": 100, "total_output_tokens": 50}
        plugin = TrackPlugin()
        result = await plugin.execute(ctx)

        usage = result.state_updates["track.llm_usage"]
        assert usage["total_input_tokens"] == 150
        assert usage["total_output_tokens"] == 75
        assert usage["last_input_tokens"] == 50

    @pytest.mark.asyncio
    async def test_no_route_signal(self, ctx):
        """测试不产生路由信号。"""
        plugin = TrackPlugin()
        result = await plugin.execute(ctx)
        assert result.route_signal is None
