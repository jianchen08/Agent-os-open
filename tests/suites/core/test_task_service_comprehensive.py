"""TaskService 综合单元测试。

覆盖范围：
- SimpleStateMachine：所有合法/非法状态转换（100% 转换覆盖）
- TaskStorage：CRUD + 持久化 + 边界条件
- TaskService：全生命周期编排
  - 创建/查询/绑定
  - 状态转换：start/pause/resume/fail/complete_evaluation/move_to_evaluating
  - reactivate_task / reset_to_pending / recover_to_completed
  - reject_task（含打回次数上限）
  - delete_task（容器/非容器/子任务）
  - cancel_task_cascade（级联取消）
  - force_transition / can_transition / get_valid_transitions
  - get_root_task_id / get_progress
  - save_task / list_all
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.types import AgentLevel
from tasks.service import SimpleStateMachine, TaskService
from tasks.state_machine import InvalidTransitionError
from tasks.storage import TaskStorage
from tasks.types import TaskModel, TaskPriority, TaskStatus, create_task


# ═══════════════════════════════════════════════════════════
# Helper
# ═══════════════════════════════════════════════════════════

def _make_service() -> TaskService:
    """创建使用内存存储的 TaskService 实例。"""
    return TaskService(storage=TaskStorage())


# ═══════════════════════════════════════════════════════════
# SimpleStateMachine — 状态转换全覆盖
# ═══════════════════════════════════════════════════════════

class TestSimpleStateMachineTransitions:
    """SimpleStateMachine 所有合法/非法转换全覆盖。

    状态机定义（6 种状态）:
    - PENDING → [RUNNING, PAUSED, COMPLETED, FAILED]
    - RUNNING → [COMPLETED, FAILED, EVALUATING, PAUSED]
    - EVALUATING → [COMPLETED, FAILED, RUNNING]
    - FAILED → [PENDING]
    - COMPLETED → [PENDING]
    - PAUSED → [PENDING, RUNNING, FAILED]
    """

    def setup_method(self) -> None:
        self.sm = SimpleStateMachine()

    # ── 合法转换（参数化覆盖全部 16 条边）─────────────────

    @pytest.mark.parametrize("from_s, to_s", [
        (TaskStatus.PENDING, TaskStatus.RUNNING),
        (TaskStatus.PENDING, TaskStatus.PAUSED),
        (TaskStatus.PENDING, TaskStatus.COMPLETED),
        (TaskStatus.PENDING, TaskStatus.FAILED),
        (TaskStatus.RUNNING, TaskStatus.COMPLETED),
        (TaskStatus.RUNNING, TaskStatus.FAILED),
        (TaskStatus.RUNNING, TaskStatus.EVALUATING),
        (TaskStatus.RUNNING, TaskStatus.PAUSED),
        (TaskStatus.EVALUATING, TaskStatus.COMPLETED),
        (TaskStatus.EVALUATING, TaskStatus.FAILED),
        (TaskStatus.EVALUATING, TaskStatus.RUNNING),
        (TaskStatus.FAILED, TaskStatus.PENDING),
        (TaskStatus.COMPLETED, TaskStatus.PENDING),
        (TaskStatus.PAUSED, TaskStatus.PENDING),
        (TaskStatus.PAUSED, TaskStatus.RUNNING),
        (TaskStatus.PAUSED, TaskStatus.FAILED),
    ])
    def test_valid_transition(self, from_s: TaskStatus, to_s: TaskStatus) -> None:
        """合法转换应成功。"""
        task = TaskModel(status=from_s)
        self.sm.transition(task, to_s)
        assert task.status == to_s

    # ── 非法转换（采样关键场景）────────────────────────────

    @pytest.mark.parametrize("from_s, to_s", [
        (TaskStatus.COMPLETED, TaskStatus.RUNNING),
        (TaskStatus.COMPLETED, TaskStatus.FAILED),
        (TaskStatus.COMPLETED, TaskStatus.EVALUATING),
        (TaskStatus.FAILED, TaskStatus.RUNNING),
        (TaskStatus.FAILED, TaskStatus.COMPLETED),
        (TaskStatus.PAUSED, TaskStatus.COMPLETED),
        (TaskStatus.PAUSED, TaskStatus.EVALUATING),
        (TaskStatus.EVALUATING, TaskStatus.PAUSED),
        (TaskStatus.RUNNING, TaskStatus.PENDING),
    ])
    def test_invalid_transition_raises(
        self, from_s: TaskStatus, to_s: TaskStatus,
    ) -> None:
        """非法转换应抛出 InvalidTransitionError。"""
        task = TaskModel(status=from_s)
        with pytest.raises(InvalidTransitionError) as exc_info:
            self.sm.transition(task, to_s)
        assert exc_info.value.from_status == from_s.value
        assert exc_info.value.to_status == to_s.value

    def test_can_transition_returns_bool(self) -> None:
        """can_transition 对合法/非法返回正确的布尔值。"""
        assert self.sm.can_transition(TaskStatus.PENDING, TaskStatus.RUNNING) is True
        assert self.sm.can_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING) is False
        assert self.sm.can_transition(TaskStatus.FAILED, TaskStatus.PENDING) is True
        assert self.sm.can_transition(TaskStatus.PAUSED, TaskStatus.EVALUATING) is False


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
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_start_task_success(self) -> None:
        """pending → running 成功。"""
        task = await self.svc.create_task(title="启动")
        result = await self.svc.start_task(task.id)
        assert result.status == TaskStatus.RUNNING
        assert result.started_at is not None

    @pytest.mark.asyncio
    async def test_start_task_sets_started_at(self) -> None:
        """启动任务设置 started_at。"""
        task = await self.svc.create_task(title="时间")
        assert task.started_at is None
        started = await self.svc.start_task(task.id)
        assert started.started_at is not None

    @pytest.mark.asyncio
    async def test_move_to_evaluating_success(self) -> None:
        """running → evaluating 成功。"""
        task = await self.svc.create_task(title="评估")
        await self.svc.start_task(task.id)
        result = await self.svc.move_to_evaluating(task.id)
        assert result.status == TaskStatus.EVALUATING

    @pytest.mark.asyncio
    async def test_complete_evaluation_passed(self) -> None:
        """evaluating → completed（通过）。"""
        task = await self.svc.create_task(title="通过")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)
        result = await self.svc.complete_evaluation(task.id, passed=True, result={"score": 0.95})
        assert result.status == TaskStatus.COMPLETED
        assert result.result == {"score": 0.95}
        assert result.completed_at is not None

    @pytest.mark.asyncio
    async def test_complete_evaluation_failed(self) -> None:
        """evaluating → failed（不通过）。"""
        task = await self.svc.create_task(title="不通过")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)
        result = await self.svc.complete_evaluation(task.id, passed=False)
        assert result.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_complete_evaluation_stores_history(self) -> None:
        """评估结果记录到 evaluation_history。"""
        task = await self.svc.create_task(title="历史")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)
        await self.svc.complete_evaluation(task.id, passed=True, result="OK")

        fetched = self.svc.get_task(task.id)
        history = fetched.metadata.get("evaluation_history", [])
        assert len(history) == 1
        assert history[0]["passed"] is True
        assert history[0]["data"] == "OK"

    @pytest.mark.asyncio
    async def test_pause_task_success(self) -> None:
        """running → paused 成功。"""
        task = await self.svc.create_task(title="暂停")
        await self.svc.start_task(task.id)
        result = await self.svc.pause_task(task.id)
        assert result.status == TaskStatus.PAUSED

    @pytest.mark.asyncio
    async def test_resume_task_success(self) -> None:
        """paused → running 成功。"""
        task = await self.svc.create_task(title="恢复")
        await self.svc.start_task(task.id)
        await self.svc.pause_task(task.id)
        result = await self.svc.resume_task(task.id)
        assert result.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_fail_task_with_error(self) -> None:
        """running → failed，带错误信息。"""
        task = await self.svc.create_task(title="失败")
        await self.svc.start_task(task.id)
        result = await self.svc.fail_task(task.id, error="出错了")
        assert result.status == TaskStatus.FAILED
        assert result.error == "出错了"

    @pytest.mark.asyncio
    async def test_fail_task_without_error(self) -> None:
        """running → failed，不带错误信息。"""
        task = await self.svc.create_task(title="静默失败")
        await self.svc.start_task(task.id)
        result = await self.svc.fail_task(task.id)
        assert result.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_full_lifecycle_pass(self) -> None:
        """完整生命周期：pending → running → evaluating → completed。"""
        task = await self.svc.create_task(title="全流程通过")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)
        result = await self.svc.complete_evaluation(task.id, passed=True)
        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_full_lifecycle_fail(self) -> None:
        """完整生命周期：pending → running → evaluating → failed。"""
        task = await self.svc.create_task(title="全流程失败")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)
        result = await self.svc.complete_evaluation(task.id, passed=False)
        assert result.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self) -> None:
        """非法状态转换抛出 InvalidTransitionError。"""
        task = await self.svc.create_task(title="非法")
        await self.svc.start_task(task.id)
        # running → running 非法
        with pytest.raises(InvalidTransitionError):
            await self.svc.start_task(task.id)

    @pytest.mark.asyncio
    async def test_task_not_found_raises_key_error(self) -> None:
        """操作不存在的任务抛出 KeyError。"""
        with pytest.raises(KeyError):
            await self.svc.start_task("不存在")

        with pytest.raises(KeyError):
            await self.svc.fail_task("不存在")

        with pytest.raises(KeyError):
            await self.svc.move_to_evaluating("不存在")


# ═══════════════════════════════════════════════════════════
# TaskService — reactivate_task
# ═══════════════════════════════════════════════════════════

class TestTaskServiceReactivate:
    """reactivate_task 测试（completed → pending）。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_reactivate_completed_task(self) -> None:
        """重新激活已完成任务。"""
        task = await self.svc.create_task(title="已完成")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)
        await self.svc.complete_evaluation(task.id, passed=True)
        assert self.svc.get_task(task.id).status == TaskStatus.COMPLETED

        result = await self.svc.reactivate_task(task.id)
        assert result.status == TaskStatus.PENDING
        assert result.completed_at == ""
        assert result.error == ""
        assert result.reject_count == 0

    @pytest.mark.asyncio
    async def test_reactivate_clears_pipeline_run_id(self) -> None:
        """重新激活清除 pipeline_run_id 并记录到 pipeline_history。"""
        task = await self.svc.create_task(title="带管道")
        await self.svc.start_task(task.id)
        await self.svc.bind_pipeline_run(task.id, "pipeline_001")
        await self.svc.move_to_evaluating(task.id)
        await self.svc.complete_evaluation(task.id, passed=True)

        result = await self.svc.reactivate_task(task.id)
        assert result.pipeline_run_id == ""
        history = result.metadata.get("pipeline_history", [])
        assert "pipeline_001" in history

    @pytest.mark.asyncio
    async def test_reactivate_with_message(self) -> None:
        """重新激活带追加需求消息。"""
        task = await self.svc.create_task(title="追加需求")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)
        await self.svc.complete_evaluation(task.id, passed=True)

        result = await self.svc.reactivate_task(task.id, message="增加新功能")
        reqs = result.metadata.get("reactivate_requirements", [])
        assert len(reqs) == 1
        assert reqs[0]["message"] == "增加新功能"
        assert reqs[0]["timestamp"] is not None

    @pytest.mark.asyncio
    async def test_reactivate_nonexistent_raises(self) -> None:
        """重新激活不存在的任务抛出 KeyError。"""
        with pytest.raises(KeyError):
            await self.svc.reactivate_task("不存在")


# ═══════════════════════════════════════════════════════════
# TaskService — reset_to_pending
# ═══════════════════════════════════════════════════════════

class TestTaskServiceResetToPending:
    """reset_to_pending 测试（强制重置）。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_reset_running_to_pending(self) -> None:
        """将 running 任务重置为 pending。"""
        task = await self.svc.create_task(title="运行中")
        await self.svc.start_task(task.id)

        result = await self.svc.reset_to_pending(task.id)
        assert result.status == TaskStatus.PENDING
        assert result.started_at == ""
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_reset_failed_to_pending(self) -> None:
        """将 failed 任务重置为 pending。"""
        task = await self.svc.create_task(title="失败")
        await self.svc.start_task(task.id)
        await self.svc.fail_task(task.id, error="崩溃")

        result = await self.svc.reset_to_pending(task.id)
        assert result.status == TaskStatus.PENDING
        assert result.error == ""

    @pytest.mark.asyncio
    async def test_reset_nonexistent_raises(self) -> None:
        """重置不存在的任务抛出 KeyError。"""
        with pytest.raises(KeyError):
            await self.svc.reset_to_pending("不存在")


# ═══════════════════════════════════════════════════════════
# TaskService — recover_to_completed
# ═══════════════════════════════════════════════════════════

class TestTaskServiceRecover:
    """recover_to_completed 测试（failed → completed 恢复）。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_recover_failed_task(self) -> None:
        """将 failed 任务恢复为 completed。"""
        task = await self.svc.create_task(title="恢复")
        await self.svc.start_task(task.id)
        await self.svc.fail_task(task.id, error="临时错误")

        result = await self.svc.recover_to_completed(task.id, result="已修复")
        assert result.status == TaskStatus.COMPLETED
        assert result.error is None
        assert result.result == "已修复"
        assert result.completed_at is not None

    @pytest.mark.asyncio
    async def test_recover_non_failed_raises(self) -> None:
        """对非 FAILED 状态调用 recover 抛出 ValueError。"""
        task = await self.svc.create_task(title="非失败")
        with pytest.raises(ValueError, match="FAILED"):
            await self.svc.recover_to_completed(task.id)

    @pytest.mark.asyncio
    async def test_recover_running_raises(self) -> None:
        """对 RUNNING 状态调用 recover 抛出 ValueError。"""
        task = await self.svc.create_task(title="运行中")
        await self.svc.start_task(task.id)
        with pytest.raises(ValueError, match="FAILED"):
            await self.svc.recover_to_completed(task.id)

    @pytest.mark.asyncio
    async def test_recover_completed_raises(self) -> None:
        """对 COMPLETED 状态调用 recover 抛出 ValueError。"""
        task = await self.svc.create_task(title="已完成")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)
        await self.svc.complete_evaluation(task.id, passed=True)
        with pytest.raises(ValueError, match="FAILED"):
            await self.svc.recover_to_completed(task.id)

    @pytest.mark.asyncio
    async def test_recover_nonexistent_raises(self) -> None:
        """恢复不存在的任务抛出 KeyError。"""
        with pytest.raises(KeyError):
            await self.svc.recover_to_completed("不存在")


# ═══════════════════════════════════════════════════════════
# TaskService — reject_task（打回重做）
# ═══════════════════════════════════════════════════════════

class TestTaskServiceReject:
    """reject_task 测试（evaluating → running 打回重做）。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_reject_once(self) -> None:
        """打回一次，回到 running。"""
        task = await self.svc.create_task(title="打回")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)

        result = await self.svc.reject_task(task.id, reason="质量不够")
        assert result.status == TaskStatus.RUNNING
        assert result.reject_count == 1
        assert "质量不够" in result.error
        assert "1/3" in result.error

    @pytest.mark.asyncio
    async def test_reject_without_reason(self) -> None:
        """打回不带原因。"""
        task = await self.svc.create_task(title="无原因打回")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)

        result = await self.svc.reject_task(task.id)
        assert result.status == TaskStatus.RUNNING
        assert "1/3" in result.error

    @pytest.mark.asyncio
    async def test_reject_exceeds_max_count(self) -> None:
        """打回次数超过上限，标记为 failed。"""
        task = await self.svc.create_task(title="超限打回")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)

        # 第 1 次打回
        await self.svc.reject_task(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.RUNNING

        # 回到 evaluating
        await self.svc.move_to_evaluating(task.id)

        # 第 2 次打回
        await self.svc.reject_task(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.RUNNING

        # 回到 evaluating
        await self.svc.move_to_evaluating(task.id)

        # 第 3 次打回（达到上限）
        result = await self.svc.reject_task(task.id, reason="最终拒绝", max_reject_count=3)
        assert result.status == TaskStatus.FAILED
        assert "最终拒绝" in result.error
        assert result.reject_count == 3

    @pytest.mark.asyncio
    async def test_reject_custom_max_count(self) -> None:
        """自定义最大打回次数。"""
        task = await self.svc.create_task(title="自定义上限")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)

        # max_reject_count=1，第 1 次就超限
        result = await self.svc.reject_task(task.id, max_reject_count=1)
        assert result.status == TaskStatus.FAILED
        assert result.reject_count == 1

    @pytest.mark.asyncio
    async def test_reject_nonexistent_raises(self) -> None:
        """打回不存在的任务抛出 KeyError。"""
        with pytest.raises(KeyError):
            await self.svc.reject_task("不存在")


# ═══════════════════════════════════════════════════════════
# TaskService — delete_task
# ═══════════════════════════════════════════════════════════

class TestTaskServiceDelete:
    """delete_task 测试（容器/非容器/子任务删除策略）。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self) -> None:
        """删除不存在的任务返回 False。"""
        result = await self.svc.delete_task("不存在")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_normal_task(self) -> None:
        """删除普通任务（根任务）— 清理工作空间 + 删除数据。"""
        task = await self.svc.create_task(title="普通任务")
        await self.svc.start_task(task.id)

        with patch.object(self.svc, "_cleanup_workspace") as mock_cleanup:
            result = await self.svc.delete_task(task.id)
        assert result is True
        assert self.svc.get_task(task.id) is None
        mock_cleanup.assert_called_once_with(task.id)

    @pytest.mark.asyncio
    async def test_delete_container_task_soft_delete(self) -> None:
        """删除容器任务 — 软删除（标记 failed + soft_deleted）。"""
        task = await self.svc.create_task(
            title="容器任务",
            metadata={"task_scope": "container"},
        )

        result = await self.svc.delete_task(task.id)
        assert result is True

        # 容器任务仍然存在（软删除）
        fetched = self.svc.get_task(task.id)
        assert fetched is not None
        assert fetched.status == TaskStatus.FAILED
        assert fetched.error == "已取消: 用户删除"
        assert fetched.metadata.get("soft_deleted") is True

    @pytest.mark.asyncio
    async def test_delete_container_cascades_children(self) -> None:
        """删除容器任务级联取消子任务。"""
        container = await self.svc.create_task(
            title="容器",
            metadata={"task_scope": "container"},
        )
        child = await self.svc.create_task(
            title="子任务",
            parent_task_id=container.id,
        )
        await self.svc.start_task(child.id)

        await self.svc.delete_task(container.id)

        # 子任务应被级联标记为 failed
        fetched_child = self.svc.get_task(child.id)
        assert fetched_child is not None
        assert fetched_child.status == TaskStatus.FAILED

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

        with patch.object(self.svc, "_cleanup_workspace") as mock_cleanup:
            await self.svc.delete_task(child.id)

        # 子任务已删除
        assert self.svc.get_task(child.id) is None
        # 不清理工作空间
        mock_cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_root_task_with_subtasks(self) -> None:
        """删除根任务时级联取消子任务。

        根任务被删除，活跃子任务被级联标记为 FAILED（不删除数据）。
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

        with patch.object(self.svc, "_cleanup_workspace"):
            await self.svc.delete_task(root.id)

        # 根任务被删除
        assert self.svc.get_task(root.id) is None
        # 子任务被级联标记为 FAILED（仍存在于存储中）
        fetched_c1 = self.svc.get_task(child1.id)
        fetched_c2 = self.svc.get_task(child2.id)
        assert fetched_c1 is not None
        assert fetched_c1.status == TaskStatus.FAILED
        assert fetched_c2 is not None
        assert fetched_c2.status == TaskStatus.FAILED


# ═══════════════════════════════════════════════════════════
# TaskService — cancel_task_cascade
# ═══════════════════════════════════════════════════════════

class TestTaskServiceCascadeCancel:
    """cancel_task_cascade 级联取消测试。"""

    def setup_method(self) -> None:
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
        assert self.svc.get_task(child1.id).status == TaskStatus.FAILED
        assert self.svc.get_task(child2.id).status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_cascade_skips_terminal_subtasks(self) -> None:
        """级联取消跳过终态子任务。"""
        parent = await self.svc.create_task(title="父任务")
        child_completed = await self.svc.create_task(
            title="已完成子任务", parent_task_id=parent.id,
        )
        child_active = await self.svc.create_task(
            title="活跃子任务", parent_task_id=parent.id,
        )
        # 手动完成一个子任务
        await self.svc.start_task(child_completed.id)
        await self.svc.move_to_evaluating(child_completed.id)
        await self.svc.complete_evaluation(child_completed.id, passed=True)
        await self.svc.start_task(child_active.id)

        count = await self.svc.cancel_task_cascade(parent.id, reason="测试")
        assert count == 1
        assert self.svc.get_task(child_completed.id).status == TaskStatus.COMPLETED
        assert self.svc.get_task(child_active.id).status == TaskStatus.FAILED

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
        assert self.svc.get_task(child.id).status == TaskStatus.FAILED
        assert self.svc.get_task(grandchild.id).status == TaskStatus.FAILED


# ═══════════════════════════════════════════════════════════
# TaskService — 绑定操作
# ═══════════════════════════════════════════════════════════

class TestTaskServiceBind:
    """bind_pipeline_run / bind_execution_record 测试。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_bind_pipeline_run(self) -> None:
        """绑定管道运行 ID。"""
        task = await self.svc.create_task(title="绑定管道")
        result = await self.svc.bind_pipeline_run(task.id, "pipeline_run_001")
        assert result.pipeline_run_id == "pipeline_run_001"

        # 持久化验证
        fetched = self.svc.get_task(task.id)
        assert fetched.pipeline_run_id == "pipeline_run_001"

    @pytest.mark.asyncio
    async def test_bind_execution_record(self) -> None:
        """绑定执行记录 ID。"""
        task = await self.svc.create_task(title="绑定记录")
        result = await self.svc.bind_execution_record(task.id, "record_001")
        assert result.execution_record_id == "record_001"

    @pytest.mark.asyncio
    async def test_bind_pipeline_nonexistent_raises(self) -> None:
        """绑定管道到不存在的任务抛出 KeyError。"""
        with pytest.raises(KeyError):
            await self.svc.bind_pipeline_run("不存在", "pipeline_001")

    @pytest.mark.asyncio
    async def test_bind_record_nonexistent_raises(self) -> None:
        """绑定记录到不存在的任务抛出 KeyError。"""
        with pytest.raises(KeyError):
            await self.svc.bind_execution_record("不存在", "record_001")


# ═══════════════════════════════════════════════════════════
# TaskService — force_transition / can_transition / get_valid_transitions
# ═══════════════════════════════════════════════════════════

class TestTaskServiceTransitionHelpers:
    """force_transition / can_transition / get_valid_transitions 测试。"""

    def setup_method(self) -> None:
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_force_transition_valid(self) -> None:
        """强制转换到合法状态。"""
        task = await self.svc.create_task(title="强制")
        result = await self.svc.force_transition(task.id, TaskStatus.RUNNING)
        assert result.status == TaskStatus.RUNNING

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
        assert "paused" in transitions
        assert "completed" in transitions
        assert "failed" in transitions

    @pytest.mark.asyncio
    async def test_get_valid_transitions_running(self) -> None:
        """获取 running 状态的有效转换列表。"""
        task = await self.svc.create_task(title="运行中转换")
        await self.svc.start_task(task.id)
        transitions = self.svc.get_valid_transitions(task.id)
        assert "completed" in transitions
        assert "failed" in transitions
        assert "evaluating" in transitions
        assert "paused" in transitions

    def test_get_valid_transitions_nonexistent(self) -> None:
        """获取不存在的任务的转换列表返回空。"""
        assert self.svc.get_valid_transitions("不存在") == []


# ═══════════════════════════════════════════════════════════
# TaskService — get_root_task_id / get_progress
# ═══════════════════════════════════════════════════════════

class TestTaskServiceRootAndProgress:
    """get_root_task_id / get_progress 测试。"""

    def setup_method(self) -> None:
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

    @pytest.mark.asyncio
    async def test_get_progress_no_subtasks(self) -> None:
        """无子任务时进度为 0。"""
        task = await self.svc.create_task(title="无子任务")
        assert self.svc.get_progress(task.id) == 0.0

    @pytest.mark.asyncio
    async def test_get_progress_partial(self) -> None:
        """部分子任务完成。"""
        parent = await self.svc.create_task(title="父")
        c1 = await self.svc.create_task(title="C1", parent_task_id=parent.id)
        c2 = await self.svc.create_task(title="C2", parent_task_id=parent.id)

        # 完成 c1
        await self.svc.start_task(c1.id)
        await self.svc.move_to_evaluating(c1.id)
        await self.svc.complete_evaluation(c1.id, passed=True)

        assert self.svc.get_progress(parent.id) == 50.0

    @pytest.mark.asyncio
    async def test_get_progress_all_completed(self) -> None:
        """所有子任务完成，进度 100%。"""
        parent = await self.svc.create_task(title="父")
        c1 = await self.svc.create_task(title="C1", parent_task_id=parent.id)
        c2 = await self.svc.create_task(title="C2", parent_task_id=parent.id)

        for c in [c1, c2]:
            await self.svc.start_task(c.id)
            await self.svc.move_to_evaluating(c.id)
            await self.svc.complete_evaluation(c.id, passed=True)

        assert self.svc.get_progress(parent.id) == 100.0

    def test_get_progress_nonexistent_parent(self) -> None:
        """不存在的父任务进度为 0。"""
        assert self.svc.get_progress("不存在") == 0.0


# ═══════════════════════════════════════════════════════════
# TaskService — save_task
# ═══════════════════════════════════════════════════════════

class TestTaskServiceSave:
    """save_task 测试。"""

    def setup_method(self) -> None:
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
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_list_all_default(self) -> None:
        """默认返回最多 50 条。"""
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
        svc = TaskService(storage=TaskStorage(), event_bus=mock_bus)

        task = await svc.create_task(title="事件测试")
        await svc.start_task(task.id)
        # 不崩溃即可


# ═══════════════════════════════════════════════════════════
# TaskService — _is_child_of_container
# ═══════════════════════════════════════════════════════════

class TestTaskServiceIsChildOfContainer:
    """_is_child_of_container 测试。"""

    def setup_method(self) -> None:
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
        self.svc = _make_service()

    @pytest.mark.asyncio
    async def test_multiple_transitions_sequential(self) -> None:
        """连续多次状态转换。"""
        task = await self.svc.create_task(title="连续转换")
        # pending -> running
        await self.svc.start_task(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.RUNNING

        # running -> paused
        await self.svc.pause_task(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.PAUSED

        # paused -> running
        await self.svc.resume_task(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.RUNNING

        # running -> evaluating
        await self.svc.move_to_evaluating(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.EVALUATING

        # evaluating -> completed
        await self.svc.complete_evaluation(task.id, passed=True)
        assert self.svc.get_task(task.id).status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_reactivate_and_recomplete(self) -> None:
        """重新激活后再次完成。"""
        task = await self.svc.create_task(title="再完成")
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)
        await self.svc.complete_evaluation(task.id, passed=True)

        # 重新激活
        await self.svc.reactivate_task(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.PENDING

        # 再次走完生命周期
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)
        result = await self.svc.complete_evaluation(task.id, passed=True)
        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_failed_to_pending_retry(self) -> None:
        """失败后重试（failed → pending → running → completed）。"""
        task = await self.svc.create_task(title="重试")
        await self.svc.start_task(task.id)
        await self.svc.fail_task(task.id, error="第一次失败")

        # 重置为 pending
        await self.svc.reset_to_pending(task.id)
        assert self.svc.get_task(task.id).status == TaskStatus.PENDING

        # 重新执行
        await self.svc.start_task(task.id)
        await self.svc.move_to_evaluating(task.id)
        result = await self.svc.complete_evaluation(task.id, passed=True)
        assert result.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_recover_and_reactivate_interaction(self) -> None:
        """recover_to_completed 后可以 reactivate。"""
        task = await self.svc.create_task(title="恢复后重新激活")
        await self.svc.start_task(task.id)
        await self.svc.fail_task(task.id, error="临时错误")

        # 恢复为 completed
        await self.svc.recover_to_completed(task.id, result="已修复")
        assert self.svc.get_task(task.id).status == TaskStatus.COMPLETED

        # 重新激活
        result = await self.svc.reactivate_task(task.id, message="新需求")
        assert result.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_create_task_with_all_options(self) -> None:
        """创建任务传入所有可选参数。"""
        task = await self.svc.create_task(
            title="完整任务",
            description="详细描述",
            parent_task_id="parent_001",
            parent_pipeline_id="pipeline_001",
            metadata={"custom_key": "custom_value"},
            agent_name="灵汐",
            priority=TaskPriority.CRITICAL,
        )
        assert task.title == "完整任务"
        assert task.description == "详细描述"
        assert task.parent_task_id == "parent_001"
        assert task.agent_name == "灵汐"
        assert task.priority == TaskPriority.CRITICAL
        assert task.metadata.get("custom_key") == "custom_value"
