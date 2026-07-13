"""M6b Input 插件测试 — security_check, param_inject, reasoning_check。

验证三个安全模块 Input 插件的独立功能。
"""

from __future__ import annotations

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import ErrorPolicy, StateKeys, create_initial_state
from plugins.input.param_inject import ParamInjectPlugin
from plugins.input.reasoning_check import ReasoningCheckPlugin
from plugins.input.security_check import SecurityCheckPlugin


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


# ── SecurityCheckPlugin Tests ──


class TestSecurityCheckPlugin:
    """安全检查插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = SecurityCheckPlugin()
        assert plugin.name == "security_check"
        assert plugin.priority == 70
        assert plugin.error_policy == ErrorPolicy.ABORT

    @pytest.mark.asyncio
    async def test_disabled_returns_allowed(self, ctx):
        """测试禁用时返回允许。"""
        plugin = SecurityCheckPlugin({"enabled": False})
        result = await plugin.execute(ctx)

        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_llm_call_always_allowed(self, ctx, base_state):
        """测试 LLM 调用始终允许。"""
        base_state[StateKeys.CORE_TYPE] = "llm_call"
        plugin = SecurityCheckPlugin()
        result = await plugin.execute(ctx)

        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_safe_tool_call_allowed(self, ctx, base_state):
        """测试安全的工具调用通过。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "read_file", "args": {"path": "/workspace/test.py"}},
        ]
        plugin = SecurityCheckPlugin({"workspace": "/workspace"})
        result = await plugin.execute(ctx)

        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_dangerous_command_blocked(self, ctx, base_state):
        """测试危险命令被拦截。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "execute_command", "args": {"command": "rm -rf /"}},
        ]
        plugin = SecurityCheckPlugin()
        result = await plugin.execute(ctx)

        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is False
        assert "Blocked by security rule: dangerous_commands" in decision["reason"]

    @pytest.mark.asyncio
    async def test_protected_path_blocked(self, ctx, base_state):
        """测试受保护路径被拦截。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "write_file", "args": {"path": "/etc/passwd", "content": "hacked"}},
        ]
        plugin = SecurityCheckPlugin()
        result = await plugin.execute(ctx)

        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is False
        assert "Blocked by security rule: protected_paths" in decision["reason"]

    @pytest.mark.asyncio
    async def test_out_of_workspace_blocked(self, ctx, base_state):
        """测试工作目录外访问被拦截。"""
        import os
        import tempfile
        # 使用真实的临时目录确保跨平台兼容
        workspace = tempfile.gettempdir()
        outside_path = os.path.join(os.path.dirname(workspace), "outside_test", "file.py")
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "write_file", "args": {"path": outside_path, "content": "x"}},
        ]
        plugin = SecurityCheckPlugin({"workspace": workspace})
        result = await plugin.execute(ctx)

        decision = result.state_updates["security.decision"]
        # outside_path 不在 workspace 内应被拦截
        # 但如果 workspace 恰好是父目录可能通过，所以检查逻辑合理性
        if not outside_path.startswith(os.path.abspath(workspace)):
            assert decision["allowed"] is False

    @pytest.mark.asyncio
    async def test_no_tool_calls_passes(self, ctx, base_state):
        """测试无工具调用时通过。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.RAW_TOOL_CALLS] = []
        plugin = SecurityCheckPlugin()
        result = await plugin.execute(ctx)

        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_custom_blocked_commands(self, ctx, base_state):
        """测试自定义拦截命令。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "execute_command", "args": {"command": "custom_danger"}},
        ]
        plugin = SecurityCheckPlugin({
            "rules": [
                {
                    "name": "custom_blocked",
                    "tools": ["*"],
                    "params": ["command", "cmd"],
                    "action": "block",
                    "patterns": [
                        {"type": "keyword", "value": "custom_danger"}
                    ]
                }
            ]
        })
        result = await plugin.execute(ctx)

        decision = result.state_updates["security.decision"]
        assert decision["allowed"] is False


# ── ParamInjectPlugin Tests ──


class TestParamInjectPlugin:
    """参数注入插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = ParamInjectPlugin()
        assert plugin.name == "param_inject"
        assert plugin.priority == 20
        assert plugin.error_policy == ErrorPolicy.ABORT

    @pytest.mark.asyncio
    async def test_llm_call_skips_injection(self, ctx, base_state):
        """测试 LLM 调用跳过注入。"""
        base_state[StateKeys.CORE_TYPE] = "llm_call"
        plugin = ParamInjectPlugin()
        result = await plugin.execute(ctx)

        assert result.state_updates["tool.params_injected"] is False

    @pytest.mark.asyncio
    async def test_injects_session_id(self, ctx, base_state):
        """测试注入会话 ID。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "read_file", "args": {"path": "test.py"}},
        ]
        plugin = ParamInjectPlugin()
        result = await plugin.execute(ctx)

        tool_calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert tool_calls[0]["args"]["session_id"] == "test-session"
        assert result.state_updates["tool.params_injected"] is True

    @pytest.mark.asyncio
    async def test_does_not_override_existing_params(self, ctx, base_state):
        """测试不覆盖已有参数。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "read_file", "args": {"path": "test.py", "session_id": "custom-id"}},
        ]
        plugin = ParamInjectPlugin()
        result = await plugin.execute(ctx)

        tool_calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert tool_calls[0]["args"]["session_id"] == "custom-id"

    @pytest.mark.asyncio
    async def test_injects_task_id_when_absent(self, ctx, base_state):
        """task_id 缺失时从 state 注入。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.TASK_ID] = "task-001"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "task_submit", "args": {"goal": {"title": "x"}}},
        ]
        plugin = ParamInjectPlugin()
        result = await plugin.execute(ctx)

        tool_calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert tool_calls[0]["args"]["task_id"] == "task-001"

    @pytest.mark.asyncio
    async def test_injects_task_id_overriding_empty_value(self, ctx, base_state):
        """LLM 传入空 task_id（null/""）时仍注入 state 的有效值。

        回归 fix_20260619_l2_parent_task_id_lost：
        原条件 `if "task_id" not in args` 会因 args 含空值键而跳过注入，
        导致 L2 task_submit 拿不到 parent_task_id。空值应被视为「无值」并注入。
        """
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.TASK_ID] = "task-002"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            # 模拟 LLM 传入空字符串 task_id
            {"name": "task_submit", "args": {"task_id": "", "goal": {"title": "x"}}},
        ]
        plugin = ParamInjectPlugin()
        result = await plugin.execute(ctx)

        tool_calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert tool_calls[0]["args"]["task_id"] == "task-002"

    @pytest.mark.asyncio
    async def test_keeps_valid_task_id_from_args(self, ctx, base_state):
        """args 中已有有效 task_id 时不覆盖。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.TASK_ID] = "state-task"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "task_submit", "args": {"task_id": "args-task", "goal": {"title": "x"}}},
        ]
        plugin = ParamInjectPlugin()
        result = await plugin.execute(ctx)

        tool_calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert tool_calls[0]["args"]["task_id"] == "args-task"

    @pytest.mark.asyncio
    async def test_injects_timestamp(self, ctx, base_state):
        """测试注入时间戳。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "read_file", "args": {"path": "test.py"}},
        ]
        plugin = ParamInjectPlugin()
        result = await plugin.execute(ctx)

        tool_calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert "timestamp" in tool_calls[0]["args"]
        assert "T" in tool_calls[0]["args"]["timestamp"]  # ISO format

    @pytest.mark.asyncio
    async def test_default_params(self, ctx, base_state):
        """测试工具默认参数注入。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "read_file", "args": {}},
        ]
        plugin = ParamInjectPlugin({
            "default_params": {"read_file": {"encoding": "utf-8"}},
        })
        result = await plugin.execute(ctx)

        tool_calls = result.state_updates[StateKeys.RAW_TOOL_CALLS]
        assert tool_calls[0]["args"]["encoding"] == "utf-8"

    @pytest.mark.asyncio
    async def test_no_tool_calls(self, ctx, base_state):
        """测试无工具调用。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        base_state[StateKeys.RAW_TOOL_CALLS] = []
        plugin = ParamInjectPlugin()
        result = await plugin.execute(ctx)

        assert result.state_updates["tool.params_injected"] is False


# ── ReasoningCheckPlugin Tests ──


class TestReasoningCheckPlugin:
    """推理检查插件测试。"""

    def test_name_and_priority(self):
        """测试插件名称和优先级。"""
        plugin = ReasoningCheckPlugin()
        assert plugin.name == "reasoning_check"
        assert plugin.priority == 75
        assert plugin.error_policy == ErrorPolicy.SKIP

    @pytest.mark.asyncio
    async def test_disabled_returns_passed(self, ctx):
        """测试禁用时返回通过。"""
        plugin = ReasoningCheckPlugin({"enabled": False})
        result = await plugin.execute(ctx)

        check_result = result.state_updates["reasoning.check_result"]
        assert check_result["passed"] is True

    @pytest.mark.asyncio
    async def test_tool_execution_passes(self, ctx, base_state):
        """测试工具执行轮次跳过检查。"""
        base_state[StateKeys.CORE_TYPE] = "tool_execute"
        plugin = ReasoningCheckPlugin()
        result = await plugin.execute(ctx)

        check_result = result.state_updates["reasoning.check_result"]
        assert check_result["passed"] is True

    @pytest.mark.asyncio
    async def test_no_output_passes(self, ctx, base_state):
        """测试无输出时通过。"""
        base_state[StateKeys.CORE_TYPE] = "llm_call"
        base_state[StateKeys.RAW_RESULT] = ""
        plugin = ReasoningCheckPlugin()
        result = await plugin.execute(ctx)

        check_result = result.state_updates["reasoning.check_result"]
        assert check_result["passed"] is True

    @pytest.mark.asyncio
    async def test_normal_reasoning_passes(self, ctx, base_state):
        """测试正常推理通过。"""
        base_state[StateKeys.CORE_TYPE] = "llm_call"
        base_state[StateKeys.RAW_RESULT] = "根据你的要求，我建议使用Python来实现。以下是一个简单的示例。"
        plugin = ReasoningCheckPlugin()
        result = await plugin.execute(ctx)

        check_result = result.state_updates["reasoning.check_result"]
        assert check_result["passed"] is True

    @pytest.mark.asyncio
    async def test_excessive_steps_fails(self, ctx, base_state):
        """测试过度推理步数检测。"""
        base_state[StateKeys.CORE_TYPE] = "llm_call"
        # 构造超过阈值的推理步骤
        steps = "\n".join(f"步骤{i}: 这是第{i}步推理" for i in range(25))
        base_state[StateKeys.RAW_RESULT] = steps
        plugin = ReasoningCheckPlugin({"max_reasoning_steps": 20})
        result = await plugin.execute(ctx)

        check_result = result.state_updates["reasoning.check_result"]
        assert check_result["passed"] is False
        assert "Too many reasoning steps" in check_result["reason"]

    @pytest.mark.asyncio
    async def test_very_long_reasoning_fails(self, ctx, base_state):
        """测试超长推理检测。"""
        base_state[StateKeys.CORE_TYPE] = "llm_call"
        base_state[StateKeys.RAW_RESULT] = "x" * 20000  # 超过 max_tokens
        plugin = ReasoningCheckPlugin({"max_reasoning_tokens": 4096})
        result = await plugin.execute(ctx)

        check_result = result.state_updates["reasoning.check_result"]
        assert check_result["passed"] is False
        assert "too long" in check_result["reason"].lower() or "Reasoning too long" in check_result["reason"]
