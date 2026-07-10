"""task_notifier._on_task_state_changed stopped 分流回归测试。

BUG-FIX-fix_20260702_engine_not_stopped_on_cancel:
任务级联取消（cancel_task）emit "stopped"，但 _TERMINAL_STATES 不含 stopped、
cancel_pipeline 触发条件判 ("cancelled","failed") 也不含 stopped →
任务 stopped 后管道引擎继续空转（对上游反复 timeout 重试）。

修复：
1. _TERMINAL_STATES 加入 "stopped"
2. cancel_pipeline 触发条件区分：cancel-stopped（无 paused_by）停引擎；
   pause-stopped（有 paused_by）保留引擎待 resume_task 唤醒
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from infrastructure.task_notifier import TaskNotifierMixin
from tasks.types import TaskStatus


def _make_worker(
    *,
    task_metadata: dict | None = None,
    task_status: TaskStatus = TaskStatus.STOPPED,
) -> TaskNotifierMixin:
    """构造最小化 TaskNotifierMixin 实例，mock 掉协作依赖。"""
    worker = TaskNotifierMixin()
    worker._contexts = {}
    worker._task_service = MagicMock()

    task = MagicMock()
    task.status = task_status
    task.metadata = task_metadata or {}
    task.error = ""
    worker._task_service.get_task.return_value = task

    # cancel_pipeline 在 TaskExecutorMixin，测试中直接 mock 掉
    worker.cancel_pipeline = MagicMock(return_value=True)
    # _check_stale_containers / _notify_suspended_pipelines 是 async，mock 成 no-op
    worker._check_stale_containers = AsyncMock()
    worker._notify_suspended_pipelines = AsyncMock()
    return worker


class TestStoppedTerminalRouting:
    """stopped 任务进入 _on_task_state_changed 的终态分流。"""

    @pytest.mark.asyncio
    async def test_cancel_stopped_triggers_cancel_pipeline(self) -> None:
        """修复 C 核心：cancel_task 产生的 stopped（有 cancel_reason，无 paused_by）
        必须触发 cancel_pipeline 停止引擎。"""
        worker = _make_worker(
            task_metadata={"cancel_reason": "父任务失败，级联取消"},
        )

        await worker._on_task_state_changed("task-cancel", "running", "stopped")

        worker.cancel_pipeline.assert_called_once_with("task-cancel")

    @pytest.mark.asyncio
    async def test_stopped_without_any_metadata_treats_as_cancel(self) -> None:
        """无 paused_by 也无 cancel_reason 的 stopped → 保守按 cancel 处理（停引擎）。
        避免漏停导致引擎空转（卡死根因）。"""
        worker = _make_worker(task_metadata={})

        await worker._on_task_state_changed("task-unknown", "running", "stopped")

        worker.cancel_pipeline.assert_called_once_with("task-unknown")

    @pytest.mark.asyncio
    async def test_pause_stopped_does_not_stop_engine(self) -> None:
        """pause_task 产生的 stopped（有 paused_by）绝不能停引擎——
        resume_task 需要保留的引擎来 wake。"""
        worker = _make_worker(
            task_metadata={"paused_by": "user"},
        )

        await worker._on_task_state_changed("task-pause", "running", "stopped")

        worker.cancel_pipeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_pause_system_stopped_does_not_stop_engine(self) -> None:
        """系统暂停（paused_by=system）同样保留引擎。"""
        worker = _make_worker(
            task_metadata={"paused_by": "system"},
        )

        await worker._on_task_state_changed("task-syspause", "running", "stopped")

        worker.cancel_pipeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_always_stops_engine(self) -> None:
        """failed 终态无条件停止引擎（不受 pause/cancel 区分影响）。"""
        worker = _make_worker(
            task_status=TaskStatus.FAILED,
            task_metadata={},
        )

        await worker._on_task_state_changed("task-fail", "running", "failed")

        worker.cancel_pipeline.assert_called_once_with("task-fail")

    @pytest.mark.asyncio
    async def test_completed_does_not_stop_engine(self) -> None:
        """completed 是正常完成，不走 cancel_pipeline（引擎正常退出）。"""
        worker = _make_worker(
            task_status=TaskStatus.COMPLETED,
            task_metadata={},
        )

        await worker._on_task_state_changed("task-done", "running", "completed")

        worker.cancel_pipeline.assert_not_called()


class TestIsCancelStopped:
    """_is_cancel_stopped 判定逻辑（pause vs cancel 区分核心）。"""

    def test_cancel_reason_without_paused_by_is_cancel(self) -> None:
        worker = _make_worker(task_metadata={"cancel_reason": "cascade"})
        assert worker._is_cancel_stopped("t") is True

    def test_paused_by_user_is_pause(self) -> None:
        worker = _make_worker(task_metadata={"paused_by": "user"})
        assert worker._is_cancel_stopped("t") is False

    def test_paused_by_system_is_pause(self) -> None:
        worker = _make_worker(task_metadata={"paused_by": "system"})
        assert worker._is_cancel_stopped("t") is False

    def test_empty_metadata_is_cancel(self) -> None:
        """无任何标记的 stopped 保守按 cancel（防漏停空转）。"""
        worker = _make_worker(task_metadata={})
        assert worker._is_cancel_stopped("t") is True

    def test_paused_by_and_cancel_reason_both_present_prefers_pause(self) -> None:
        """二者同时存在时（异常情况），优先按 pause 保留引擎（保守不破坏可恢复性）。"""
        worker = _make_worker(
            task_metadata={"paused_by": "user", "cancel_reason": "x"},
        )
        assert worker._is_cancel_stopped("t") is False


class TestTerminalStatesContainsStopped:
    """_TERMINAL_STATES 必须包含 stopped（修复 C 第一步）。"""

    def test_stopped_in_terminal_states(self) -> None:
        from infrastructure.task_notifier import _TERMINAL_STATES
        assert "stopped" in _TERMINAL_STATES

    def test_failed_in_terminal_states(self) -> None:
        from infrastructure.task_notifier import _TERMINAL_STATES
        assert "failed" in _TERMINAL_STATES

    def test_completed_in_terminal_states(self) -> None:
        from infrastructure.task_notifier import _TERMINAL_STATES
        assert "completed" in _TERMINAL_STATES
