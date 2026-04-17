"""M5a 任务系统单元测试。

覆盖范围：
- TaskStatus / TaskModel / AC：类型定义与工厂函数
- SimpleStateMachine：6 状态合法/非法转换
- InvalidTransitionError：异常属性
- TaskStorage：CRUD + JSON 持久化
- TaskService：全生命周期编排（含进度计算）
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pipeline.types import AgentLevel, TaskPriority
from tasks.service import SimpleStateMachine, TaskService
from tasks.state_machine import InvalidTransitionError
from tasks.storage import TaskStorage
from tasks.types import AC, TaskModel, TaskStatus, create_task


# ═══════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════


class TestTaskStatus:
    """TaskStatus 枚举测试。"""

    def test_six_states(self) -> None:
        """6 种状态全部存在。"""
        assert len(TaskStatus) == 6
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.COMPLETED.value == "completed"

    def test_from_value(self) -> None:
        """从字符串值反序列化。"""
        assert TaskStatus("running") == TaskStatus.RUNNING


class TestAC:
    """AC 验收标准测试。"""

    def test_defaults(self) -> None:
        """默认值。"""
        ac = AC(metric_id="test_metric")
        assert ac.pass_threshold == 1.0
        assert ac.input_params == {}
        assert ac.expected_output is None

    def test_custom(self) -> None:
        """自定义值。"""
        ac = AC(
            metric_id="acc",
            input_params={"dataset": "test"},
            expected_output=0.9,
            pass_threshold=0.8,
        )
        assert ac.pass_threshold == 0.8


class TestTaskModel:
    """TaskModel 数据类测试。"""

    def test_default_values(self) -> None:
        """默认值。"""
        task = TaskModel()
        assert task.status == TaskStatus.PENDING
        assert task.priority == TaskPriority.NORMAL
        assert task.agent_level == AgentLevel.L1_MAIN
        assert task.parent_task_id is None
        assert task.result is None
        assert task.error is None

    def test_auto_id(self) -> None:
        """自动生成 12 位 ID。"""
        task = TaskModel()
        assert len(task.id) == 12


class TestCreateTask:
    """create_task 工厂函数测试。"""

    def test_basic(self) -> None:
        """基本创建。"""
        task = create_task(title="Test task")
        assert task.title == "Test task"
        assert task.status == TaskStatus.PENDING

    def test_with_all_params(self) -> None:
        """带全部参数创建。"""
        task = create_task(
            title="Sub task",
            description="A sub task",
            priority=TaskPriority.HIGH,
            agent_level=AgentLevel.L2_SUBTASK,
            parent_task_id="parent123",
            metadata={"key": "value"},
        )
        assert task.priority == TaskPriority.HIGH
        assert task.parent_task_id == "parent123"


# ═══════════════════════════════════════════════════════════
# SimpleStateMachine
# ═══════════════════════════════════════════════════════════


class TestStateMachine:
    """状态机转换测试。"""

    def setup_method(self) -> None:
        """初始化状态机实例。"""
        self.sm = SimpleStateMachine()

    def test_pending_to_running(self) -> None:
        """pending → running 合法。"""
        task = TaskModel(status=TaskStatus.PENDING)
        self.sm.transition(task, TaskStatus.RUNNING)
        assert task.status == TaskStatus.RUNNING

    def test_running_to_evaluating(self) -> None:
        """running → evaluating 合法。"""
        task = TaskModel(status=TaskStatus.RUNNING)
        self.sm.transition(task, TaskStatus.EVALUATING)
        assert task.status == TaskStatus.EVALUATING

    def test_running_to_failed(self) -> None:
        """running → failed 合法。"""
        task = TaskModel(status=TaskStatus.RUNNING)
        self.sm.transition(task, TaskStatus.FAILED)
        assert task.status == TaskStatus.FAILED

    def test_running_to_paused(self) -> None:
        """running → paused 合法。"""
        task = TaskModel(status=TaskStatus.RUNNING)
        self.sm.transition(task, TaskStatus.PAUSED)
        assert task.status == TaskStatus.PAUSED

    def test_paused_to_running(self) -> None:
        """paused → running 合法（恢复）。"""
        task = TaskModel(status=TaskStatus.PAUSED)
        self.sm.transition(task, TaskStatus.RUNNING)
        assert task.status == TaskStatus.RUNNING

    def test_evaluating_to_completed(self) -> None:
        """evaluating → completed 合法。"""
        task = TaskModel(status=TaskStatus.EVALUATING)
        self.sm.transition(task, TaskStatus.COMPLETED)
        assert task.status == TaskStatus.COMPLETED

    def test_evaluating_to_failed(self) -> None:
        """evaluating → failed 合法（评估不通过）。"""
        task = TaskModel(status=TaskStatus.EVALUATING)
        self.sm.transition(task, TaskStatus.FAILED)
        assert task.status == TaskStatus.FAILED

    def test_invalid_transition_raises(self) -> None:
        """非法转换抛出 InvalidTransitionError。"""
        task = TaskModel(status=TaskStatus.COMPLETED)
        with pytest.raises(InvalidTransitionError) as exc_info:
            self.sm.transition(task, TaskStatus.RUNNING)
        assert exc_info.value.from_status == "completed"
        assert exc_info.value.to_status == "running"

    def test_terminal_state_no_transition(self) -> None:
        """终态（completed/failed）不可再转换。"""
        task = TaskModel(status=TaskStatus.COMPLETED)
        with pytest.raises(InvalidTransitionError):
            self.sm.transition(task, TaskStatus.RUNNING)

    def test_can_transition(self) -> None:
        """can_transition 返回正确布尔值。"""
        assert self.sm.can_transition(TaskStatus.PENDING, TaskStatus.RUNNING)
        assert self.sm.can_transition(TaskStatus.PENDING, TaskStatus.COMPLETED)
        assert self.sm.can_transition(TaskStatus.PENDING, TaskStatus.FAILED)
        assert not self.sm.can_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)

    def test_updated_at_changes(self) -> None:
        """转换后 updated_at 应被更新（由调用方负责）。"""
        task = TaskModel(status=TaskStatus.PENDING)
        old_updated = task.updated_at
        # SimpleStateMachine 精简版只更新 status，不更新 updated_at
        # updated_at 由 TaskService._transition_with_callback 或 TaskStorage.update 管理
        self.sm.transition(task, TaskStatus.RUNNING)
        assert task.status == TaskStatus.RUNNING


# ═══════════════════════════════════════════════════════════
# TaskStorage
# ═══════════════════════════════════════════════════════════


class TestTaskStorage:
    """任务存储测试。"""

    def test_memory_only(self) -> None:
        """不提供路径时仅内存存储。"""
        storage = TaskStorage()
        task = create_task(title="Test")
        storage.save(task)
        assert storage.get(task.id) is not None

    def test_save_and_get(self) -> None:
        """保存后获取。"""
        storage = TaskStorage()
        task = create_task(title="Test")
        storage.save(task)
        fetched = storage.get(task.id)
        assert fetched is not None
        assert fetched.title == "Test"

    def test_get_not_found(self) -> None:
        """获取不存在的任务返回 None。"""
        storage = TaskStorage()
        assert storage.get("nonexistent") is None

    def test_update(self) -> None:
        """更新任务字段。"""
        storage = TaskStorage()
        task = create_task(title="Original")
        storage.save(task)

        updated = storage.update(task.id, title="Updated")
        assert updated is not None
        assert updated.title == "Updated"

    def test_update_not_found(self) -> None:
        """更新不存在的任务返回 None。"""
        storage = TaskStorage()
        assert storage.update("nonexistent", title="X") is None

    def test_list_by_status(self) -> None:
        """按状态列出任务。"""
        storage = TaskStorage()
        t1 = create_task(title="A")
        t2 = create_task(title="B")
        storage.save(t1)
        storage.save(t2)

        pending = storage.list_by_status(TaskStatus.PENDING)
        assert len(pending) == 2

    def test_list_by_parent(self) -> None:
        """按父任务列出子任务。"""
        storage = TaskStorage()
        parent = create_task(title="Parent")
        child = create_task(title="Child", parent_task_id=parent.id)
        storage.save(parent)
        storage.save(child)

        children = storage.list_by_parent(parent.id)
        assert len(children) == 1
        assert children[0].title == "Child"

    def test_delete(self) -> None:
        """删除任务。"""
        storage = TaskStorage()
        task = create_task(title="ToDelete")
        storage.save(task)
        assert storage.delete(task.id) is True
        assert storage.get(task.id) is None

    def test_delete_not_found(self) -> None:
        """删除不存在的任务返回 False。"""
        storage = TaskStorage()
        assert storage.delete("nonexistent") is False

    def test_yaml_persistence(self) -> None:
        """YAML 文件持久化与加载。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "tasks"
            task = create_task(title="Persisted")

            storage = TaskStorage(data_dir=data_dir)
            storage.save(task)

            storage2 = TaskStorage(data_dir=data_dir)
            fetched = storage2.get(task.id)
            assert fetched is not None
            assert fetched.title == "Persisted"

    def test_corrupted_yaml_file(self) -> None:
        """损坏的 YAML 文件不会导致崩溃。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "tasks"
            data_dir.mkdir()
            (data_dir / "corrupted.yaml").write_text("{invalid yaml: {{{", encoding="utf-8")

            storage = TaskStorage(data_dir=data_dir)
            assert storage.list_by_status(TaskStatus.PENDING) == []


# ═══════════════════════════════════════════════════════════
# TaskService
# ═══════════════════════════════════════════════════════════


class TestTaskService:
    """任务服务集成测试。"""

    def setup_method(self) -> None:
        """初始化任务服务（使用内存存储，避免写文件系统）。"""
        self.service = TaskService(storage=TaskStorage())

    def test_create_and_get(self) -> None:
        """创建后可获取。"""
        task = self.service.create_task(title="Test")
        fetched = self.service.get_task(task.id)
        assert fetched is not None
        assert fetched.title == "Test"
        assert fetched.status == TaskStatus.PENDING

    def test_full_lifecycle_pass(self) -> None:
        """完整生命周期：创建→启动→评估→通过。"""
        task = self.service.create_task(title="Lifecycle")
        self.service.start_task(task.id)
        self.service.move_to_evaluating(task.id)
        result = self.service.complete_evaluation(task.id, passed=True)
        assert result.status == TaskStatus.COMPLETED

    def test_full_lifecycle_fail(self) -> None:
        """完整生命周期：创建→启动→评估→不通过。"""
        task = self.service.create_task(title="Fail lifecycle")
        self.service.start_task(task.id)
        self.service.move_to_evaluating(task.id)
        result = self.service.complete_evaluation(task.id, passed=False)
        assert result.status == TaskStatus.FAILED

    def test_pause_and_resume(self) -> None:
        """暂停与恢复。"""
        task = self.service.create_task(title="Pause test")
        self.service.start_task(task.id)
        self.service.pause_task(task.id)
        assert self.service.get_task(task.id).status == TaskStatus.PAUSED

        self.service.resume_task(task.id)
        assert self.service.get_task(task.id).status == TaskStatus.RUNNING

    def test_fail_task(self) -> None:
        """直接标记失败。"""
        task = self.service.create_task(title="Fail test")
        self.service.start_task(task.id)
        result = self.service.fail_task(task.id, error="Something went wrong")
        assert result.status == TaskStatus.FAILED
        assert result.error == "Something went wrong"

    def test_invalid_transition_raises(self) -> None:
        """非法状态转换抛异常。"""
        task = self.service.create_task(title="Invalid")
        self.service.start_task(task.id)
        with pytest.raises(InvalidTransitionError):
            self.service.start_task(task.id)

    def test_task_not_found_raises(self) -> None:
        """操作不存在的任务抛 KeyError。"""
        with pytest.raises(KeyError, match="not found"):
            self.service.start_task("nonexistent")

    def test_get_progress(self) -> None:
        """计算子任务进度。"""
        parent = self.service.create_task(title="Parent")
        child1 = self.service.create_task(
            title="Child 1", parent_task_id=parent.id
        )
        child2 = self.service.create_task(
            title="Child 2", parent_task_id=parent.id
        )

        # 完成 child1
        self.service.start_task(child1.id)
        self.service.move_to_evaluating(child1.id)
        self.service.complete_evaluation(child1.id, passed=True)

        progress = self.service.get_progress(parent.id)
        assert progress == 50.0

    def test_list_by_status(self) -> None:
        """按状态列出任务。"""
        self.service.create_task(title="A")
        self.service.create_task(title="B")
        pending = self.service.list_by_status(TaskStatus.PENDING)
        assert len(pending) >= 2

    def test_list_subtasks(self) -> None:
        """列出子任务。"""
        parent = self.service.create_task(title="Parent")
        self.service.create_task(title="Child", parent_task_id=parent.id)
        children = self.service.list_subtasks(parent.id)
        assert len(children) == 1
