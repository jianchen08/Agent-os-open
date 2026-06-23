"""
容器状态转换 Bug 修复回归测试。

根因回顾：
  _TASK_TRANSITIONS 中 pending 不包含 completed/failed，
  而 tool.py 要求容器必须是 PENDING 才能调 complete/fail，
  形成"前置校验放行 → 状态机拦截"的死锁。

修复内容：
  P0-1: pending 增加 completed/failed 转换目标
  P0-2: tool.py 前置校验放宽到允许 PENDING 和 RUNNING
  P2:   state_service.py 的 except ValueError → except InvalidTransitionError

回归测试目标：验证根因被修复（转换路径打通），而非仅验证症状消失。
"""

from __future__ import annotations

import pytest

from src.tasks.state_machine import (
    InvalidTransitionError,
    SimpleStateMachine,
    _TASK_TRANSITIONS,
    get_task_state_machine,
)
from tasks.types import TaskStatus


# ═══════════════════════════════════════════════════════════════════
# P0-1: 状态转换表 — pending 增加 completed/failed 路径
# ═══════════════════════════════════════════════════════════════════


class TestPendingToCompletedTransition:
    """回归：验证 PENDING→COMPLETED 路径已打通（根因修复验证）。"""

    def test_task_transitions_pending_includes_completed(self) -> None:
        """_TASK_TRANSITIONS 的 pending 应包含 completed 目标。"""
        assert "completed" in _TASK_TRANSITIONS["pending"], (
            "根因未修复: pending 转换目标不包含 completed"
        )

    def test_task_transitions_pending_includes_failed(self) -> None:
        """_TASK_TRANSITIONS 的 pending 应包含 failed 目标。"""
        assert "failed" in _TASK_TRANSITIONS["pending"], (
            "根因未修复: pending 转换目标不包含 failed"
        )

    def test_state_machine_pending_to_completed_succeeds(self) -> None:
        """状态机实例: pending → completed 应成功（原 Bug 核心路径）。"""
        sm = get_task_state_machine()
        assert sm.current_state == "pending"
        sm.transition("completed")
        assert sm.current_state == "completed"

    def test_state_machine_pending_to_failed_succeeds(self) -> None:
        """状态机实例: pending → failed 应成功（原 Bug 核心路径）。"""
        sm = get_task_state_machine()
        assert sm.current_state == "pending"
        sm.transition("failed")
        assert sm.current_state == "failed"

    def test_can_transition_pending_to_completed_returns_true(self) -> None:
        """can_transition('completed') 对 pending 应返回 True。"""
        sm = get_task_state_machine()
        assert sm.can_transition("completed") is True

    def test_can_transition_pending_to_failed_returns_true(self) -> None:
        """can_transition('failed') 对 pending 应返回 True。"""
        sm = get_task_state_machine()
        assert sm.can_transition("failed") is True


class TestRunningToCompletedFailedStillWorks:
    """回归：验证 RUNNING→COMPLETED/FAILED 路径仍然正常（未因修复被破坏）。"""

    def test_running_to_completed(self) -> None:
        """running → completed 应保持正常。"""
        sm = get_task_state_machine()
        sm.transition("running")
        sm.transition("completed")
        assert sm.current_state == "completed"

    def test_running_to_failed(self) -> None:
        """running → failed 应保持正常。"""
        sm = get_task_state_machine()
        sm.transition("running")
        sm.transition("failed")
        assert sm.current_state == "failed"


class TestStoppedCannotGoToCompletedFailed:
    """回归：验证 stopped 状态不能直接转到 completed/failed（守卫条件）。"""

    def test_stopped_to_completed_raises(self) -> None:
        """stopped → completed 应被拦截。"""
        sm = get_task_state_machine()
        sm.transition("stopped")
        with pytest.raises(InvalidTransitionError):
            sm.transition("completed")

    def test_stopped_to_failed_raises(self) -> None:
        """stopped → failed 应被拦截。"""
        sm = get_task_state_machine()
        sm.transition("stopped")
        with pytest.raises(InvalidTransitionError):
            sm.transition("failed")


# ═══════════════════════════════════════════════════════════════════
# P0-2: tool.py 前置校验逻辑验证（通过 TaskStatus 枚举值验证）
# ═══════════════════════════════════════════════════════════════════


class TestContainerPreValidation:
    """回归：验证容器操作前置校验允许 PENDING 和 RUNNING 状态。

    注意：这里验证的是逻辑组合。实际 tool.py 的完整测试需要集成测试环境，
    此处验证状态机层面的前置条件正确性。
    """

    def test_pending_status_allows_complete_transition(self) -> None:
        """PENDING 状态应允许 complete 操作对应的状态转换。"""
        sm = SimpleStateMachine(
            initial_state="pending", transitions=_TASK_TRANSITIONS
        )
        assert sm.can_transition("completed") is True, (
            "PENDING 状态应允许转换到 COMPLETED"
        )

    def test_pending_status_allows_fail_transition(self) -> None:
        """PENDING 状态应允许 fail 操作对应的状态转换。"""
        sm = SimpleStateMachine(
            initial_state="pending", transitions=_TASK_TRANSITIONS
        )
        assert sm.can_transition("failed") is True, (
            "PENDING 状态应允许转换到 FAILED"
        )

    def test_running_status_allows_complete_transition(self) -> None:
        """RUNNING 状态应允许 complete 操作对应的状态转换。"""
        sm = SimpleStateMachine(
            initial_state="running", transitions=_TASK_TRANSITIONS
        )
        assert sm.can_transition("completed") is True, (
            "RUNNING 状态应允许转换到 COMPLETED"
        )

    def test_running_status_allows_fail_transition(self) -> None:
        """RUNNING 状态应允许 fail 操作对应的状态转换。"""
        sm = SimpleStateMachine(
            initial_state="running", transitions=_TASK_TRANSITIONS
        )
        assert sm.can_transition("failed") is True, (
            "RUNNING 状态应允许转换到 FAILED"
        )

    def test_stopped_status_blocks_complete_transition(self) -> None:
        """STOPPED 状态不允许 complete 操作。"""
        sm = SimpleStateMachine(
            initial_state="stopped", transitions=_TASK_TRANSITIONS
        )
        assert sm.can_transition("completed") is False

    def test_completed_status_blocks_any_transition(self) -> None:
        """COMPLETED 状态不允许 further 操作（终态）。"""
        sm = SimpleStateMachine(
            initial_state="completed", transitions=_TASK_TRANSITIONS
        )
        assert sm.can_transition("failed") is False
        assert sm.can_transition("running") is False


# ═══════════════════════════════════════════════════════════════════
# P2: 异常类型不匹配修复验证
# ═══════════════════════════════════════════════════════════════════


class TestExceptionTypeFix:
    """回归：验证 InvalidTransitionError 是正确的异常类型。"""

    def test_state_machine_raises_invalid_transition_error(self) -> None:
        """非法转换应抛出 InvalidTransitionError（非 ValueError）。"""
        sm = get_task_state_machine()
        # pending -> completed 现在是合法的（Bug 已修复）
        sm.transition("completed")
        # completed -> running 是非法的（终态不可转出）
        with pytest.raises(InvalidTransitionError) as exc_info:
            sm.transition("running")
        assert exc_info.value.current_state == "completed"
        assert exc_info.value.target_state == "running"

    def test_invalid_transition_error_is_exception_subclass(self) -> None:
        """InvalidTransitionError 应继承自 Exception（非 ValueError）。"""
        assert issubclass(InvalidTransitionError, Exception)
        assert not issubclass(InvalidTransitionError, ValueError)

    def test_invalid_transition_error_has_attributes(self) -> None:
        """InvalidTransitionError 应包含 current_state 和 target_state 属性。"""
        err = InvalidTransitionError("pending", "completed")
        assert err.current_state == "pending"
        assert err.target_state == "completed"
        assert "pending" in str(err)
        assert "completed" in str(err)


# ═══════════════════════════════════════════════════════════════════
# 全量状态转换矩阵验证（确保修复没有引入回归）
# ═══════════════════════════════════════════════════════════════════


class TestFullTransitionMatrix:
    """全量验证修复后的状态转换矩阵，确保没有破坏现有路径。"""

    @pytest.mark.parametrize("from_s, to_s", [
        # pending 的新增路径（Bug 修复的核心）
        ("pending", "completed"),
        ("pending", "failed"),
        # pending 原有路径
        ("pending", "running"),
        ("pending", "stopped"),
        # running 路径
        ("running", "completed"),
        ("running", "failed"),
        ("running", "stopped"),
        ("running", "timeout"),
        # stopped 路径
        ("stopped", "running"),
        ("stopped", "pending"),
        # completed 路径
        ("completed", "pending"),
        # failed 路径
        ("failed", "pending"),
        ("failed", "running"),
        # timeout 路径
        ("timeout", "running"),
        ("timeout", "pending"),
        ("timeout", "failed"),
    ])
    def test_valid_transition(self, from_s: str, to_s: str) -> None:
        """合法转换应成功。"""
        sm = SimpleStateMachine(
            initial_state=from_s, transitions=_TASK_TRANSITIONS
        )
        sm.transition(to_s)
        assert sm.current_state == to_s

    @pytest.mark.parametrize("from_s, to_s", [
        # 终态不应允许非法转出
        ("completed", "running"),
        ("completed", "failed"),
        ("completed", "stopped"),
        ("completed", "timeout"),
        # stopped 不应直达终态
        ("stopped", "completed"),
        ("stopped", "failed"),
        ("stopped", "timeout"),
        # timeout 不应直达 completed
        ("timeout", "completed"),
        ("timeout", "stopped"),
    ])
    def test_invalid_transition_raises(self, from_s: str, to_s: str) -> None:
        """非法转换应抛出 InvalidTransitionError。"""
        sm = SimpleStateMachine(
            initial_state=from_s, transitions=_TASK_TRANSITIONS
        )
        with pytest.raises(InvalidTransitionError):
            sm.transition(to_s)
