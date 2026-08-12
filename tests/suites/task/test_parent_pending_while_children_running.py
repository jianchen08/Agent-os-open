import tests._tasks_path  # noqa: F401  注入 tasks 插件目录到 sys.path

"""复现测试：父任务等待子任务执行时却处于 pending 状态。

根因分析：
  系统重启或 TaskWorker.stop() 时，running 的父任务会被 reset_to_pending。
  但此时子任务可能仍在 running/evaluating 状态（子任务是独立的 asyncio task）。
  恢复时父任务从 pending 重新开始执行，而不是恢复到等待子任务的状态，
  导致看起来"任务在等待子任务但状态却是 pending"。

0.2 迁移：tasks.types → tasks.task_types；其余 tasks 子模块平铺 import。
依赖 infrastructure.task_worker / infrastructure.task_recovery / plugins.output.*
的集成级用例已删除（0.2 无对应模块）；保留纯状态机/模型层面的单元用例。
"""

from datetime import datetime

import pytest

from state_machine import SimpleStateMachine, get_task_state_machine
from task_types import TaskStatus, TaskModel, create_task

pytestmark = pytest.mark.unit


def _make_task(
    task_id: str = "parent-001",
    title: str = "父任务",
    status: TaskStatus = TaskStatus.PENDING,
    parent_task_id: str | None = None,
    parent_pipeline_id: str | None = None,
    **extra_meta,
) -> TaskModel:
    """创建测试用任务模型。"""
    task = create_task(
        title=title,
        description="测试任务",
        parent_task_id=parent_task_id,
        parent_pipeline_id=parent_pipeline_id,
    )
    task.id = task_id
    task.status = status
    if extra_meta:
        task.metadata.update(extra_meta)
    return task


def _make_child(
    child_id: str = "child-001",
    parent_id: str = "parent-001",
    parent_pipeline_id: str = "pipe-parent",
    status: TaskStatus = TaskStatus.RUNNING,
) -> TaskModel:
    """创建子任务。"""
    child = create_task(
        title="子任务",
        description="测试子任务",
        parent_task_id=parent_id,
        parent_pipeline_id=parent_pipeline_id,
    )
    child.id = child_id
    child.status = status
    return child


# ── 1. 基准：正常流程 ──


class TestBaselineNormalFlow:
    """验证正常流程中父任务的状态变化。"""

    def test_parent_is_running_after_start(self):
        """正常流程：父任务创建子任务后应为 running。"""
        parent = _make_task()
        assert parent.status == TaskStatus.PENDING

        parent.status = TaskStatus.RUNNING
        parent.started_at = datetime.now().isoformat()

        child = _make_child(parent_id=parent.id)

        assert parent.status == TaskStatus.RUNNING


# ── 关键场景：父任务 pending 但有子任务在跑（纯模型构造，不依赖 worker）──


class TestParentPendingWithActiveChildren:
    """测试父任务 pending 但子任务正在执行的场景。

    这是最直接复现用户看到的现象：
    任务在等待下级子任务执行但却是 pending 状态。
    """

    def test_parent_pending_child_running(self):
        """直接构造：父任务 pending，子任务 running。"""
        parent = _make_task(task_id="parent-001", status=TaskStatus.PENDING)
        child = _make_child(
            child_id="child-001",
            parent_id="parent-001",
            status=TaskStatus.RUNNING,
        )

        assert parent.status == TaskStatus.PENDING
        assert child.status == TaskStatus.RUNNING
        assert child.parent_task_id == parent.id

    def test_parent_pending_child_evaluating(self):
        """直接构造：父任务 pending，子任务 evaluating。"""
        parent = _make_task(task_id="parent-001", status=TaskStatus.PENDING)
        child = _make_child(
            child_id="child-001",
            parent_id="parent-001",
            status=TaskStatus.EVALUATING,
        )

        assert parent.status == TaskStatus.PENDING
        assert child.status == TaskStatus.EVALUATING


# ── 状态机层面的恢复语义验证（不依赖 infrastructure 层）──


class TestStateMachineRecoverySemantics:
    """验证任务状态机的恢复相关转移规则（0.2：PAUSED 已并入 STOPPED）。

    0.2 状态机把恢复路径建模为 STOPPED → PENDING / TIMEOUT → PENDING
    （原先 0.1 的 PAUSED 概念合并进 STOPPED）。这里锁定这些转移仍然合法，
    确保恢复流程（kernel 侧）依赖的状态机契约不被破坏。
    """

    def test_stopped_can_transition_to_pending(self):
        """状态机应允许 STOPPED → PENDING（resume/恢复路径所需）。"""
        sm = get_task_state_machine()
        # 推进到 stopped：pending → running → stopped
        sm.transition(TaskStatus.RUNNING.value)
        sm.transition(TaskStatus.STOPPED.value)
        sm.transition(TaskStatus.PENDING.value)
        assert sm.current_state == TaskStatus.PENDING.value

    def test_timeout_can_transition_to_pending(self):
        """状态机应允许 TIMEOUT → PENDING（超时后恢复路径所需）。"""
        sm = get_task_state_machine()
        sm.transition(TaskStatus.RUNNING.value)
        sm.transition(TaskStatus.TIMEOUT.value)
        sm.transition(TaskStatus.PENDING.value)
        assert sm.current_state == TaskStatus.PENDING.value


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
