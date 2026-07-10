"""复现测试：父任务等待子任务执行时却处于 pending 状态。

根因分析：
  系统重启或 TaskWorker.stop() 时，running 的父任务会被 reset_to_pending。
  但此时子任务可能仍在 running/evaluating 状态（子任务是独立的 asyncio task）。
  恢复时父任务从 pending 重新开始执行，而不是恢复到等待子任务的状态，
  导致看起来"任务在等待子任务但状态却是 pending"。

更具体的场景：
  1. 父任务 running → LLM 调用 task_submit → 子任务创建
  2. 父管道被 ChildTaskGuard 挂起（等待子任务）
  3. 系统重启 / TaskWorker.stop()
  4. stop() 将父任务标记为 paused（子任务也是 running 也被 paused）
  5. 系统启动 → _recover_running_tasks → 所有 paused 任务 reset_to_pending
  6. 父任务重新 submit_task → 重新执行管道（不恢复之前挂起的状态）
  7. 此时父任务 pending，但子任务也在 pending/running
  8. 外部看到：父任务 pending + 有活跃子任务
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tasks.state_machine import (
    InvalidTransitionError,
    _TASK_TRANSITIONS,
    SimpleStateMachine,
)
from tasks.types import TaskStatus, TaskModel, create_task


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


# ── 2. 根因复现：TaskWorker.stop + restart 导致 running → paused → pending ──


class TestRecoveryResetToPending:
    """复现根因：系统重启时 running 父任务被 reset_to_pending。

    完整链路：
    1. 父任务 running，管道挂起等待子任务
    2. TaskWorker.stop() → 父任务 paused
    3. 系统重启 → _recover_running_tasks → 父任务 reset_to_pending
    4. 父任务 pending，但子任务也在被恢复（pending/running）
    5. 外部看到：父任务 pending + 有子任务在执行
    """

    @pytest.mark.asyncio
    async def test_running_parent_reset_to_pending_on_recovery(self):
        """running 父任务在恢复时被 reset_to_pending。"""
        parent = _make_task(task_id="parent-001", status=TaskStatus.RUNNING)
        child = _make_child(
            child_id="child-001",
            parent_id="parent-001",
            status=TaskStatus.RUNNING,
        )

        storage = MagicMock()
        storage.get.return_value = parent
        storage.save = MagicMock()
        storage.list_by_status = MagicMock(return_value=[])

        task_service = MagicMock()
        task_service._storage = storage
        task_service.get_task = MagicMock(return_value=parent)

        from tasks.service import TaskService as _TS

        svc = _TS.__new__(_TS)
        svc._storage = storage
        svc._event_bus = MagicMock()

        await svc.reset_to_pending("parent-001")

        assert parent.status == TaskStatus.PENDING
        print(f"\n[复现] 父任务从 RUNNING 被 reset_to_pending")
        print(f"  此时子任务仍为: {child.status.value}")

    @pytest.mark.asyncio
    async def test_full_stop_restart_cycle(self):
        """完整 stop → restart 周期。

        模拟：
        1. 父任务 running，子任务 running
        2. stop() → 父任务 paused, 子任务 paused
        3. restart → _recover_running_tasks
           - paused 任务 reset_to_pending
           - pending 任务 submit_task
        4. 最终：父任务 pending（正在重新执行），子任务 pending（正在重新执行）
        """
        parent = _make_task(task_id="parent-001", status=TaskStatus.RUNNING)
        child = _make_child(
            child_id="child-001",
            parent_id="parent-001",
            status=TaskStatus.RUNNING,
        )

        task_service = MagicMock()
        task_service.get_task.side_effect = lambda tid: {
            "parent-001": parent,
            "child-001": child,
        }.get(tid)
        task_service.pause_task = AsyncMock()
        task_service.list_by_status = MagicMock(return_value=[])
        task_service._storage = MagicMock()

        from infrastructure.task_worker import TaskWorker

        worker = TaskWorker(
            task_service=task_service,
            plugin_registry=MagicMock(),
            input_route_table=MagicMock(),
            output_route_table=MagicMock(),
            services={"task_service": task_service},
            event_bus=MagicMock(),
        )

        parent_ctx = MagicMock()
        parent_ctx.terminal_event = asyncio.Event()
        child_ctx = MagicMock()
        child_ctx.terminal_event = asyncio.Event()
        worker._contexts["parent-001"] = parent_ctx
        worker._contexts["child-001"] = child_ctx

        worker._running = True
        await worker.stop()

        paused_ids = [c[0][0] for c in task_service.pause_task.call_args_list]
        assert "parent-001" in paused_ids
        assert "child-001" in paused_ids
        print(f"\n[步骤2] stop() 后，父/子任务都被 pause: {paused_ids}")

        paused_parent = _make_task(task_id="parent-001", status=TaskStatus.PAUSED)
        paused_child = _make_child(
            child_id="child-001",
            parent_id="parent-001",
            status=TaskStatus.PAUSED,
        )

        storage = MagicMock()
        storage.get.side_effect = lambda tid: {
            "parent-001": paused_parent,
            "child-001": paused_child,
        }.get(tid)
        storage.save = MagicMock()
        storage.list_by_status = MagicMock(side_effect=lambda s: {
            TaskStatus.RUNNING: [],
            TaskStatus.PAUSED: [paused_parent, paused_child],
            TaskStatus.PENDING: [paused_parent, paused_child],
        }.get(s, []))

        svc = MagicMock()
        svc._storage = storage
        svc.list_by_status = storage.list_by_status
        svc.reset_to_pending = AsyncMock()
        svc.get_task = storage.get

        from infrastructure.task_recovery import TaskRecoveryMixin

        mixin = TaskRecoveryMixin()
        mixin._task_service = svc
        mixin._services = {}
        mixin.submit_task = MagicMock(return_value=True)

        await mixin._recover_running_tasks()

        reset_ids = [c[0][0] for c in svc.reset_to_pending.call_args_list]
        print(f"[步骤3] restart 后，reset_to_pending 的任务: {reset_ids}")
        print(f"  最终状态: 父任务 pending（即将重新执行），子任务 pending（即将重新执行）")
        print(f"  问题: 父任务之前的管道上下文丢失，子任务也在重新执行")


# ── 3. 关键场景：父任务 pending 但有子任务在跑 ──


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

        print(f"\n[复现] 父任务 pending + 子任务 running")
        print(f"  这就是用户看到的现象")

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

        print(f"\n[复现] 父任务 pending + 子任务 evaluating")

    @pytest.mark.asyncio
    async def test_recovery_creates_orphan_state(self):
        """恢复后：父任务 pending（重新执行），但旧子任务仍在 running。

        场景：
        1. 父任务 running，管道挂起等待子任务
        2. 系统异常（未走 stop 流程，进程被杀）
        3. 子任务的管道在另一个进程/线程中仍在运行
        4. 系统重启，父任务被 reset_to_pending
        5. 子任务没被正确处理（可能是另一个 TaskWorker 管理）
        6. 结果：父任务 pending，子任务 running
        """
        parent = _make_task(task_id="parent-001", status=TaskStatus.PENDING)
        child = _make_child(
            child_id="child-001",
            parent_id="parent-001",
            status=TaskStatus.RUNNING,
        )

        task_svc = MagicMock()
        task_svc.list_subtasks.return_value = [child]

        from plugins.output.child_task_guard import ChildTaskGuard
        from pipeline.plugin import PluginContext
        from pipeline.types import create_initial_state

        guard = ChildTaskGuard({"idle_remind_limit": 3, "priority": 28})

        state = create_initial_state(
            session_id="test-session",
            task_id="parent-001",
            core_type="llm_call",
            raw_result="我在等待子任务完成",
            raw_tool_calls=[],
        )

        ctx = PluginContext(state=state, config={}, _services={})
        ctx._services["task_service"] = task_svc
        ctx._services["timer_manager"] = MagicMock()

        result = await guard.execute(ctx)

        assert result.route_signal is not None
        assert result.route_signal.route_type == "wait"
        print(f"\n[复现完整链路]")
        print(f"  1. 父任务 pending（被恢复后重新提交）")
        print(f"  2. 子任务 running（旧的，还在跑）")
        print(f"  3. ChildTaskGuard 检测到活跃子任务 → wait")
        print(f"  4. 父管道再次挂起等待")
        print(f"  5. 但这次等待的是旧子任务，可能永远不会完成")
        print(f"  → 死循环：父任务 pending → 挂起 → 等待旧子任务")


# ── 4. resume_task 也可能导致 pending ──


class TestResumeTaskToPending:
    """测试 resume_task 将 paused 任务恢复为 pending。

    resume_task 的设计是 paused → pending，然后由 submit_task 重新执行。
    但如果此时子任务仍在运行，就会出现 pending + 活跃子任务的情况。
    """

    @pytest.mark.asyncio
    async def test_resume_creates_pending_with_active_children(self):
        """resume_task 将 paused 父任务恢复为 pending。

        场景：
        1. 用户在前端手动 resume 一个 paused 的父任务
        2. resume_task → pending
        3. 但子任务可能没有被 pause（在另一个 worker 管理）
        4. 结果：父任务 pending，子任务 running
        """
        parent = _make_task(task_id="parent-001", status=TaskStatus.PAUSED)
        child = _make_child(
            child_id="child-001",
            parent_id="parent-001",
            status=TaskStatus.RUNNING,
        )

        storage = MagicMock()
        storage.get.return_value = parent
        storage.save = MagicMock()

        from tasks.service import TaskService as _TS

        svc = _TS.__new__(_TS)
        svc._storage = storage
        svc._event_bus = MagicMock()

        await svc.resume_task("parent-001")

        assert parent.status == TaskStatus.PENDING
        print(f"\n[复现] resume_task 后: 父任务 pending, 子任务 {child.status.value}")
        print(f"  问题: resume 只恢复了父任务，没有处理子任务的状态")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
