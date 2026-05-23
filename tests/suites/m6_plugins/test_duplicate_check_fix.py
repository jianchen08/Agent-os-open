"""测试: DuplicateCheckPlugin 三级渐进策略。

验证行为：
- 第一级（count < max）：注入软提示，工具调用仍执行
- 第二级（count >= max）：移除重复调用 + 注入强警告 + 路由回 LLM
- 第三级（intercepts >= hard_limit）：终止管道
  - 主 agent：注入用户通知后终止
  - 子 agent：直接终止
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys, create_initial_state
from plugins.output.duplicate_check import DuplicateCheckPlugin


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


# ── 第一级：软提示 ──


class TestSoftHint:
    """验证早期重复时注入软提示，工具调用仍执行。"""

    @pytest.mark.asyncio
    async def test_duplicate_count_1_injects_hint(self, ctx, base_state):
        """测试 count=1 时注入软提示，不拦截。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "file_read", "args": {"path": "test.jpg"}},
        ]
        base_state[StateKeys.RAW_RESULT] = None
        base_state.setdefault("messages", [])

        sig = hashlib.md5("file_read:[('path', 'test.jpg')]".encode()).hexdigest()[:8]
        base_state["router.last_tool_call"] = sig

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3})
        result = await plugin.execute(ctx)

        assert result.route_signal is None
        assert result.state_updates.get("router.duplicate_count") == 1
        assert result.state_updates.get(StateKeys.RAW_TOOL_CALLS) is None
        hints = [m for m in base_state["messages"] if "连续" in m.get("content", "")]
        assert len(hints) == 1

    @pytest.mark.asyncio
    async def test_duplicate_count_2_injects_hint(self, ctx, base_state):
        """测试 count=2 时注入更强的软提示。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "file_read", "args": {"path": "test.jpg"}},
        ]
        base_state[StateKeys.RAW_RESULT] = None
        base_state.setdefault("messages", [])

        sig = hashlib.md5("file_read:[('path', 'test.jpg')]".encode()).hexdigest()[:8]
        base_state["router.last_tool_call"] = sig
        base_state["router.duplicate_count"] = 1

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3})
        result = await plugin.execute(ctx)

        assert result.route_signal is None
        assert result.state_updates.get("router.duplicate_count") == 2
        hints = [m for m in base_state["messages"] if "立即停止" in m.get("content", "")]
        assert len(hints) == 1


# ── 第二级：拦截 + 路由回 LLM ──


class TestIntercept:
    """验证重复达到阈值时拦截并路由回 LLM。"""

    @pytest.mark.asyncio
    async def test_duplicate_at_max_intercepts(self, ctx, base_state):
        """测试 count=max 时拦截，移除调用，路由到 next_llm。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "file_read", "args": {"path": "test.jpg"}},
        ]
        base_state[StateKeys.RAW_RESULT] = None
        base_state.setdefault("messages", [])

        sig = hashlib.md5("file_read:[('path', 'test.jpg')]".encode()).hexdigest()[:8]
        base_state["router.last_tool_call"] = sig
        base_state["router.duplicate_count"] = 2

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "hard_limit_intercepts": 4})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates[StateKeys.RAW_TOOL_CALLS] == []
        assert result.state_updates["router.duplicate_count"] == 0
        assert result.state_updates["router.duplicate_intercepts"] == 1

    @pytest.mark.asyncio
    async def test_repetitive_output_at_max_intercepts(self, ctx, base_state):
        """测试输出重复达到阈值时清空输出，路由到 next_llm。"""
        base_state[StateKeys.RAW_RESULT] = "重复的回复"
        base_state["router.last_response"] = hashlib.md5("重复的回复"[:500].encode()).hexdigest()[:8]
        base_state["router.repetitive_count"] = 2
        base_state.setdefault("messages", [])

        plugin = DuplicateCheckPlugin({"max_repetitive_output": 3, "hard_limit_intercepts": 4})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates[StateKeys.RAW_RESULT] == ""
        assert result.state_updates["router.repetitive_count"] == 0
        assert result.state_updates["router.duplicate_intercepts"] == 1

    @pytest.mark.asyncio
    async def test_intercept_injects_warning_with_tool_desc(self, ctx, base_state):
        """测试拦截时注入包含工具描述的强警告。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "file_read", "args": {"path": "C:\\Users\\test.jpg"}},
        ]
        base_state[StateKeys.RAW_RESULT] = None
        base_state.setdefault("messages", [])

        sig = hashlib.md5("file_read:[('path', 'C:\\\\Users\\\\test.jpg')]".encode()).hexdigest()[:8]
        base_state["router.last_tool_call"] = sig
        base_state["router.duplicate_count"] = 2

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "hard_limit_intercepts": 4})
        result = await plugin.execute(ctx)

        warnings = [m for m in base_state["messages"] if "已跳过执行" in m.get("content", "")]
        assert len(warnings) == 1
        assert "file_read" in warnings[0]["content"]


# ── 第三级：终止管道 ──


class TestTerminate:
    """验证拦截次数达到硬上限时终止管道。"""

    @pytest.mark.asyncio
    async def test_main_agent_terminate_notifies_user(self, ctx, base_state):
        """测试主 agent 终止时注入用户通知。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "file_read", "args": {"path": "test.jpg"}},
        ]
        base_state[StateKeys.RAW_RESULT] = None
        base_state.setdefault("messages", [])
        base_state[StateKeys.AGENT_LEVEL] = "L1"

        sig = hashlib.md5("file_read:[('path', 'test.jpg')]".encode()).hexdigest()[:8]
        base_state["router.last_tool_call"] = sig
        base_state["router.duplicate_count"] = 2
        base_state["router.duplicate_intercepts"] = 4

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "hard_limit_intercepts": 4})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        user_msgs = [m for m in base_state["messages"] if m.get("role") == "assistant" and "死循环" in m.get("content", "")]
        assert len(user_msgs) == 1

    @pytest.mark.asyncio
    async def test_sub_agent_terminate_no_notification(self, ctx, base_state):
        """测试子 agent 终止时不注入用户通知。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "file_read", "args": {"path": "test.jpg"}},
        ]
        base_state[StateKeys.RAW_RESULT] = None
        base_state.setdefault("messages", [])
        base_state[StateKeys.AGENT_LEVEL] = "L2"
        base_state["delegate_depth"] = 1

        sig = hashlib.md5("file_read:[('path', 'test.jpg')]".encode()).hexdigest()[:8]
        base_state["router.last_tool_call"] = sig
        base_state["router.duplicate_count"] = 2
        base_state["router.duplicate_intercepts"] = 4

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "hard_limit_intercepts": 4})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"
        user_msgs = [m for m in base_state["messages"] if m.get("role") == "assistant"]
        assert len(user_msgs) == 0

    @pytest.mark.asyncio
    async def test_below_hard_limit_does_not_terminate(self, ctx, base_state):
        """测试拦截次数未达硬上限时不终止。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "file_read", "args": {"path": "test.jpg"}},
        ]
        base_state[StateKeys.RAW_RESULT] = None
        base_state.setdefault("messages", [])

        sig = hashlib.md5("file_read:[('path', 'test.jpg')]".encode()).hexdigest()[:8]
        base_state["router.last_tool_call"] = sig
        base_state["router.duplicate_count"] = 2
        base_state["router.duplicate_intercepts"] = 3

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "hard_limit_intercepts": 4})
        result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "next_llm"
        assert result.state_updates["router.duplicate_intercepts"] == 4


# ── 无重复时的行为 ──


class TestNoDuplicate:
    """验证无重复时不产出路由信号。"""

    @pytest.mark.asyncio
    async def test_no_duplicate_no_signal(self, ctx, base_state):
        """测试无重复时不产出路由信号。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "file_read", "args": {"path": "a.py"}},
        ]
        base_state[StateKeys.RAW_RESULT] = "第一次回复"
        plugin = DuplicateCheckPlugin()
        result = await plugin.execute(ctx)

        assert result.route_signal is None

    @pytest.mark.asyncio
    async def test_different_tool_calls_resets_count(self, ctx, base_state):
        """测试不同工具调用重置重复计数。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "file_read", "args": {"path": "new_file.py"}},
        ]
        base_state[StateKeys.RAW_RESULT] = None
        base_state["router.duplicate_count"] = 5

        plugin = DuplicateCheckPlugin()
        result = await plugin.execute(ctx)

        assert result.route_signal is None
        assert result.state_updates.get("router.duplicate_count") == 0


# ── 签名区分 ──


class TestSignatureFix:
    """验证签名正确区分不同参数的工具调用。"""

    @pytest.mark.asyncio
    async def test_different_args_not_duplicate(self, ctx, base_state):
        """测试不同参数的工具调用不应被判为重复。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "bash_execute", "arguments": '{"command":"grep -n Application file.py","timeout":10}'},
        ]
        base_state[StateKeys.RAW_RESULT] = None
        base_state["router.last_tool_call"] = ""
        plugin = DuplicateCheckPlugin()
        result = await plugin.execute(ctx)
        assert result.route_signal is None
        assert result.state_updates.get("router.duplicate_count", 0) == 0

    @pytest.mark.asyncio
    async def test_same_args_arguments_key_is_duplicate(self, ctx, base_state):
        """测试相同参数（arguments key）应被判为重复。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "bash_execute", "arguments": '{"command":"sed -n 100,200p file.py","timeout":10}'},
        ]
        base_state[StateKeys.RAW_RESULT] = None
        base_state.setdefault("messages", [])

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 2, "hard_limit_intercepts": 4})
        r1 = await plugin.execute(ctx)
        base_state.update(r1.state_updates)

        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "bash_execute", "arguments": '{"command":"sed -n 100,200p file.py","timeout":10}'},
        ]
        r2 = await plugin.execute(ctx)
        base_state.update(r2.state_updates)
        assert base_state.get("router.duplicate_count", 0) == 1
        assert r2.route_signal is None

    @pytest.mark.asyncio
    async def test_different_commands_not_duplicate(self, ctx, base_state):
        """测试不同命令（arguments key）连续调用不应累积重复计数。"""
        commands = [
            '{"command":"grep -n Application stream_handler.py","timeout":10}',
            '{"command":"sed -n 155,175p stream_handler.py","timeout":10}',
            '{"command":"sed -n 175,250p stream_handler.py","timeout":10}',
        ]
        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "max_repetitive_output": 3})
        for cmd_args in commands:
            base_state[StateKeys.RAW_TOOL_CALLS] = [
                {"name": "bash_execute", "arguments": cmd_args},
            ]
            base_state[StateKeys.RAW_RESULT] = None
            result = await plugin.execute(ctx)
            base_state.update(result.state_updates)
        assert result.state_updates.get("router.duplicate_count", 0) == 0


# ── 工具描述构建 ──


class TestToolCallDescription:
    """验证工具调用描述构建。"""

    @pytest.mark.asyncio
    async def test_build_description_with_args(self, ctx, base_state):
        """测试构建包含参数的工具调用描述。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "file_read", "args": {"path": "test.jpg"}},
        ]
        base_state[StateKeys.RAW_RESULT] = None
        base_state.setdefault("messages", [])

        sig = hashlib.md5("file_read:[('path', 'test.jpg')]".encode()).hexdigest()[:8]
        base_state["router.last_tool_call"] = sig

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "hard_limit_intercepts": 4})
        await plugin.execute(ctx)

        hints = [m for m in base_state["messages"] if "DuplicateCheck" in m.get("content", "")]
        assert len(hints) == 1
        assert "file_read(path=test.jpg)" in hints[0]["content"]
