"""Agent 配置守护集成测试。

验证三层防御体系：
1. task_submit: 提交时校验目标 Agent 存在性 + 级别匹配
2. TaskWorker: 执行前校验 agent_config 非空，找不到直接 fail_task
3. PipelineEngine: allow_default_fallback=False 时拒绝静默回退

覆盖场景:
- 目标 Agent 不存在 → task_submit 拒绝
- 目标 Agent 为 L1 → task_submit 拒绝
- 目标 Agent 级别不兼容 → task_submit 拒绝
- TaskWorker target_id 为空 → fail_task
- TaskWorker agent_registry 找不到 → fail_task
- PipelineEngine allow_default_fallback=False + agent_config=None → ValueError
- PipelineEngine allow_default_fallback=True + agent_config=None → 正常回退
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from tools.builtin.task_submit import TaskSubmitTool
from infrastructure.task_context import TaskExecutionContext


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def tool():
    """创建 TaskSubmitTool 实例。"""
    return TaskSubmitTool()


@pytest.fixture
def task_worker():
    """创建 TaskWorker 实例（不启动）。"""
    from infrastructure.task_worker import TaskWorker

    task_service = MagicMock()
    task_service.fail_task = AsyncMock()
    task_service.get_task = MagicMock(return_value=None)
    return TaskWorker(
        task_service=task_service,
        plugin_registry=MagicMock(),
        input_route_table=MagicMock(),
        output_route_table=MagicMock(),
        services={"task_service": task_service},
        event_bus=MagicMock(),
    )


# ---------------------------------------------------------------------------
# 1. task_submit: _validate_target_agent 校验
# ---------------------------------------------------------------------------


class TestValidateTargetAgent:
    """测试 task_submit 提交时对目标 Agent 的存在性和级别校验。"""

    def test_rejects_nonexistent_agent(self, tool):
        """目标 Agent 不存在时校验失败。"""
        ok, msg, code = tool._validate_target_agent(
            "nonexistent_agent_xyz_999", parent_agent_level=1,
        )
        assert ok is False
        assert code == "TARGET_AGENT_NOT_FOUND"
        assert "不存在" in msg

    def test_rejects_l1_agent_as_target(self, tool):
        """L1 Agent（灵汐）不能作为子任务执行者。"""
        ok, msg, code = tool._validate_target_agent(
            "lingxi", parent_agent_level=1,
        )
        assert ok is False
        assert code == "TARGET_AGENT_IS_L1"
        assert "L1" in msg

    def test_rejects_same_level_agent(self, tool):
        """目标 Agent 级别与提交者相同时校验失败。

        模拟 L2 Agent 提交给同级别 L2 Agent，应被拒绝。
        programming_orchestrator_agent 是 L2 级别。
        """
        ok, msg, code = tool._validate_target_agent(
            "programming_orchestrator_agent", parent_agent_level=2,
        )
        assert ok is False
        assert code == "TARGET_AGENT_LEVEL_INVALID"
        assert "L2" in msg

    def test_rejects_higher_level_agent(self, tool):
        """目标 Agent 级别高于提交者时校验失败。

        模拟 L3 Agent 试图提交给 L2 Agent，应被拒绝。
        """
        ok, msg, code = tool._validate_target_agent(
            "programming_orchestrator_agent", parent_agent_level=3,
        )
        assert ok is False
        assert code == "TARGET_AGENT_LEVEL_INVALID"

    def test_accepts_valid_l2_target_from_l1(self, tool):
        """L1 提交给 L2 Agent 应通过校验。

        programming_orchestrator_agent 是 L2 级别。
        """
        ok, msg, code = tool._validate_target_agent(
            "programming_orchestrator_agent", parent_agent_level=1,
        )
        assert ok is True
        assert msg == ""
        assert code == ""

    def test_accepts_valid_l3_target_from_l1(self, tool):
        """L1 提交给 L3 Agent 应通过校验。

        general_agent 是 L3 级别。
        """
        ok, msg, code = tool._validate_target_agent(
            "general_agent", parent_agent_level=1,
        )
        assert ok is True

    def test_accepts_valid_l3_target_from_l2(self, tool):
        """L2 提交给 L3 Agent 应通过校验。"""
        ok, msg, code = tool._validate_target_agent(
            "function_verifier_agent", parent_agent_level=2,
        )
        assert ok is True


# ---------------------------------------------------------------------------
# 2. task_submit: execute() 中校验集成
# ---------------------------------------------------------------------------


class TestTaskSubmitAgentValidation:
    """测试 execute() 方法中目标 Agent 校验的集成效果。"""

    @pytest.mark.asyncio
    async def test_execute_rejects_nonexistent_target(self, tool):
        """execute() 对不存在的 target_id 返回错误。"""
        result = await tool.execute({
            "goal": {"title": "测试任务"},
            "target_type": "agent",
            "target_id": "nonexistent_agent_xyz_999",
            "acceptance_criteria": {"file_check": {"input_params": {"path": "test.md"}}},
            "task_scope": "non_container",
            "parent_agent_level": 1,
        })
        assert result.success is False
        assert "不存在" in result.error or "TARGET_AGENT_NOT_FOUND" in str(result.to_dict())

    @pytest.mark.asyncio
    async def test_execute_rejects_l1_target(self, tool):
        """execute() 对 L1 Agent 作为 target_id 返回错误。"""
        result = await tool.execute({
            "goal": {"title": "测试任务"},
            "target_type": "agent",
            "target_id": "lingxi",
            "acceptance_criteria": {"file_check": {"input_params": {"path": "test.md"}}},
            "task_scope": "non_container",
            "parent_agent_level": 1,
        })
        assert result.success is False
        assert "L1" in result.error or "TARGET_AGENT_IS_L1" in str(result.to_dict())


# ---------------------------------------------------------------------------
# 3. TaskWorker: agent_config 加载失败处理
# ---------------------------------------------------------------------------


class TestTaskWorkerAgentConfigGuard:
    """测试 TaskWorker 对 agent_config 缺失的防御。"""

    async def test_fails_task_when_target_id_empty(self, task_worker):
        """target_id 为空时直接 fail_task。"""
        task_data = {
            "task_id": "task_empty_target",
            "target_id": "",
            "user_input": "测试",
        }

        mock_task = MagicMock()
        mock_task.metadata = {"task_scope": "non_container"}
        task_worker._task_service.get_task.return_value = mock_task

        await task_worker._execute_background_task(task_data, TaskExecutionContext("task_empty_target"))

        task_worker._task_service.fail_task.assert_called_once()
        call_args = task_worker._task_service.fail_task.call_args
        assert call_args[0][0] == "task_empty_target"
        assert "target_id" in call_args[0][1]

    async def test_fails_task_when_agent_not_in_registry(self, task_worker):
        """agent_registry 中找不到 target_id 时直接 fail_task。"""
        task_data = {
            "task_id": "task_missing_agent",
            "target_id": "nonexistent_agent_xyz_999",
            "user_input": "测试",
        }

        mock_task = MagicMock()
        mock_task.metadata = {"task_scope": "non_container"}
        task_worker._task_service.get_task.return_value = mock_task

        mock_registry = MagicMock()
        mock_registry.get.return_value = None
        task_worker._services["agent_registry"] = mock_registry

        await task_worker._execute_background_task(task_data, TaskExecutionContext("task_missing_agent"))

        task_worker._task_service.fail_task.assert_called_once()
        call_args = task_worker._task_service.fail_task.call_args
        assert call_args[0][0] == "task_missing_agent"
        assert "未在系统中注册" in call_args[0][1]


# ---------------------------------------------------------------------------
# 4. PipelineEngine: allow_default_fallback 校验
# ---------------------------------------------------------------------------


class TestPipelineEngineFallbackGuard:
    """测试 PipelineEngine 的 allow_default_fallback 参数。"""

    async def test_rejects_none_agent_config_when_fallback_disabled(self):
        """allow_default_fallback=False + agent_config=None → ValueError。"""
        from pipeline.engine import PipelineEngine

        mock_input_route = MagicMock()
        mock_input_route.resolve = MagicMock(return_value=([], "core"))
        mock_output_route = MagicMock()
        mock_output_route.arbitrate = MagicMock(
            return_value=MagicMock(route_type="end", reason="test")
        )
        mock_registry = MagicMock()
        mock_registry.get_core = MagicMock(return_value=None)
        mock_registry.get_output_plugins = MagicMock(return_value=[])
        mock_registry.get = MagicMock(return_value=None)

        engine = PipelineEngine(
            input_route_table=mock_input_route,
            output_route_table=mock_output_route,
            plugin_registry=mock_registry,
            max_iterations=1,
        )

        # engine.py 已全面 fail-closed：错误消息为"禁止静默回退到默认 Agent"
        with pytest.raises(ValueError, match="禁止静默回退到默认 Agent"):
            await engine.run(
                user_input="测试",
                agent_config=None,
                allow_default_fallback=False,
            )

    async def test_allows_none_agent_config_when_fallback_enabled(self):
        """全面禁止降级：即使 allow_default_fallback=True 也必须拒绝 None。

        engine.py 不再认 allow_default_fallback=True 作为放行降级的开关，
        agent_config=None 一律抛 ValueError。此用例锁定该行为（原"允许回退"
        语义已被 P0-安全 禁止静默降级策略覆盖）。
        """
        from pipeline.engine import PipelineEngine

        mock_input_route = MagicMock()
        mock_input_route.resolve = MagicMock(return_value=([], "core"))
        mock_output_route = MagicMock()
        mock_output_route.arbitrate = MagicMock(
            return_value=MagicMock(route_type="end", reason="test")
        )
        mock_registry = MagicMock()
        mock_registry.get_core = MagicMock(return_value=None)
        mock_registry.get_output_plugins = MagicMock(return_value=[])
        mock_registry.get = MagicMock(return_value=None)

        engine = PipelineEngine(
            input_route_table=mock_input_route,
            output_route_table=mock_output_route,
            plugin_registry=mock_registry,
            max_iterations=1,
        )

        with pytest.raises(ValueError, match="禁止静默回退到默认 Agent"):
            await engine.run(
                user_input="测试",
                agent_config=None,
                allow_default_fallback=True,
            )
