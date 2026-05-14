"""BUG-FIX-fix_20260514 测试: 父管道不应因 duplicate_check 强制结束。

验证 DuplicateCheckPlugin 在检测到重复超过阈值时，
对有活跃触发器或活跃子任务的父管道不发出 end 信号，
而是重置计数器并注入警告消息。
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from pipeline.plugin import PluginContext
from pipeline.types import StateKeys, create_initial_state
from plugins.output.duplicate_check import DuplicateCheckPlugin


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


# ── 复现测试: 原有行为不变 ──


class TestDuplicateCheckExistingBehavior:
    """验证原有行为在无活跃后台工作时不变。"""

    @pytest.mark.asyncio
    async def test_excessive_duplicate_triggers_end(self, ctx, base_state):
        """测试无活跃后台工作时超限仍触发 end 信号。"""
        base_state["router.duplicate_count"] = 5
        base_state[StateKeys.RAW_TOOL_CALLS] = []
        base_state[StateKeys.RAW_RESULT] = "some response"

        # 设置与当前相同的签名
        current_hash = hashlib.md5("some response"[:500].encode()).hexdigest()[:8]
        base_state["router.last_response"] = current_hash

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "max_repetitive_output": 3})
        result = await plugin.execute(ctx)

        # 应触发 end
        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_no_duplicate_no_signal(self, ctx, base_state):
        """测试无重复时不产出路由信号。"""
        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "read_file", "args": {"path": "a.py"}},
        ]
        base_state[StateKeys.RAW_RESULT] = "第一次回复"
        plugin = DuplicateCheckPlugin()
        result = await plugin.execute(ctx)

        assert result.route_signal is None


# ── 修复验证: 有活跃触发器的父管道 ──


class TestDuplicateCheckParentPipelineFix:
    """BUG-FIX-fix_20260514: 父管道不应因 duplicate_check 强制结束。"""

    @pytest.mark.asyncio
    async def test_active_triggers_no_end(self, ctx, base_state):
        """测试有活跃触发器的父管道超限时注入警告而非结束。"""
        # 构造超限场景
        base_state["router.duplicate_count"] = 5
        base_state[StateKeys.PIPELINE_ID] = "test-parent-pipeline"
        base_state[StateKeys.RAW_TOOL_CALLS] = []
        base_state[StateKeys.RAW_RESULT] = "some response"
        base_state.setdefault("messages", [])

        # Mock TriggerManager 返回活跃触发器
        mock_trigger = MagicMock()
        mock_trigger.pipeline_id = "test-parent-pipeline"
        mock_trigger.status.value = "active"

        mock_tm = MagicMock()
        mock_tm._triggers = {"trigger_1": mock_trigger}

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "max_repetitive_output": 3})

        with patch("triggers.manager.get_trigger_manager", return_value=mock_tm):
            result = await plugin.execute(ctx)

        # 不应产出 end 信号
        assert result.route_signal is None
        # 计数器应被重置
        assert result.state_updates["router.duplicate_count"] == 0
        assert result.state_updates["router.repetitive_count"] == 0
        # messages 中应注入了警告
        warning_msgs = [m for m in base_state["messages"] if "系统警告" in m.get("content", "")]
        assert len(warning_msgs) == 1
        assert "重复调用相同工具" in warning_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_submitted_tasks_no_end(self, ctx, base_state):
        """测试有活跃子任务的父管道超限时注入警告而非结束。"""
        # 构造超限场景（repetitive output）
        base_state[StateKeys.RAW_RESULT] = "重复的回复"
        base_state["router.last_response"] = hashlib.md5("重复的回复"[:500].encode()).hexdigest()[:8]
        base_state["router.repetitive_count"] = 5
        base_state[StateKeys.PIPELINE_ID] = "test-parent-pipeline"
        base_state["submitted_task_ids"] = ["task_001", "task_002"]
        base_state.setdefault("messages", [])

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "max_repetitive_output": 3})
        result = await plugin.execute(ctx)

        # 不应产出 end 信号
        assert result.route_signal is None
        # 计数器应被重置
        assert result.state_updates["router.repetitive_count"] == 0
        # messages 中应注入了警告
        warning_msgs = [m for m in base_state["messages"] if "系统警告" in m.get("content", "")]
        assert len(warning_msgs) == 1
        assert "重复输出相似内容" in warning_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_no_background_work_still_ends(self, ctx, base_state):
        """测试无活跃后台工作的普通管道超限时仍正常结束。"""
        base_state["router.duplicate_count"] = 5
        base_state[StateKeys.PIPELINE_ID] = "test-normal-pipeline"
        base_state[StateKeys.RAW_TOOL_CALLS] = []
        base_state[StateKeys.RAW_RESULT] = "some response"

        mock_tm = MagicMock()
        mock_tm._triggers = {}

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "max_repetitive_output": 3})

        with patch("triggers.manager.get_trigger_manager", return_value=mock_tm):
            result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_trigger_different_pipeline_still_ends(self, ctx, base_state):
        """测试活跃触发器属于其他管道时，当前管道仍正常结束。"""
        base_state["router.duplicate_count"] = 5
        base_state[StateKeys.PIPELINE_ID] = "test-pipeline-A"
        base_state[StateKeys.RAW_TOOL_CALLS] = []
        base_state[StateKeys.RAW_RESULT] = "some response"

        mock_trigger = MagicMock()
        mock_trigger.pipeline_id = "other-pipeline-B"
        mock_trigger.status.value = "active"

        mock_tm = MagicMock()
        mock_tm._triggers = {"trigger_1": mock_trigger}

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "max_repetitive_output": 3})

        with patch("triggers.manager.get_trigger_manager", return_value=mock_tm):
            result = await plugin.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "end"

    @pytest.mark.asyncio
    async def test_soft_reset_counter_values(self, ctx, base_state):
        """测试软重置时两个计数器都被正确重置为 0。"""
        base_state["router.duplicate_count"] = 10
        base_state["router.repetitive_count"] = 7
        base_state[StateKeys.PIPELINE_ID] = "test-pipeline"
        base_state[StateKeys.RAW_TOOL_CALLS] = []
        base_state[StateKeys.RAW_RESULT] = "some response"
        base_state["submitted_task_ids"] = ["task_001"]
        base_state.setdefault("messages", [])

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "max_repetitive_output": 3})
        result = await plugin.execute(ctx)

        assert result.state_updates["router.duplicate_count"] == 0
        assert result.state_updates["router.repetitive_count"] == 0

    @pytest.mark.asyncio
    async def test_repetitive_output_with_triggers_no_end(self, ctx, base_state):
        """测试有活跃触发器时重复输出超限也不结束。"""
        base_state[StateKeys.RAW_RESULT] = "重复的回复"
        base_state["router.last_response"] = hashlib.md5("重复的回复"[:500].encode()).hexdigest()[:8]
        base_state["router.repetitive_count"] = 5
        base_state[StateKeys.PIPELINE_ID] = "test-parent-pipeline"
        base_state.setdefault("messages", [])

        mock_trigger = MagicMock()
        mock_trigger.pipeline_id = "test-parent-pipeline"
        mock_trigger.status.value = "active"

        mock_tm = MagicMock()
        mock_tm._triggers = {"trigger_1": mock_trigger}

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 3, "max_repetitive_output": 3})

        with patch("triggers.manager.get_trigger_manager", return_value=mock_tm):
            result = await plugin.execute(ctx)

        # 不应产出 end 信号
        assert result.route_signal is None
        # 计数器应被重置
        assert result.state_updates["router.repetitive_count"] == 0
        assert result.state_updates["router.duplicate_count"] == 0


class TestDuplicateCheckSignatureFix:
    """BUG-FIX-fix_20260514: 签名应正确区分不同参数的工具调用。"""

    @pytest.mark.asyncio
    async def test_different_args_not_duplicate(self, ctx, base_state):
        """测试不同参数的 bash_execute 不应被判为重复（arguments key）。"""
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

        plugin = DuplicateCheckPlugin({"max_duplicate_calls": 1, "max_repetitive_output": 3})
        r1 = await plugin.execute(ctx)
        base_state.update(r1.state_updates)

        base_state[StateKeys.RAW_TOOL_CALLS] = [
            {"name": "bash_execute", "arguments": '{"command":"sed -n 100,200p file.py","timeout":10}'},
        ]
        r2 = await plugin.execute(ctx)
        base_state.update(r2.state_updates)
        assert base_state.get("router.duplicate_count", 0) == 1

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
        assert result.state_updates.get("router.duplicate_count", 0) == 0
