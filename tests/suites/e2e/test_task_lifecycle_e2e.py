"""场景1：任务生命周期端到端测试。

覆盖任务状态流转：
- 正常路径：pending → running → evaluating → completed
- 超时路径：running → timeout（通过 ExecutionStatus 状态机验证）
- 失败路径：running → failed
- 重试路径：evaluating → running（打回重做）→ evaluating → completed
"""
import pytest

from tasks.service import TaskService
from tasks.state_machine import SimpleStateMachine
from tasks.storage import TaskStorage
from tasks.types import TaskStatus, create_task


# ── Fixture ──────────────────────────────────────────────────────

@pytest.fixture
def storage(tmp_path):
    """创建临时任务存储。"""
    return TaskStorage(data_dir=str(tmp_path / "tasks"))


@pytest.fixture
def task_service(storage):
    """创建任务服务实例。"""
    return TaskService(storage=storage)


# ── 1. 状态机基础验证 ──────────────────────────────────────────────

class TestStateMachine:
    """SimpleStateMachine 转换规则验证。"""

    def test_pending_to_running_is_valid(self):
        sm = SimpleStateMachine()
        assert sm.can_transition(TaskStatus.PENDING, TaskStatus.RUNNING)

    def test_running_to_evaluating_is_valid(self):
        sm = SimpleStateMachine()
        assert sm.can_transition(TaskStatus.RUNNING, TaskStatus.EVALUATING)

    def test_evaluating_to_completed_is_valid(self):
        sm = SimpleStateMachine()
        assert sm.can_transition(TaskStatus.EVALUATING, TaskStatus.COMPLETED)

    def test_evaluating_to_failed_is_valid(self):
        sm = SimpleStateMachine()
        assert sm.can_transition(TaskStatus.EVALUATING, TaskStatus.FAILED)

    def test_running_to_failed_is_valid(self):
        sm = SimpleStateMachine()
        assert sm.can_transition(TaskStatus.RUNNING, TaskStatus.FAILED)

    def test_evaluating_to_running_is_valid(self):
        """评估不通过时打回重做。"""
        sm = SimpleStateMachine()
        assert sm.can_transition(TaskStatus.EVALUATING, TaskStatus.RUNNING)

    def test_completed_to_running_is_invalid(self):
        """终态不能继续转换（只能通过 reactivate）。"""
        sm = SimpleStateMachine()
        assert not sm.can_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)

    def test_failed_to_pending_is_valid(self):
        """失败任务可以重置回 pending 重试。"""
        sm = SimpleStateMachine()
        assert sm.can_transition(TaskStatus.FAILED, TaskStatus.PENDING)


# ── 2. 正常全流程：pending → running → evaluating → completed ──────

class TestTaskLifecycleHappyPath:
    """任务正常完成全流程。"""

    @pytest.mark.asyncio
    async def test_full_lifecycle_completed(self, task_service):
        """完整路径：创建 → 启动 → 评估 → 完成。"""
        # Arrange: 创建任务
        task = await task_service.create_task(title="测试任务", description="正常流程")
        assert task.status == TaskStatus.PENDING

        # Act: 启动
        task = await task_service.start_task(task.id)
        assert task.status == TaskStatus.RUNNING
        assert task.started_at is not None

        # Act: 移入评估
        task = await task_service.move_to_evaluating(task.id)
        assert task.status == TaskStatus.EVALUATING

        # Act: 评估通过 → 完成
        task = await task_service.complete_evaluation(task.id, passed=True, result={"score": 95})
        assert task.status == TaskStatus.COMPLETED
        assert task.completed_at is not None
        assert task.result is not None

    @pytest.mark.asyncio
    async def test_task_persisted_across_statuses(self, task_service, storage):
        """验证状态变更被持久化。"""
        task = await task_service.create_task(title="持久化测试")
        task_id = task.id

        # 启动
        await task_service.start_task(task_id)
        retrieved = task_service.get_task(task_id)
        assert retrieved.status == TaskStatus.RUNNING

        # 评估
        await task_service.move_to_evaluating(task_id)
        retrieved = task_service.get_task(task_id)
        assert retrieved.status == TaskStatus.EVALUATING

        # 完成
        await task_service.complete_evaluation(task_id, passed=True)
        retrieved = task_service.get_task(task_id)
        assert retrieved.status == TaskStatus.COMPLETED


# ── 3. 失败路径 ────────────────────────────────────────────────────

class TestTaskLifecycleFailure:
    """任务执行失败流程。"""

    @pytest.mark.asyncio
    async def test_running_to_failed(self, task_service):
        """执行过程中失败：running → failed。"""
        task = await task_service.create_task(title="会失败的任务")
        await task_service.start_task(task.id)

        task = await task_service.fail_task(task.id, error="执行超时")
        assert task.status == TaskStatus.FAILED
        assert task.error == "执行超时"

    @pytest.mark.asyncio
    async def test_evaluating_to_failed(self, task_service):
        """评估不通过 → failed。"""
        task = await task_service.create_task(title="评估不通过")
        await task_service.start_task(task.id)
        await task_service.move_to_evaluating(task.id)

        task = await task_service.complete_evaluation(
            task.id, passed=False, result={"score": 30, "reason": "未达到标准"},
        )
        assert task.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_failed_task_can_be_reset_to_pending(self, task_service):
        """失败任务重置回 pending 重新执行。"""
        task = await task_service.create_task(title="重试任务")
        await task_service.start_task(task.id)
        await task_service.fail_task(task.id, error="首次失败")

        task = await task_service.reset_to_pending(task.id)
        assert task.status == TaskStatus.PENDING
        assert task.error == ""
        assert task.started_at is None


# ── 4. 重试路径（打回重做） ──────────────────────────────────────────

class TestTaskLifecycleRetry:
    """评估打回重做流程。"""

    @pytest.mark.asyncio
    async def test_reject_then_pass(self, task_service):
        """打回后重新执行并通过。"""
        task = await task_service.create_task(title="打回重做任务")
        await task_service.start_task(task.id)
        await task_service.move_to_evaluating(task.id)

        # 打回（evaluating → running）
        task = await task_service.reject_task(task.id, reason="质量不够")
        assert task.status == TaskStatus.RUNNING
        assert task.reject_count == 1

        # 重新评估通过
        await task_service.move_to_evaluating(task.id)
        task = await task_service.complete_evaluation(task.id, passed=True)
        assert task.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_reject_exhausted_to_failed(self, task_service):
        """打回次数耗尽后标记为 failed。"""
        task = await task_service.create_task(title="反复不合格")
        await task_service.start_task(task.id)
        await task_service.move_to_evaluating(task.id)

        # 连续打回 3 次
        for i in range(3):
            task = await task_service.reject_task(task.id, reason=f"第{i+1}次不合格")
            if i < 2:
                assert task.status == TaskStatus.RUNNING
                # 重新进入评估
                await task_service.move_to_evaluating(task.id)

        # 第 3 次后应该直接标记为 failed
        assert task.status == TaskStatus.FAILED
        assert task.reject_count == 3


# ── 5. ExecutionStatus 与 EXECUTION_TRANSITIONS 验证 ───────────────

class TestExecutionStatusTransitions:
    """ExecutionStatus 状态机转换规则（含 timeout 路径）。"""

    def test_timeout_is_terminal(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.TIMEOUT.is_terminal

    def test_timeout_is_failure(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.TIMEOUT.is_failure

    def test_running_can_transition_to_timeout(self):
        from core.states.execution import ExecutionStatus, EXECUTION_TRANSITIONS
        assert ExecutionStatus.TIMEOUT in EXECUTION_TRANSITIONS[ExecutionStatus.RUNNING]

    def test_timeout_can_transition_to_pending(self):
        """超时任务可以重新执行。"""
        from core.states.execution import ExecutionStatus, EXECUTION_TRANSITIONS
        assert ExecutionStatus.PENDING in EXECUTION_TRANSITIONS[ExecutionStatus.TIMEOUT]

    def test_completed_is_terminal(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.COMPLETED.is_terminal
        assert ExecutionStatus.COMPLETED.is_success

    def test_failed_is_terminal(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.FAILED.is_terminal
        assert ExecutionStatus.FAILED.is_failure

    def test_cancelled_is_terminal(self):
        from core.states.execution import ExecutionStatus
        assert ExecutionStatus.CANCELLED.is_terminal


# ── 6. 通用 StateMachine 验证 ──────────────────────────────────────

class TestGenericStateMachine:
    """core.states.machine.StateMachine 通用状态机。"""

    @pytest.mark.asyncio
    async def test_valid_transition_produces_event(self):
        from core.states.execution import ExecutionStatus, EXECUTION_TRANSITIONS
        from core.states.machine import StateMachine, StateMachineConfig

        config = StateMachineConfig(transitions=EXECUTION_TRANSITIONS)
        machine = StateMachine(config)

        event = await machine.transition(
            entity_type="task",
            entity_id="test-001",
            from_state=ExecutionStatus.PENDING,
            to_state=ExecutionStatus.RUNNING,
            reason="开始执行",
        )
        assert event.from_state == "pending"
        assert event.to_state == "running"
        assert event.reason == "开始执行"

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self):
        from core.states.execution import ExecutionStatus, EXECUTION_TRANSITIONS
        from core.states.machine import StateMachine, StateMachineConfig

        config = StateMachineConfig(transitions=EXECUTION_TRANSITIONS)
        machine = StateMachine(config)

        with pytest.raises(ValueError, match="非法状态转换"):
            await machine.transition(
                entity_type="task",
                entity_id="test-002",
                from_state=ExecutionStatus.COMPLETED,
                to_state=ExecutionStatus.RUNNING,
            )

    def test_can_transition_check(self):
        from core.states.execution import ExecutionStatus, EXECUTION_TRANSITIONS
        from core.states.machine import StateMachine, StateMachineConfig

        config = StateMachineConfig(transitions=EXECUTION_TRANSITIONS)
        machine = StateMachine(config)

        assert machine.can_transition(ExecutionStatus.PENDING, ExecutionStatus.RUNNING)
        assert not machine.can_transition(ExecutionStatus.COMPLETED, ExecutionStatus.RUNNING)

    def test_is_terminal(self):
        from core.states.execution import ExecutionStatus, EXECUTION_TRANSITIONS
        from core.states.machine import StateMachine, StateMachineConfig

        config = StateMachineConfig(transitions=EXECUTION_TRANSITIONS)
        machine = StateMachine(config)

        assert machine.is_terminal(ExecutionStatus.COMPLETED)
        # TIMEOUT 可转换到 PENDING，在转换表中不是终态
        assert not machine.is_terminal(ExecutionStatus.TIMEOUT)
        assert not machine.is_terminal(ExecutionStatus.RUNNING)
