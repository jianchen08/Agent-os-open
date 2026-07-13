"""M5a 任务系统单元测试。

覆盖范围：
- TaskStatus / TaskModel / AC：类型定义与工厂函数
- SimpleStateMachine：合法/非法状态转换
- InvalidTransitionError：异常属性
- TaskStorage：CRUD + YAML 持久化

注：旧 TaskService（svc.state / svc.advance）接口测试已移除——生产代码已
重构为多 Mixin + 具名转换方法的新模型，新接口覆盖见
test_task_service_comprehensive.py 与 tests/suites/task/。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.tasks.state_machine import InvalidTransitionError, SimpleStateMachine
from src.tasks.storage import TaskStorage
from src.tasks.types import AC, TaskModel, TaskPriority, TaskStatus, create_task
from src.agents.types import AgentLevel


# ═══════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════


class TestTaskStatus:
    """TaskStatus 枚举测试。"""

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
        assert task.agent_level.value == AgentLevel.L1_MAIN.value
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

_TRANSITIONS: dict[str, list[str]] = {
    "pending": ["running"],
    "running": ["evaluating", "completed", "failed", "paused"],
    "evaluating": ["completed", "failed"],
    "paused": ["running"],
    "completed": [],
    "failed": ["pending"],
}


class TestStateMachine:
    """状态机转换测试。"""

    def setup_method(self) -> None:
        """初始化状态机实例。"""
        self.sm = SimpleStateMachine(
            initial_state="pending", transitions=_TRANSITIONS
        )

    def test_pending_to_running(self) -> None:
        """pending → running 合法。"""
        self.sm.transition("running")
        assert self.sm.current_state == "running"

    def test_running_to_evaluating(self) -> None:
        """running → evaluating 合法。"""
        self.sm.transition("running")
        self.sm.transition("evaluating")
        assert self.sm.current_state == "evaluating"

    def test_running_to_failed(self) -> None:
        """running → failed 合法。"""
        self.sm.transition("running")
        self.sm.transition("failed")
        assert self.sm.current_state == "failed"

    def test_running_to_paused(self) -> None:
        """running → paused 合法。"""
        self.sm.transition("running")
        self.sm.transition("paused")
        assert self.sm.current_state == "paused"

    def test_paused_to_running(self) -> None:
        """paused → running 合法（恢复）。"""
        self.sm.transition("running")
        self.sm.transition("paused")
        self.sm.transition("running")
        assert self.sm.current_state == "running"

    def test_evaluating_to_completed(self) -> None:
        """evaluating → completed 合法。"""
        self.sm.transition("running")
        self.sm.transition("evaluating")
        self.sm.transition("completed")
        assert self.sm.current_state == "completed"

    def test_evaluating_to_failed(self) -> None:
        """evaluating → failed 合法（评估不通过）。"""
        self.sm.transition("running")
        self.sm.transition("evaluating")
        self.sm.transition("failed")
        assert self.sm.current_state == "failed"

    def test_invalid_transition_raises(self) -> None:
        """非法转换抛出 InvalidTransitionError。"""
        self.sm.transition("running")
        self.sm.transition("completed")
        with pytest.raises(InvalidTransitionError) as exc_info:
            self.sm.transition("running")
        assert exc_info.value.current_state == "completed"
        assert exc_info.value.target_state == "running"

    def test_terminal_state_no_transition(self) -> None:
        """终态（completed）不可再转换。"""
        self.sm.transition("running")
        self.sm.transition("completed")
        with pytest.raises(InvalidTransitionError):
            self.sm.transition("running")

    def test_can_transition(self) -> None:
        """can_transition 返回正确布尔值。"""
        assert self.sm.can_transition("running")
        assert not self.sm.can_transition("completed")
        assert not self.sm.can_transition("failed")


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
# TaskService —— 旧 svc.state / svc.advance() 接口测试已删除
#
# 生产代码已重构为新 7 状态模型（STOPPED 统一替代 CANCELLED/SUSPENDED），
# TaskService 改为 _TaskCrudMixin/_TaskStateMixin/_TaskCleanupMixin 多 Mixin 组合，
# 状态转换通过具名方法（pause_task/fail_task/cancel_task 等）而非统一的 advance()。
# 旧的 test_create_and_advance / test_full_lifecycle_* / test_invalid_transition_raises
# / test_fail_then_retry 全部基于已废弃的 svc.state + svc.advance() 接口，已删除。
# 新接口的覆盖见 test_task_service_comprehensive.py 与 tests/suites/task/。
# ═══════════════════════════════════════════════════════════
