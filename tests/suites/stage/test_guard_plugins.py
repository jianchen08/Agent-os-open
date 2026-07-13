"""阶段 3.5/3.6 守卫插件测试 — level_guard + delegate_depth_guard。

验证 Agent 层级权限守卫和委派深度守卫的独立功能。
两个插件均通过插件机制实现，不依赖基础设施改动。
"""

from __future__ import annotations

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import ErrorPolicy, StateKeys, create_initial_state
from plugins.input.level_guard import LevelGuardPlugin
from plugins.output.delegate_depth_guard import DelegateDepthGuardPlugin


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


# ── LevelGuardPlugin Tests ──


class TestLevelGuardPlugin:
    """Agent 层级权限守卫插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = LevelGuardPlugin()
        assert plugin.name == "level_guard"
        # 重构后 priority 从 65 改为 20（基于 tool_ids SSOT）
        assert plugin.priority == 20
        assert plugin.error_policy == ErrorPolicy.ABORT

    def test_custom_priority(self):
        """测试自定义优先级。"""
        plugin = LevelGuardPlugin({"priority": 70})
        assert plugin.priority == 70

    @pytest.mark.asyncio
    async def test_disabled_plugin(self):
        """测试禁用时直接放行。"""
        plugin = LevelGuardPlugin({"enabled": False})
        state = create_initial_state()
        state[StateKeys.CORE_TYPE] = "tool_execute"
        state[StateKeys.RAW_TOOL_CALLS] = [{"name": "dangerous_tool", "args": {}}]
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        assert result.state_updates["security.level_decision"]["allowed"] is True

    @pytest.mark.asyncio
    async def test_llm_call_not_checked(self, ctx):
        """LLM 调用不需要权限检查。"""
        plugin = LevelGuardPlugin()
        # 默认 core_type 是 llm_call
        result = await plugin.execute(ctx)
        assert result.state_updates["security.level_decision"]["allowed"] is True

    @pytest.mark.asyncio
    async def test_l1_allowed_basic_tools(self):
        """L1 可以调用基础工具（在 tool_ids SSOT 授权集合内）。"""
        plugin = LevelGuardPlugin()
        state = create_initial_state()
        state[StateKeys.AGENT_LEVEL] = "l1_main"
        state[StateKeys.CORE_TYPE] = "tool_execute"
        # 新逻辑：授权来源是 state["tool_ids"]（SSOT）
        state["tool_ids"] = ["task_submit", "resource_search"]
        state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "task_submit", "args": {}},
            {"name": "resource_search", "args": {}},
        ]
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_l1_blocked_unauthorized_task_tool(self):
        """L1 不能调用不在 tool_ids 授权集合中的任务类工具（硬限制）。"""
        plugin = LevelGuardPlugin()
        state = create_initial_state()
        state[StateKeys.AGENT_LEVEL] = "l1_main"
        state[StateKeys.CORE_TYPE] = "tool_execute"
        # task_manage 不在 tool_ids 中
        state["tool_ids"] = ["task_submit", "resource_search"]
        state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "task_manage", "args": {"action": "get"}},
        ]
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        assert decision["allowed"] is False
        assert "task_manage" in decision["blocked_tools"]

    @pytest.mark.asyncio
    async def test_non_task_tool_soft_allowed(self):
        """非任务类工具（如 bash/file_write）软放行：不在 tool_ids 也不拦截。

        软限制由 tool_schema 可见性过滤 + 提示词约束兜底，level_guard 不硬拦。
        """
        plugin = LevelGuardPlugin()
        state = create_initial_state()
        state[StateKeys.AGENT_LEVEL] = "l1_main"
        state[StateKeys.CORE_TYPE] = "tool_execute"
        # bash / file_write 都不在 tool_ids 中
        state["tool_ids"] = ["task_submit", "resource_search"]
        state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "bash", "args": {"command": "echo hi"}},
            {"name": "file_write", "args": {"path": "/tmp/x", "content": "y"}},
        ]
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_l2_more_tools_than_l1(self):
        """L2 的 tool_ids 包含 write_file，可以调用。"""
        plugin = LevelGuardPlugin()
        state = create_initial_state()
        state[StateKeys.AGENT_LEVEL] = "l2_subtask"
        state[StateKeys.CORE_TYPE] = "tool_execute"
        state["tool_ids"] = ["write_file", "read_file"]
        state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "write_file", "args": {"path": "/tmp/test.txt", "content": "hi"}},
        ]
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_l3_full_access(self):
        """L3 的 tool_ids 包含该工具，可以调用。"""
        plugin = LevelGuardPlugin()
        state = create_initial_state()
        state[StateKeys.AGENT_LEVEL] = "l3_atomic"
        state[StateKeys.CORE_TYPE] = "tool_execute"
        state["tool_ids"] = ["any_dangerous_tool"]
        state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "any_dangerous_tool", "args": {}},
        ]
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_custom_allowed_list(self):
        """tool_ids 未包含的工具会被拦截。"""
        plugin = LevelGuardPlugin()
        state = create_initial_state()
        state[StateKeys.AGENT_LEVEL] = "l1_main"
        state[StateKeys.CORE_TYPE] = "tool_execute"
        # tool_ids 只包含 read_file
        state["tool_ids"] = ["read_file"]
        state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "task_submit", "args": {}},
        ]
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        assert decision["allowed"] is False

    @pytest.mark.asyncio
    async def test_mixed_allowed_and_blocked(self):
        """混合调用：任务类工具部分允许、部分拦截；非任务工具不参与拦截判断。"""
        plugin = LevelGuardPlugin()
        state = create_initial_state()
        state[StateKeys.AGENT_LEVEL] = "l1_main"
        state[StateKeys.CORE_TYPE] = "tool_execute"
        # tool_ids 只授权 task_submit
        state["tool_ids"] = ["task_submit"]
        state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "task_submit", "args": {}},  # 任务类工具，允许
            {"name": "task_manage", "args": {}},  # 任务类工具，拦截
            {"name": "bash", "args": {}},  # 非任务工具，软放行（不计入 blocked）
        ]
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        assert decision["allowed"] is False
        assert "task_manage" in decision["blocked_tools"]
        assert "task_submit" not in decision["blocked_tools"]
        assert "bash" not in decision["blocked_tools"]

    @pytest.mark.asyncio
    async def test_no_tool_calls_passes(self):
        """无工具调用时通过。"""
        plugin = LevelGuardPlugin()
        state = create_initial_state()
        state[StateKeys.AGENT_LEVEL] = "l1_main"
        state[StateKeys.CORE_TYPE] = "tool_execute"
        state[StateKeys.RAW_TOOL_CALLS] = []
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        decision = result.state_updates["security.level_decision"]
        assert decision["allowed"] is True


# ── DelegateDepthGuardPlugin Tests ──


class TestDelegateDepthGuardPlugin:
    """委派深度守卫插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = DelegateDepthGuardPlugin()
        assert plugin.name == "delegate_depth_guard"
        assert plugin.priority == 3
        assert plugin.error_policy == ErrorPolicy.SKIP

    def test_route_signals(self):
        """测试关注 delegate 信号。"""
        plugin = DelegateDepthGuardPlugin()
        assert "delegate" in plugin.route_signals

    @pytest.mark.asyncio
    async def test_initializes_depth_fields(self):
        """首次运行时初始化深度字段。"""
        plugin = DelegateDepthGuardPlugin({"max_depth": 5})
        state = create_initial_state()
        # 确保没有 depth 字段
        assert "delegate_depth" not in state
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        assert result.state_updates.get("delegate_depth") == 0
        assert result.state_updates.get("max_delegate_depth") == 5

    @pytest.mark.asyncio
    async def test_no_delegation_no_signal(self):
        """无委派时不产生路由信号。"""
        plugin = DelegateDepthGuardPlugin()
        state = create_initial_state()
        state["delegate_depth"] = 0
        state["max_delegate_depth"] = 3
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_depth_within_limit(self):
        """深度在限制内，正常递增。"""
        plugin = DelegateDepthGuardPlugin({"max_depth": 3})
        state = create_initial_state()
        state["delegate_depth"] = 1
        state["max_delegate_depth"] = 3
        state[StateKeys.ROUTED_TO] = "pipeline-2"
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        assert result.state_updates.get("delegate_depth") == 2
        assert result.route_signal is None  # 未超限

    @pytest.mark.asyncio
    async def test_depth_exceeds_limit(self):
        """深度超限时拦截，产生 end 信号。"""
        plugin = DelegateDepthGuardPlugin({"max_depth": 3})
        state = create_initial_state()
        state["delegate_depth"] = 3
        state["max_delegate_depth"] = 3
        state[StateKeys.ROUTED_TO] = "pipeline-4"
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        assert result.state_updates.get("delegate_depth") == 4
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        assert "exceeded" in result.route_signal.reason
        # 验证拦截信息
        blocked = result.state_updates.get("delegation.depth_blocked")
        assert blocked is not None
        assert blocked["depth"] == 4

    @pytest.mark.asyncio
    async def test_custom_depth_keys(self):
        """自定义 depth 字段键名。"""
        plugin = DelegateDepthGuardPlugin({
            "depth_key": "custom_depth",
            "max_depth_key": "custom_max",
            "max_depth": 2,
        })
        state = create_initial_state()
        # 无自定义字段 → 初始化
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        assert result.state_updates.get("custom_depth") == 0
        assert result.state_updates.get("custom_max") == 2

    @pytest.mark.asyncio
    async def test_disabled_passes(self):
        """禁用时无操作。"""
        plugin = DelegateDepthGuardPlugin({"enabled": False})
        state = create_initial_state()
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        assert result.state_updates == {}
        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_depth_zero_first_delegation(self):
        """第一次委派：深度从 0 变 1，不超限。"""
        plugin = DelegateDepthGuardPlugin({"max_depth": 3})
        state = create_initial_state()
        state["delegate_depth"] = 0
        state["max_delegate_depth"] = 3
        state[StateKeys.ROUTED_TO] = "pipeline-1"
        ctx = PluginContext(state=state)
        result = await plugin.execute(ctx)
        assert result.state_updates.get("delegate_depth") == 1
        assert result.route_signal is None
