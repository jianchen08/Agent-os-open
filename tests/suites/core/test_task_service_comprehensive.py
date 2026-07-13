"""TaskService 综合单元测试。

覆盖范围：
- SimpleStateMachine：所有合法/非法状态转换（100% 转换覆盖）
- TaskStorage：CRUD + 持久化 + 边界条件
- TaskService：全生命周期编排
  - 创建/查询/绑定
  - 状态转换：start/pause/resume/fail/complete_evaluation/force_transition
  - reset_to_pending / cancel_task_cascade
  - delete_task（容器/非容器/子任务）
  - force_transition / can_transition / get_valid_transitions
  - get_root_task_id
  - save_task / list_all
"""

from __future__ import annotations

import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.tasks.state_machine import InvalidTransitionError
from tasks.service import TaskService
from tasks.storage import TaskStorage
from tasks.types import TaskPriority, TaskStatus, create_task

# ═══════════════════════════════════════════════════════════
# Helper
# ═══════════════════════════════════════════════════════════

def _make_service() -> TaskService:
    """创建使用临时目录的 TaskService 实例。"""
    tmp_dir = tempfile.mkdtemp(prefix="test_task_service_")
    return TaskService(data_dir=tmp_dir)


def _move_to_evaluating(svc: TaskService, task_id: str) -> None:
    """辅助方法：将任务从 running 转到 evaluating（通过 force_transition）。"""
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        svc.force_transition(task_id, TaskStatus.EVALUATING)
    )


# ═══════════════════════════════════════════════════════════
# SimpleStateMachine 状态转换全覆盖 —— 已移除
#
# 原 TestSimpleStateMachineTransitions 基于旧转换表（scheduled/paused/
# suspended/blocked/cancelled 等状态），与当前 _TASK_TRANSITIONS（新 7 状态
# 模型：stopped 统一替代 suspended/cancelled）不符，全部参数化用例失败。
# 新转换表的覆盖由 SimpleStateMachine 自身的单元测试与
# tests/suites/core/test_tasks.py::TestStateMachine 承担。
# ═══════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# TaskStorage — CRUD + 持久化 + 边界条件
# ═══════════════════════════════════════════════════════════

class TestTaskStorageComprehensive:
    """TaskStorage 全面测试。"""

    def test_save_and_get_roundtrip(self) -> None:
        """保存后获取，字段完全一致。"""
        storage = TaskStorage()
        task = create_task(title="持久化测试", description="描述", priority=TaskPriority.HIGH)
        storage.save(task)

        fetched = storage.get(task.id)
        assert fetched is not None
        assert fetched.title == "持久化测试"
        assert fetched.description == "描述"
        assert fetched.priority == TaskPriority.HIGH
        assert fetched.status == TaskStatus.PENDING

    def test_get_nonexistent_returns_none(self) -> None:
        """获取不存在的任务返回 None。"""
        assert TaskStorage().get("不存在") is None

    def test_update_fields(self) -> None:
        """更新指定字段。"""
        storage = TaskStorage()
        task = create_task(title="原始")
        storage.save(task)

        updated = storage.update(task.id, title="更新后", description="新增描述")
        assert updated is not None
        assert updated.title == "更新后"
        assert updated.description == "新增描述"

    def test_update_nonexistent_returns_none(self) -> None:
        """更新不存在的任务返回 None。"""
        assert TaskStorage().update("不存在", title="X") is None

    def test_delete_existing(self) -> None:
        """删除已存在的任务。"""
        storage = TaskStorage()
        task = create_task(title="待删除")
        storage.save(task)
        assert storage.delete(task.id) is True
        assert storage.get(task.id) is None

    def test_delete_nonexistent(self) -> None:
        """删除不存在的任务返回 False。"""
        assert TaskStorage().delete("不存在") is False

    def test_list_by_status_empty(self) -> None:
        """无任务时按状态查询返回空列表。"""
        storage = TaskStorage()
        assert storage.list_by_status(TaskStatus.RUNNING) == []

    def test_list_by_status_filters_correctly(self) -> None:
        """按状态过滤任务。"""
        storage = TaskStorage()
        t1 = create_task(title="A")
        t2 = create_task(title="B")
        storage.save(t1)
        storage.save(t2)

        # 手动修改 t2 状态
        t2.status = TaskStatus.RUNNING
        storage.save(t2)

        pending = storage.list_by_status(TaskStatus.PENDING)
        running = storage.list_by_status(TaskStatus.RUNNING)
        assert len(pending) == 1
        assert len(running) == 1

    def test_list_by_parent_empty(self) -> None:
        """无子任务时返回空列表。"""
        storage = TaskStorage()
        assert storage.list_by_parent("无此父任务") == []

    def test_find_root_id_direct_root(self) -> None:
        """根任务的 root_id 是自身。"""
        storage = TaskStorage()
        root = create_task(title="Root")
        storage.save(root)
        assert storage._find_root_id(root) == root.id

    def test_find_root_id_nested(self) -> None:
        """多层嵌套时正确追溯根任务。"""
        storage = TaskStorage()
        root = create_task(title="Root")
        storage.save(root)
        child = create_task(title="Child", parent_task_id=root.id)
        storage.save(child)
        grandchild = create_task(title="Grandchild", parent_task_id=child.id)
        storage.save(grandchild)

        assert storage._find_root_id(grandchild) == root.id
        assert storage._find_root_id(child) == root.id

    def test_overwrite_save(self) -> None:
        """重复 save 会覆盖。"""
        storage = TaskStorage()
        task = create_task(title="V1")
        storage.save(task)
        task.title = "V2"
        storage.save(task)
        assert storage.get(task.id).title == "V2"

    def test_list_by_parent_multiple_children(self) -> None:
        """一个父任务有多个子任务。"""
        storage = TaskStorage()
        parent = create_task(title="Parent")
        storage.save(parent)
        for i in range(5):
            storage.save(create_task(title=f"Child-{i}", parent_task_id=parent.id))
        children = storage.list_by_parent(parent.id)
        assert len(children) == 5


# ═══════════════════════════════════════════════════════════
# TaskService — 创建与查询
# ═══════════════════════════════════════════════════════════

class TestTaskServiceCreate:
    """TaskService 创建与查询测试。"""

    def setup_method(self) -> None:
        """初始化 TaskService 实例。"""
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_create_task_defaults(self) -> None:
        """创建任务默认 PENDING 状态。"""
        task = await self.svc.create_task(title="测试")
        assert task.status == TaskStatus.PENDING
        assert task.title == "测试"
        assert task.id != ""

    @pytest.mark.asyncio
    async def test_create_task_with_kwargs(self) -> None:
        """创建任务带额外参数。"""
        task = await self.svc.create_task(
            title="子任务",
            description="描述",
            parent_task_id="parent_001",
            priority=TaskPriority.HIGH,
        )
        assert task.parent_task_id == "parent_001"
        assert task.priority == TaskPriority.HIGH

    @pytest.mark.asyncio
    async def test_get_task_found(self) -> None:
        """获取存在的任务。"""
        task = await self.svc.create_task(title="查找")
        found = self.svc.get_task(task.id)
        assert found is not None
        assert found.id == task.id

    def test_get_task_not_found(self) -> None:
        """获取不存在的任务返回 None。"""
        assert self.svc.get_task("不存在") is None

    @pytest.mark.asyncio
    async def test_list_by_status(self) -> None:
        """按状态列出任务。"""
        await self.svc.create_task(title="A")
        await self.svc.create_task(title="B")
        pending = self.svc.list_by_status(TaskStatus.PENDING)
        assert len(pending) >= 2

    @pytest.mark.asyncio
    async def test_list_subtasks(self) -> None:
        """列出子任务。"""
        parent = await self.svc.create_task(title="Parent")
        await self.svc.create_task(title="C1", parent_task_id=parent.id)
        await self.svc.create_task(title="C2", parent_task_id=parent.id)
        children = self.svc.list_subtasks(parent.id)
        assert len(children) == 2

    @pytest.mark.asyncio
    async def test_list_subtasks_empty(self) -> None:
        """无子任务时返回空列表。"""
        parent = await self.svc.create_task(title="Parent")
        assert self.svc.list_subtasks(parent.id) == []


# ═══════════════════════════════════════════════════════════
# TaskService — 状态转换（全生命周期）
# ═══════════════════════════════════════════════════════════

class TestTaskServiceTransitions:
    """TaskService 状态转换测试。"""

    def setup_method(self) -> None:
        """初始化 TaskService 实例。"""
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_start_task_success(self) -> None:
        """pending → running 成功。"""
        task = await self.svc.create_task(title="启动")
        await self.svc.start_task(task.id)
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_move_to_evaluating_success(self) -> None:
        """running → evaluating 成功。"""
        task = await self.svc.create_task(title="评估")
        await self.svc.start_task(task.id)
        await self.svc.force_transition(task.id, TaskStatus.EVALUATING)
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.EVALUATING

    @pytest.mark.asyncio
    async def test_complete_evaluation_passed(self) -> None:
        """evaluating → completed（通过）。"""
        task = await self.svc.create_task(title="通过")
        await self.svc.start_task(task.id)
        await self.svc.force_transition(task.id, TaskStatus.EVALUATING)
        await self.svc.complete_evaluation(task.id, passed=True, result={"score": 0.95})
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.COMPLETED
        assert fetched.result == {"score": 0.95}

    @pytest.mark.asyncio
    async def test_complete_evaluation_failed(self) -> None:
        """evaluating → failed（不通过）。"""
        task = await self.svc.create_task(title="不通过")
        await self.svc.start_task(task.id)
        await self.svc.force_transition(task.id, TaskStatus.EVALUATING)
        await self.svc.complete_evaluation(task.id, passed=False)
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_resume_task_success(self) -> None:
        """suspended → running 成功（resume_task 将 suspended 恢复为 running）。"""
        task = await self.svc.create_task(title="恢复")
        await self.svc.start_task(task.id)
        await self.svc.pause_task(task.id)
        result = await self.svc.resume_task(task.id)
        # BUG-FIX-fix_20260603_resume_wake_engine:
        # resume_task 现在将状态设为 RUNNING（而非 PENDING），以便挂起的管道引擎继续执行
        assert result.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_fail_task_with_reason(self) -> None:
        """running → failed，带错误信息。"""
        task = await self.svc.create_task(title="失败")
        await self.svc.start_task(task.id)
        await self.svc.fail_task(task.id, reason="出错了")
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.FAILED
        assert fetched.error == "出错了"

    @pytest.mark.asyncio
    async def test_fail_task_without_reason(self) -> None:
        """running → failed，不带错误信息。"""
        task = await self.svc.create_task(title="静默失败")
        await self.svc.start_task(task.id)
        await self.svc.fail_task(task.id)
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_full_lifecycle_pass(self) -> None:
        """完整生命周期：pending → running → evaluating → completed。"""
        task = await self.svc.create_task(title="全流程通过")
        await self.svc.start_task(task.id)
        await self.svc.force_transition(task.id, TaskStatus.EVALUATING)
        await self.svc.complete_evaluation(task.id, passed=True)
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_full_lifecycle_fail(self) -> None:
        """完整生命周期：pending → running → evaluating → failed。"""
        task = await self.svc.create_task(title="全流程失败")
        await self.svc.start_task(task.id)
        await self.svc.force_transition(task.id, TaskStatus.EVALUATING)
        await self.svc.complete_evaluation(task.id, passed=False)
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self) -> None:
        """非法状态转换抛出 InvalidTransitionError。"""
        task = await self.svc.create_task(title="非法")
        # pending → evaluating 是非法转换（不在 _TASK_TRANSITIONS 中）
        with pytest.raises(InvalidTransitionError):
            await self.svc.force_transition(task.id, TaskStatus.EVALUATING)

    @pytest.mark.asyncio
    async def test_task_not_found_raises_key_error(self) -> None:
        """操作不存在的任务抛出 KeyError。"""
        with pytest.raises(KeyError):
            await self.svc.start_task("不存在")

        # fail_task 对不存在的任务不抛 KeyError（返回 None）
        # move_to_evaluating 已移除


# ═══════════════════════════════════════════════════════════
# TaskService — reset_to_pending
# ═══════════════════════════════════════════════════════════

class TestTaskServiceResetToPending:
    """reset_to_pending 测试（强制重置）。"""

    def setup_method(self) -> None:
        """初始化 TaskService 实例。"""
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_reset_running_to_pending(self) -> None:
        """将 running 任务重置为 pending。"""
        task = await self.svc.create_task(title="运行中")
        await self.svc.start_task(task.id)

        await self.svc.reset_to_pending(task.id)
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_reset_failed_to_pending(self) -> None:
        """将 failed 任务重置为 pending。"""
        task = await self.svc.create_task(title="失败")
        await self.svc.start_task(task.id)
        await self.svc.fail_task(task.id, reason="崩溃")

        await self.svc.reset_to_pending(task.id)
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.PENDING


# ═══════════════════════════════════════════════════════════
# TaskService — delete_task
# ═══════════════════════════════════════════════════════════

class TestTaskServiceDelete:
    """delete_task 测试（容器/非容器/子任务删除策略）。"""

    def setup_method(self) -> None:
        """初始化 TaskService 实例。"""
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self) -> None:
        """删除不存在的任务返回 False。"""
        result = await self.svc.delete_task("不存在")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_normal_task(self) -> None:
        """删除普通任务（无子任务）— 硬删除。"""
        task = await self.svc.create_task(title="普通任务")
        await self.svc.start_task(task.id)

        result = await self.svc.delete_task(task.id)
        assert result is True
        assert self.svc.get_task(task.id) is None

    @pytest.mark.asyncio
    async def test_delete_container_task_soft_delete(self) -> None:
        """删除容器任务 — 软删除（标记 soft_deleted）。"""
        container = await self.svc.create_task(
            title="容器任务",
            metadata={"task_scope": "container"},
        )
        # 创建子任务使其成为容器
        await self.svc.create_task(
            title="子任务",
            parent_task_id=container.id,
        )

        result = await self.svc.delete_task(container.id)
        assert result is True

        # 容器任务仍然存在（软删除）
        fetched = self.svc.get_task(container.id)
        assert fetched is not None
        assert fetched.metadata.get("soft_deleted") is True

    @pytest.mark.asyncio
    async def test_delete_container_cascades_children(self) -> None:
        """删除容器任务级联取消子任务（通过 soft_delete_container）。"""
        container = await self.svc.create_task(
            title="容器",
            metadata={"task_scope": "container"},
        )
        child = await self.svc.create_task(
            title="子任务",
            parent_task_id=container.id,
        )
        await self.svc.start_task(child.id)

        # soft_delete_container 会级联取消子任务并硬删除记录
        result = await self.svc.soft_delete_container(container.id, reason="用户删除")
        assert result.get("soft_deleted") is True

        # 子任务记录被硬删除
        fetched_child = self.svc.get_task(child.id)
        assert fetched_child is None

    @pytest.mark.asyncio
    async def test_delete_child_of_container_no_workspace_cleanup(self) -> None:
        """容器子任务删除时不清理工作空间。"""
        container = await self.svc.create_task(
            title="容器",
            metadata={"task_scope": "container"},
        )
        child = await self.svc.create_task(
            title="子任务",
            parent_task_id=container.id,
        )
        await self.svc.start_task(child.id)

        # delete_task 对无子任务的任务直接硬删除
        result = await self.svc.delete_task(child.id)
        assert result is True
        assert self.svc.get_task(child.id) is None

    @pytest.mark.asyncio
    async def test_delete_root_task_with_subtasks(self) -> None:
        """删除非容器根任务时硬删除并级联清理子任务。

        delete_task 统一委托 hard_delete_task，判定口径为 task_scope=container。
        非 container 的根任务即使有子任务也走硬删除 + 级联清理，与工具层一致。
        """
        root = await self.svc.create_task(title="根任务")
        child1 = await self.svc.create_task(
            title="子任务1",
            parent_task_id=root.id,
        )
        child2 = await self.svc.create_task(
            title="子任务2",
            parent_task_id=root.id,
        )
        await self.svc.start_task(child1.id)
        await self.svc.start_task(child2.id)

        # delete_task 委托 hard_delete_task 执行硬删除 + 级联清理
        await self.svc.delete_task(root.id)

        # 根任务被硬删除（不再软删除保留）
        assert self.svc.get_task(root.id) is None
        # 子任务被级联删除
        assert self.svc.get_task(child1.id) is None
        assert self.svc.get_task(child2.id) is None


# ═══════════════════════════════════════════════════════════
# TaskService — cancel_task_cascade
# ═══════════════════════════════════════════════════════════

class TestTaskServiceCascadeCancel:
    """cancel_task_cascade 级联取消测试。"""

    def setup_method(self) -> None:
        """初始化 TaskService 实例。"""
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_cascade_no_subtasks(self) -> None:
        """无子任务时级联取消返回 0。"""
        task = await self.svc.create_task(title="独立任务")
        result = await self.svc.cancel_task_cascade(task.id)
        assert result == 0

    @pytest.mark.asyncio
    async def test_cascade_cancels_active_subtasks(self) -> None:
        """级联取消活跃子任务。"""
        parent = await self.svc.create_task(title="父任务")
        child1 = await self.svc.create_task(
            title="子任务1", parent_task_id=parent.id,
        )
        child2 = await self.svc.create_task(
            title="子任务2", parent_task_id=parent.id,
        )
        await self.svc.start_task(child1.id)
        await self.svc.start_task(child2.id)

        count = await self.svc.cancel_task_cascade(parent.id, reason="测试级联")
        assert count == 2
        # cancel_task_cascade 使用 cancel_task，新模型统一设为 STOPPED（合并旧 cancelled/suspended）
        assert self.svc.get_task(child1.id).status == TaskStatus.STOPPED
        assert self.svc.get_task(child2.id).status == TaskStatus.STOPPED

    @pytest.mark.asyncio
    async def test_cascade_cancels_all_subtasks(self) -> None:
        """级联取消会取消所有子任务（包括终态）。"""
        parent = await self.svc.create_task(title="父任务")
        child_completed = await self.svc.create_task(
            title="已完成子任务", parent_task_id=parent.id,
        )
        child_active = await self.svc.create_task(
            title="活跃子任务", parent_task_id=parent.id,
        )
        # 手动完成一个子任务
        await self.svc.start_task(child_completed.id)
        await self.svc.force_transition(child_completed.id, TaskStatus.EVALUATING)
        await self.svc.complete_evaluation(child_completed.id, passed=True)
        await self.svc.start_task(child_active.id)

        count = await self.svc.cancel_task_cascade(parent.id, reason="测试")
        # cancel_task_cascade 不检查子任务状态，全部取消
        assert count == 2
        assert self.svc.get_task(child_completed.id).status == TaskStatus.STOPPED
        assert self.svc.get_task(child_active.id).status == TaskStatus.STOPPED

    @pytest.mark.asyncio
    async def test_cascade_deeply_nested(self) -> None:
        """深层嵌套级联取消。"""
        root = await self.svc.create_task(title="根")
        child = await self.svc.create_task(
            title="子", parent_task_id=root.id,
        )
        grandchild = await self.svc.create_task(
            title="孙", parent_task_id=child.id,
        )
        await self.svc.start_task(child.id)
        await self.svc.start_task(grandchild.id)

        count = await self.svc.cancel_task_cascade(root.id, reason="深层取消")
        assert count == 2
        assert self.svc.get_task(child.id).status == TaskStatus.STOPPED
        assert self.svc.get_task(grandchild.id).status == TaskStatus.STOPPED


# ═══════════════════════════════════════════════════════════
# TaskService — 绑定操作
# ═══════════════════════════════════════════════════════════

class TestTaskServiceBind:
    """bind_pipeline_run 测试。"""

    def setup_method(self) -> None:
        """初始化 TaskService 实例。"""
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_bind_pipeline_run(self) -> None:
        """绑定管道运行 ID。"""
        task = await self.svc.create_task(title="绑定管道")
        await self.svc.bind_pipeline_run(task.id, "pipeline_run_001")

        # 持久化验证
        fetched = self.svc.get_task(task.id)
        assert fetched.pipeline_run_id == "pipeline_run_001"

    @pytest.mark.asyncio
    async def test_bind_pipeline_nonexistent_raises(self) -> None:
        """绑定管道到不存在的任务抛出 KeyError。"""
        with pytest.raises(KeyError):
            await self.svc.bind_pipeline_run("不存在", "pipeline_001")


# ═══════════════════════════════════════════════════════════
# TaskService — force_transition / can_transition / get_valid_transitions
# ═══════════════════════════════════════════════════════════

class TestTaskServiceTransitionHelpers:
    """force_transition / can_transition / get_valid_transitions 测试。"""

    def setup_method(self) -> None:
        """初始化 TaskService 实例。"""
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_force_transition_valid(self) -> None:
        """强制转换到合法状态。"""
        task = await self.svc.create_task(title="强制")
        await self.svc.force_transition(task.id, TaskStatus.RUNNING)
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_force_transition_invalid_raises(self) -> None:
        """强制转换到非法状态抛出 InvalidTransitionError。

        PENDING → EVALUATING 是非法转换（必须先经过 RUNNING）。
        """
        task = await self.svc.create_task(title="非法强制")
        with pytest.raises(InvalidTransitionError):
            await self.svc.force_transition(task.id, TaskStatus.EVALUATING)

    @pytest.mark.asyncio
    async def test_force_transition_nonexistent_raises(self) -> None:
        """强制转换不存在的任务抛出 KeyError。"""
        with pytest.raises(KeyError):
            await self.svc.force_transition("不存在", TaskStatus.RUNNING)

    @pytest.mark.asyncio
    async def test_can_transition_true(self) -> None:
        """can_transition 对合法转换返回 True。"""
        task = await self.svc.create_task(title="可转换")
        assert self.svc.can_transition(task.id, TaskStatus.RUNNING) is True

    @pytest.mark.asyncio
    async def test_can_transition_false(self) -> None:
        """can_transition 对非法转换返回 False。"""
        task = await self.svc.create_task(title="不可转换")
        # pending → evaluating 不在 _TASK_TRANSITIONS 中
        assert self.svc.can_transition(task.id, TaskStatus.EVALUATING) is False

    def test_can_transition_nonexistent_returns_false(self) -> None:
        """can_transition 对不存在的任务返回 False。"""
        assert self.svc.can_transition("不存在", TaskStatus.RUNNING) is False

    @pytest.mark.asyncio
    async def test_get_valid_transitions_pending(self) -> None:
        """获取 pending 状态的有效转换列表。"""
        task = await self.svc.create_task(title="查询转换")
        transitions = self.svc.get_valid_transitions(task.id)
        assert "running" in transitions
        # 新 7 状态模型：stopped 统一替代旧 suspended/scheduled/cancelled
        assert "stopped" in transitions

    @pytest.mark.asyncio
    async def test_get_valid_transitions_running(self) -> None:
        """获取 running 状态的有效转换列表。"""
        task = await self.svc.create_task(title="运行中转换")
        await self.svc.start_task(task.id)
        transitions = self.svc.get_valid_transitions(task.id)
        assert "completed" in transitions
        assert "failed" in transitions
        assert "evaluating" in transitions
        # 新模型：stopped 替代旧 suspended
        assert "stopped" in transitions

    def test_get_valid_transitions_nonexistent(self) -> None:
        """获取不存在的任务的转换列表返回空。"""
        assert self.svc.get_valid_transitions("不存在") == []


# ═══════════════════════════════════════════════════════════
# TaskService — get_root_task_id
# ═══════════════════════════════════════════════════════════

class TestTaskServiceRootAndProgress:
    """get_root_task_id 测试。"""

    def setup_method(self) -> None:
        """初始化 TaskService 实例。"""
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_get_root_task_id_root(self) -> None:
        """根任务的 root_task_id 是自身。"""
        root = await self.svc.create_task(title="根")
        assert self.svc.get_root_task_id(root.id) == root.id

    @pytest.mark.asyncio
    async def test_get_root_task_id_child(self) -> None:
        """子任务的 root_task_id 是根任务。"""
        root = await self.svc.create_task(title="根")
        child = await self.svc.create_task(title="子", parent_task_id=root.id)
        assert self.svc.get_root_task_id(child.id) == root.id

    @pytest.mark.asyncio
    async def test_get_root_task_id_grandchild(self) -> None:
        """孙任务的 root_task_id 是根任务。"""
        root = await self.svc.create_task(title="根")
        child = await self.svc.create_task(title="子", parent_task_id=root.id)
        grandchild = await self.svc.create_task(title="孙", parent_task_id=child.id)
        assert self.svc.get_root_task_id(grandchild.id) == root.id

    def test_get_root_task_id_nonexistent(self) -> None:
        """不存在的任务返回 None。"""
        assert self.svc.get_root_task_id("不存在") is None


# ═══════════════════════════════════════════════════════════
# TaskService — save_task
# ═══════════════════════════════════════════════════════════

class TestTaskServiceSave:
    """save_task 测试。"""

    def setup_method(self) -> None:
        """初始化 TaskService 实例。"""
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_save_task_updates_storage(self) -> None:
        """外部修改后保存任务。"""
        task = await self.svc.create_task(title="原始")
        task.title = "修改后"
        task.status = TaskStatus.RUNNING
        await self.svc.save_task(task)

        fetched = self.svc.get_task(task.id)
        assert fetched.title == "修改后"
        assert fetched.status == TaskStatus.RUNNING


# ═══════════════════════════════════════════════════════════
# TaskService — list_all
# ═══════════════════════════════════════════════════════════

class TestTaskServiceListAll:
    """list_all 测试。"""

    def setup_method(self) -> None:
        """初始化 TaskService 实例。"""
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_list_all_default(self) -> None:
        """默认返回最多 1000 条。"""
        for i in range(3):
            await self.svc.create_task(title=f"任务-{i}")
        tasks = await self.svc.list_all()
        assert len(tasks) >= 3

    @pytest.mark.asyncio
    async def test_list_all_with_limit(self) -> None:
        """限制返回数量。"""
        for i in range(5):
            await self.svc.create_task(title=f"任务-{i}")
        tasks = await self.svc.list_all(limit=2)
        assert len(tasks) == 2

    @pytest.mark.asyncio
    async def test_list_all_reverse_order(self) -> None:
        """默认按创建时间倒序。"""
        t1 = await self.svc.create_task(title="第一个")
        t2 = await self.svc.create_task(title="第二个")
        tasks = await self.svc.list_all(reverse=True)
        # 最新的在前面
        ids = [t.id for t in tasks if t.id in (t1.id, t2.id)]
        assert ids[0] == t2.id
        assert ids[1] == t1.id


# ═══════════════════════════════════════════════════════════
# TaskService — EventBus 集成
# ═══════════════════════════════════════════════════════════

class TestTaskServiceEventBus:
    """EventBus 事件广播测试。"""

    @pytest.mark.asyncio
    async def test_transition_with_event_bus(self) -> None:
        """状态转换时通过 EventBus 广播事件（不抛异常即可）。"""
        mock_bus = MagicMock()
        mock_bus.emit = AsyncMock()
        tmp_dir = tempfile.mkdtemp(prefix="test_event_bus_")
        svc = TaskService(event_bus=mock_bus, data_dir=tmp_dir)

        task = await svc.create_task(title="事件测试")
        await svc.start_task(task.id)
        # 不崩溃即可


# ═══════════════════════════════════════════════════════════
# TaskService — _is_child_of_container
# ═══════════════════════════════════════════════════════════

class TestTaskServiceIsChildOfContainer:
    """_is_child_of_container 测试。"""

    def setup_method(self) -> None:
        """初始化 TaskService 实例。"""
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_root_task_is_not_child(self) -> None:
        """根任务不是容器子任务。"""
        task = await self.svc.create_task(title="根")
        assert self.svc._is_child_of_container(task) is False

    @pytest.mark.asyncio
    async def test_child_of_container(self) -> None:
        """容器任务的子任务返回 True。"""
        container = await self.svc.create_task(
            title="容器",
            metadata={"task_scope": "container"},
        )
        child = await self.svc.create_task(
            title="子任务",
            parent_task_id=container.id,
        )
        assert self.svc._is_child_of_container(child) is True

    @pytest.mark.asyncio
    async def test_child_of_non_container(self) -> None:
        """非容器任务的子任务返回 False。"""
        parent = await self.svc.create_task(title="普通父任务")
        child = await self.svc.create_task(
            title="子任务",
            parent_task_id=parent.id,
        )
        assert self.svc._is_child_of_container(child) is False

    @pytest.mark.asyncio
    async def test_deep_child_of_container(self) -> None:
        """容器的深层子任务返回 True。"""
        container = await self.svc.create_task(
            title="容器",
            metadata={"task_scope": "container"},
        )
        child = await self.svc.create_task(
            title="子", parent_task_id=container.id,
        )
        grandchild = await self.svc.create_task(
            title="孙", parent_task_id=child.id,
        )
        assert self.svc._is_child_of_container(grandchild) is True


# ═══════════════════════════════════════════════════════════
# TaskService — 边界条件与异常
# ═══════════════════════════════════════════════════════════

class TestTaskServiceEdgeCases:
    """边界条件与异常场景测试。"""

    def setup_method(self) -> None:
        """初始化 TaskService 实例。"""
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_multiple_transitions_sequential(self) -> None:
        """连续多次状态转换。"""
        task = await self.svc.create_task(title="连续转换")
        # pending -> running
        await self.svc.start_task(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.RUNNING

        # running -> paused（新模型 pause_task 设为 STOPPED）
        await self.svc.pause_task(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.STOPPED

        # paused -> running (resume_task 将 suspended 恢复为 running)
        await self.svc.resume_task(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.RUNNING

        # running -> evaluating
        await self.svc.force_transition(task.id, TaskStatus.EVALUATING)
        assert self.svc.get_task(task.id).status == TaskStatus.EVALUATING

        # evaluating -> completed
        await self.svc.complete_evaluation(task.id, passed=True)
        assert self.svc.get_task(task.id).status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_failed_to_pending_retry(self) -> None:
        """失败后重试（failed → pending → running → completed）。"""
        task = await self.svc.create_task(title="重试")
        await self.svc.start_task(task.id)
        await self.svc.fail_task(task.id, reason="第一次失败")

        # 重置为 pending
        await self.svc.reset_to_pending(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.PENDING

        # 重新执行
        await self.svc.start_task(task.id)
        await self.svc.force_transition(task.id, TaskStatus.EVALUATING)
        await self.svc.complete_evaluation(task.id, passed=True)
        fetched = self.svc.get_task(task.id)
        assert fetched.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_create_task_with_all_options(self) -> None:
        """创建任务传入所有可选参数。"""
        task = await self.svc.create_task(
            title="完整任务",
            description="详细描述",
            parent_task_id="parent_001",
            parent_pipeline_id="pipeline_001",
            metadata={"custom_key": "custom_value"},
            priority=TaskPriority.CRITICAL,
        )
        assert task.title == "完整任务"
        assert task.description == "详细描述"
        assert task.parent_task_id == "parent_001"
        assert task.priority == TaskPriority.CRITICAL
        assert task.metadata.get("custom_key") == "custom_value"
