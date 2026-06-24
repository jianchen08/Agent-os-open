"""task_manage 简化重构单元测试。

验证核心功能：
1. TaskStatus 枚举包含 6 种状态，不含旧状态 EVALUATING/SUSPENDED/CANCELLED
2. 状态转换矩阵的合法与非法转换
3. task_manage 工具 schema 的 action enum（YAML + 代码定义）
4. continue 逻辑：重试(failed→pending)、注入指令(running+message)、恢复(stopped→running)
5. stop 逻辑：running→STOPPED、pending→STOPPED

被测文件：
- src/tasks/types.py
- src/tasks/state_machine.py
- src/tools/builtin/task/tool.py
- src/tools/task_manage.yaml
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from tasks.state_machine import (
    InvalidTransitionError,
    SimpleStateMachine,
    _TASK_TRANSITIONS,
    get_task_state_machine,
)
from tasks.types import TaskModel, TaskPriority, TaskStatus
from tools.builtin.task.tool import TaskTool


# ============================================================
# 1. TaskStatus 枚举测试
# ============================================================


class TestTaskStatus:
    """验证 TaskStatus 枚举包含 6 种状态，不含旧状态。"""

    EXPECTED_NAMES = {"PENDING", "RUNNING", "STOPPED", "COMPLETED", "FAILED", "TIMEOUT"}
    OLD_NAMES = {"EVALUATING", "SUSPENDED", "CANCELLED"}

    def test_has_exactly_6_states(self):
        """TaskStatus 应恰好包含 6 种状态。"""
        assert len(TaskStatus) == 6, (
            f"期望 6 种状态，实际 {len(TaskStatus)} 种: {[s.name for s in TaskStatus]}"
        )

    def test_contains_all_expected_states(self):
        """TaskStatus 应包含全部 6 种预期状态。"""
        actual = {s.name for s in TaskStatus}
        missing = self.EXPECTED_NAMES - actual
        extra = actual - self.EXPECTED_NAMES
        assert not missing and not extra, (
            f"缺失: {missing}, 多余: {extra}"
        )

    @pytest.mark.parametrize("old_name", list(OLD_NAMES))
    def test_does_not_contain_old_state(self, old_name):
        """旧状态不应存在于 TaskStatus。"""
        assert old_name not in {s.name for s in TaskStatus}, (
            f"旧状态 {old_name} 不应存在于 TaskStatus"
        )

    def test_all_values_are_lowercase(self):
        """所有状态值应为小写字符串。"""
        for status in TaskStatus:
            assert status.value == status.value.lower(), (
                f"状态 {status.name} 的值应为小写: {status.value}"
            )


# ============================================================
# 2. 状态转换矩阵测试
# ============================================================


class TestTaskStateMachineTransitions:
    """验证任务状态机的合法与非法转换。"""

    ALL_STATES = {"pending", "running", "stopped", "completed", "failed", "timeout"}

    # 合法转换：(from_state, to_state)
    VALID_TRANSITIONS = [
        ("pending", "running"),
        ("pending", "stopped"),
        ("running", "completed"),
        ("running", "failed"),
        ("running", "stopped"),
        ("running", "timeout"),
        ("stopped", "running"),
        ("stopped", "pending"),
        ("completed", "pending"),
        ("failed", "pending"),
        ("failed", "running"),
        ("timeout", "running"),
        ("timeout", "pending"),
        ("timeout", "failed"),
    ]

    # 非法转换：(from_state, to_state)
    INVALID_TRANSITIONS = [
        ("pending", "completed"),
        ("pending", "failed"),
        ("pending", "timeout"),
        ("pending", "pending"),
        ("running", "pending"),
        ("running", "running"),
        ("completed", "running"),
        ("completed", "stopped"),
        ("completed", "failed"),
        ("stopped", "completed"),
        ("stopped", "stopped"),
        ("stopped", "failed"),
        ("stopped", "timeout"),
        ("failed", "completed"),
        ("failed", "stopped"),
        ("failed", "failed"),
        ("timeout", "completed"),
        ("timeout", "stopped"),
        ("timeout", "timeout"),
    ]

    def test_transitions_cover_all_6_states(self):
        """转换规则应覆盖全部 6 种状态作为源状态。"""
        actual = set(_TASK_TRANSITIONS.keys())
        assert actual == self.ALL_STATES, (
            f"转换规则缺失状态: {self.ALL_STATES - actual}, 多余: {actual - self.ALL_STATES}"
        )

    @pytest.mark.parametrize("from_state,to_state", VALID_TRANSITIONS)
    def test_valid_transition(self, from_state, to_state):
        """合法转换应成功执行。"""
        sm = get_task_state_machine()
        sm._current_state = from_state
        assert sm.can_transition(to_state), f"应允许 {from_state} → {to_state}"
        sm.transition(to_state)
        assert sm.current_state == to_state

    @pytest.mark.parametrize("from_state,to_state", INVALID_TRANSITIONS)
    def test_invalid_transition_raises(self, from_state, to_state):
        """非法转换应抛出 InvalidTransitionError。"""
        sm = get_task_state_machine()
        sm._current_state = from_state
        assert not sm.can_transition(to_state), f"不应允许 {from_state} → {to_state}"
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition(to_state)
        assert from_state in str(exc_info.value)
        assert to_state in str(exc_info.value)

    def test_initial_state_is_pending(self):
        """工厂函数创建的状态机初始状态应为 pending。"""
        sm = get_task_state_machine()
        assert sm.current_state == "pending"

    def test_stop_action_produces_stopped_state(self):
        """从 pending/running 执行 stop 应得到 stopped（操作名=状态名）。"""
        for from_state in ("pending", "running"):
            sm = get_task_state_machine()
            sm._current_state = from_state
            sm.transition("stopped")
            assert sm.current_state == "stopped"


# ============================================================
# 3. task_manage 工具 Schema 测试
# ============================================================


class TestTaskManageSchema:
    """验证 task_manage 工具的 action enum 定义。"""

    EXPECTED_ACTIONS = {"get", "continue", "stop", "delete", "change"}
    OLD_ACTIONS = {
        "retry", "inject", "resume", "pause", "cancel",
        "update", "resume_completed", "complete_container", "fail_container",
        "complete", "fail",
        "list", "status",
    }

    @pytest.fixture
    def yaml_schema(self) -> dict:
        """加载 YAML 工具定义文件。"""
        yaml_path = os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", "src", "tools", "task_manage.yaml",
        ))
        with open(yaml_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    @pytest.fixture
    def code_schema(self) -> dict:
        """获取代码中的工具 schema。"""
        tool_def = TaskTool.get_tool_definition()
        return tool_def.input_schema

    def test_yaml_action_enum_has_5_actions(self, yaml_schema):
        """YAML schema 的 action enum 应恰好包含 5 个操作。"""
        action_enum = yaml_schema["parameters"]["properties"]["action"]["enum"]
        assert set(action_enum) == self.EXPECTED_ACTIONS, (
            f"YAML action enum 不匹配: "
            f"缺失 {self.EXPECTED_ACTIONS - set(action_enum)}, "
            f"多余 {set(action_enum) - self.EXPECTED_ACTIONS}"
        )

    @pytest.mark.parametrize("old_action", list(OLD_ACTIONS))
    def test_yaml_action_enum_no_old_actions(self, yaml_schema, old_action):
        """YAML schema 不应包含旧 action。"""
        action_enum = yaml_schema["parameters"]["properties"]["action"]["enum"]
        assert old_action not in action_enum, (
            f"旧 action '{old_action}' 不应出现在 YAML schema"
        )

    def test_code_action_enum_matches_yaml(self, yaml_schema, code_schema):
        """代码中的 action enum 应与 YAML 定义一致。"""
        code_enum = code_schema["properties"]["action"]["enum"]
        yaml_enum = yaml_schema["parameters"]["properties"]["action"]["enum"]
        assert set(code_enum) == set(yaml_enum), (
            f"代码与 YAML action enum 不一致: "
            f"代码多 {set(code_enum) - set(yaml_enum)}, "
            f"YAML 多 {set(yaml_enum) - set(code_enum)}"
        )

    def test_code_action_is_required(self, code_schema):
        """action 应为必填参数。"""
        assert "action" in code_schema.get("required", []), (
            "action 应在 required 列表中"
        )

    def test_code_status_enum_has_6_values(self, code_schema):
        """代码中 status 参数的 enum 应包含 6 种状态。"""
        status_enum = code_schema["properties"]["status"]["enum"]
        expected = {"pending", "running", "stopped", "completed", "failed", "timeout"}
        assert set(status_enum) == expected, (
            f"status enum 不匹配: 缺失 {expected - set(status_enum)}, "
            f"多余 {set(status_enum) - expected}"
        )

    def test_code_tool_name_is_task_manage(self, code_schema):
        """工具定义的名称应为 task_manage（验证 Tool 对象的 name）。"""
        tool_def = TaskTool.get_tool_definition()
        assert tool_def.name == "task_manage"


# ============================================================
# 辅助：创建 mock TaskTool
# ============================================================


def _make_task(status: TaskStatus, **overrides) -> TaskModel:
    """创建测试用 TaskModel。"""
    defaults: dict = {
        "id": "test-task-001",
        "title": "测试任务",
        "status": status,
        "metadata": {"session_id": "sess-001"},
    }
    defaults.update(overrides)
    return TaskModel(**defaults)


def _make_tool(mock_service: MagicMock) -> TaskTool:
    """创建注入了 mock service 的 TaskTool。"""
    tool = TaskTool()
    tool._task_service = mock_service
    return tool


def _mock_service_for_task(task: TaskModel) -> MagicMock:
    """创建返回指定 task 的 mock TaskService。"""
    svc = MagicMock()
    svc.get_task.return_value = task
    svc.force_transition = AsyncMock()
    svc.save_task = AsyncMock()
    svc.resume_task = AsyncMock()
    svc.pause_task = AsyncMock()
    svc._cancel_pipeline_recursive = MagicMock()
    svc.cancel_task_cascade = AsyncMock(return_value=0)
    return svc


# ============================================================
# 4. Continue 逻辑测试
# ============================================================


class TestContinueLogic:
    """验证 continue 操作的四种行为。"""

    @pytest.mark.asyncio
    async def test_retry_failed_task_to_pending(self):
        """continue 对 failed 任务应重试（failed → pending）。"""
        task = _make_task(TaskStatus.FAILED, pipeline_run_id="pipe-001")
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        with patch("infrastructure.service_provider.get_service_provider") as mock_sp:
            mock_sp.return_value.get.return_value = None
            result = await tool._continue_task(
                {"task_id": "test-task-001", "action": "continue"},
                parent_agent_level=1,
            )

        assert result.success, f"continue 重试应成功: {result.error}"
        assert result.output["retried"] is True
        assert result.output["old_status"] == "failed"
        assert result.output["new_status"] == "pending"
        svc.force_transition.assert_called_once_with("test-task-001", TaskStatus.PENDING)

    @pytest.mark.asyncio
    async def test_retry_timeout_task_to_pending(self):
        """continue 对 timeout 任务应重试（timeout → pending）。"""
        task = _make_task(TaskStatus.TIMEOUT, pipeline_run_id="pipe-001")
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        with patch("infrastructure.service_provider.get_service_provider") as mock_sp:
            mock_sp.return_value.get.return_value = None
            result = await tool._continue_task(
                {"task_id": "test-task-001", "action": "continue"},
                parent_agent_level=1,
            )

        assert result.success
        assert result.output["retried"] is True
        assert result.output["old_status"] == "timeout"
        assert result.output["new_status"] == "pending"

    @pytest.mark.asyncio
    async def test_inject_to_running_task(self):
        """continue 对 running 任务 + message 应注入指令（不改变状态）。"""
        task = _make_task(TaskStatus.RUNNING, pipeline_run_id="pipe-001")
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        with patch("pipeline.message_bus.send_pipeline_message") as mock_send:
            mock_send_result = MagicMock()
            mock_send_result.success = True
            mock_send_result.method = "direct"
            mock_send.return_value = mock_send_result

            result = await tool._continue_task(
                {
                    "task_id": "test-task-001",
                    "action": "continue",
                    "message": "请调整方向",
                },
                parent_agent_level=1,
            )

        assert result.success
        assert result.output["injected"] is True
        assert result.output["target_pipeline_id"] == "pipe-001"
        assert result.output["message_preview"] == "请调整方向"

    @pytest.mark.asyncio
    async def test_resume_from_stopped(self):
        """continue 对 stopped 任务应恢复执行（stopped → running）。"""
        task = _make_task(TaskStatus.STOPPED)
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        result = await tool._continue_task(
            {"task_id": "test-task-001", "action": "continue"},
            parent_agent_level=1,
        )

        assert result.success
        assert result.output["resumed"] is True
        assert result.output["old_status"] == "stopped"
        assert result.output["new_status"] == "running"
        svc.resume_task.assert_called_once_with("test-task-001")

    @pytest.mark.asyncio
    async def test_continue_pending_returns_error(self):
        """continue 对 pending 任务应返回错误（不支持的状态）。"""
        task = _make_task(TaskStatus.PENDING)
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        result = await tool._continue_task(
            {"task_id": "test-task-001", "action": "continue"},
            parent_agent_level=1,
        )

        assert not result.success
        assert result.error_code == "INVALID_STATUS"

    @pytest.mark.asyncio
    async def test_continue_completed_returns_error(self):
        """continue 对 completed 任务应返回错误。"""
        task = _make_task(TaskStatus.COMPLETED)
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        result = await tool._continue_task(
            {"task_id": "test-task-001", "action": "continue"},
            parent_agent_level=1,
        )

        assert not result.success
        assert result.error_code == "INVALID_STATUS"

    @pytest.mark.asyncio
    async def test_continue_running_without_message_returns_error(self):
        """continue 对 running 任务不传 message 应返回 MISSING_MESSAGE 错误。"""
        task = _make_task(TaskStatus.RUNNING, pipeline_run_id="pipe-001")
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        result = await tool._continue_task(
            {"task_id": "test-task-001", "action": "continue"},
            parent_agent_level=1,
        )

        assert not result.success
        assert result.error_code == "MISSING_MESSAGE"

    @pytest.mark.asyncio
    async def test_continue_without_task_id_returns_error(self):
        """continue 不传 task_id 应返回 MISSING_TASK_ID 错误。"""
        tool = _make_tool(MagicMock())

        result = await tool._continue_task(
            {"action": "continue"},
            parent_agent_level=1,
        )

        assert not result.success
        assert result.error_code == "MISSING_TASK_ID"

    @pytest.mark.asyncio
    async def test_retry_with_message_stores_in_metadata(self):
        """continue 重试 + message 应将 message 存入 task.metadata['retry_message']。"""
        task = _make_task(TaskStatus.FAILED, pipeline_run_id="pipe-001", metadata={})
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        with patch("infrastructure.service_provider.get_service_provider") as mock_sp:
            mock_sp.return_value.get.return_value = None
            result = await tool._continue_task(
                {
                    "task_id": "test-task-001",
                    "action": "continue",
                    "message": "请检查文件路径",
                },
                parent_agent_level=1,
            )

        assert result.success
        assert task.metadata.get("retry_message") == "请检查文件路径"

    @pytest.mark.asyncio
    async def test_resume_with_message_stores_in_metadata(self):
        """continue 恢复 stopped + message 应将 message 存入 metadata。"""
        task = _make_task(TaskStatus.STOPPED, metadata={})
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        result = await tool._continue_task(
            {
                "task_id": "test-task-001",
                "action": "continue",
                "message": "恢复后请先检查配置",
            },
            parent_agent_level=1,
        )

        assert result.success
        assert task.metadata.get("retry_message") == "恢复后请先检查配置"
        assert result.output.get("message_injected") is True


# ============================================================
# 5. Stop 逻辑测试
# ============================================================


class TestStopLogic:
    """验证 stop 操作将任务转为 STOPPED 状态。"""

    @pytest.mark.asyncio
    async def test_stop_running_task(self):
        """stop 应将 running 任务转为 stopped。"""
        task = _make_task(TaskStatus.RUNNING)
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        result = await tool._stop_task(
            {"task_id": "test-task-001", "action": "stop"},
            parent_agent_level=1,
        )

        assert result.success
        assert result.output["stopped"] is True
        assert result.output["old_status"] == "running"
        assert result.output["new_status"] == "stopped"
        svc.pause_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_pending_task(self):
        """stop 应将 pending 任务转为 stopped。"""
        task = _make_task(TaskStatus.PENDING)
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        result = await tool._stop_task(
            {"task_id": "test-task-001", "action": "stop"},
            parent_agent_level=1,
        )

        assert result.success
        assert result.output["stopped"] is True
        assert result.output["old_status"] == "pending"
        assert result.output["new_status"] == "stopped"

    @pytest.mark.asyncio
    async def test_stop_already_stopped_returns_error(self):
        """stop 对已 stopped 的任务应返回 ALREADY_STOPPED 错误。"""
        task = _make_task(TaskStatus.STOPPED)
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        result = await tool._stop_task(
            {"task_id": "test-task-001", "action": "stop"},
            parent_agent_level=1,
        )

        assert not result.success
        assert result.error_code == "ALREADY_STOPPED"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMEOUT])
    async def test_stop_terminal_state_returns_error(self, status):
        """stop 对终态任务（completed/failed/timeout）应返回 INVALID_STATUS 错误。"""
        task = _make_task(status)
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        result = await tool._stop_task(
            {"task_id": "test-task-001", "action": "stop"},
            parent_agent_level=1,
        )

        assert not result.success
        assert result.error_code == "INVALID_STATUS"

    @pytest.mark.asyncio
    async def test_stop_without_task_id_returns_error(self):
        """stop 不传 task_id 应返回 MISSING_TASK_ID 错误。"""
        tool = _make_tool(MagicMock())

        result = await tool._stop_task(
            {"action": "stop"},
            parent_agent_level=1,
        )

        assert not result.success
        assert result.error_code == "MISSING_TASK_ID"

    @pytest.mark.asyncio
    async def test_stop_with_reason(self):
        """stop 应传递 reason 参数。"""
        task = _make_task(TaskStatus.RUNNING)
        svc = _mock_service_for_task(task)
        tool = _make_tool(svc)

        result = await tool._stop_task(
            {"task_id": "test-task-001", "action": "stop", "reason": "测试停止"},
            parent_agent_level=1,
        )

        assert result.success
        assert result.output["reason"] == "测试停止"
        # pause_task 的 paused_by 参数应包含用户填写的 reason
        call_args = svc.pause_task.call_args
        assert "测试停止" in call_args[1]["paused_by"]

    @pytest.mark.asyncio
    async def test_stop_task_not_found(self):
        """stop 不存在的任务应返回 TASK_NOT_FOUND 错误。"""
        svc = MagicMock()
        svc.get_task.return_value = None
        tool = _make_tool(svc)

        result = await tool._stop_task(
            {"task_id": "nonexistent-id", "action": "stop"},
            parent_agent_level=1,
        )

        assert not result.success
        assert result.error_code == "TASK_NOT_FOUND"
