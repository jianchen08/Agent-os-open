"""TaskWorker 子任务守护相关逻辑单元测试。

验证 TaskWorker 在管道挂起/唤醒场景中的行为：
1. 管道挂起时保存 engine 引用
2. 子任务终态时 resume 父任务管道
3. idle 超时时有挂起管道的提醒逻辑（不直接 fail）
4. idle 超时提醒次数限制
5. _find_parent_task_id 辅助方法
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infrastructure.task_worker import TaskWorker


@pytest.fixture
def worker() -> TaskWorker:
    """创建 TaskWorker 实例（不启动）。"""
    task_service = MagicMock()
    return TaskWorker(
        task_service=task_service,
        plugin_registry=MagicMock(),
        input_route_table=MagicMock(),
        output_route_table=MagicMock(),
        services={"task_service": task_service},
        event_bus=MagicMock(),
    )


# ── _find_parent_task_id ──


class TestFindParentTaskId:
    """测试从事件数据中提取父任务 ID。"""

    def test_dict_task_with_parent(self, worker):
        """事件数据中 task 为字典，有 parent_task_id。"""
        data = {"task": {"parent_task_id": "parent-001", "title": "子任务"}}
        assert worker._find_parent_task_id(data) == "parent-001"

    def test_dict_task_without_parent(self, worker):
        """事件数据中 task 为字典，无 parent_task_id。"""
        data = {"task": {"title": "根任务"}}
        assert worker._find_parent_task_id(data) is None

    def test_object_task_with_parent(self, worker):
        """事件数据中 task 为对象，有 parent_task_id 属性。"""
        task = MagicMock()
        task.parent_task_id = "parent-002"
        data = {"task": task}
        assert worker._find_parent_task_id(data) == "parent-002"

    def test_no_task_key(self, worker):
        """事件数据中无 task 键。"""
        data = {"task_id": "xxx", "new_status": "completed"}
        assert worker._find_parent_task_id(data) is None

    def test_task_is_none(self, worker):
        """事件数据中 task 为 None。"""
        data = {"task": None}
        assert worker._find_parent_task_id(data) is None


# ── _try_resume_engine ──


class TestTryResumeEngine:
    """测试管道唤醒逻辑。"""

    def test_no_suspended_engine(self, worker):
        """没有挂起的 engine 时不报错。"""
        worker._try_resume_engine("nonexistent-task")

    def test_resume_suspended_engine(self, worker):
        """挂起的 engine 被正确 resume。"""
        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        mock_engine.resume = AsyncMock()
        worker._suspended_engines["task-001"] = mock_engine

        worker._try_resume_engine("task-001")

    def test_resume_not_suspended_engine(self, worker):
        """engine 存在但未挂起时不调 resume。"""
        mock_engine = MagicMock()
        mock_engine.is_suspended = False
        worker._suspended_engines["task-001"] = mock_engine

        worker._try_resume_engine("task-001")


# ── _on_idle_timeout ──


class TestOnIdleTimeout:
    """测试 idle 超时回调。"""

    def test_no_task_service_logs_warning(self, worker):
        """无 task_service 时记录警告并返回。"""
        worker._task_service = None
        worker._on_idle_timeout("task-001")

    def test_task_not_found(self, worker):
        """任务不存在时直接返回。"""
        worker._task_service.get_task.return_value = None
        worker._on_idle_timeout("task-001")

    def test_task_not_running(self, worker):
        """任务不在 running 状态时直接返回。"""
        task = MagicMock()
        task.status.value = "completed"
        worker._task_service.get_task.return_value = task
        worker._on_idle_timeout("task-001")

    def test_suspended_engine_reminds_instead_of_fail(self, worker):
        """有挂起管道时提醒而非 fail。"""
        task = MagicMock()
        task.status.value = "running"
        worker._task_service.get_task.return_value = task

        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        mock_engine.resume = AsyncMock()
        worker._suspended_engines["task-001"] = mock_engine

        worker._on_idle_timeout("task-001")

        assert worker._idle_remind_counts.get("task-001") == 1
        worker._task_service.fail_task.assert_not_called()

    def test_remind_counter_increments(self, worker):
        """提醒计数器递增。"""
        task = MagicMock()
        task.status.value = "running"
        worker._task_service.get_task.return_value = task

        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        worker._suspended_engines["task-001"] = mock_engine

        worker._on_idle_timeout("task-001")
        worker._on_idle_timeout("task-001")
        worker._on_idle_timeout("task-001")

        assert worker._idle_remind_counts.get("task-001") == 3

    def test_remind_limit_exceeded_then_fail(self, worker):
        """超过提醒次数后 fail。"""
        task = MagicMock()
        task.status.value = "running"
        worker._task_service.get_task.return_value = task

        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        worker._suspended_engines["task-001"] = mock_engine
        worker._idle_remind_counts["task-001"] = 3

        worker._on_idle_timeout("task-001")

        worker._task_service.fail_task.assert_called_once()

    def test_no_suspended_engine_then_fail(self, worker):
        """没有挂起管道时直接 fail。"""
        task = MagicMock()
        task.status.value = "running"
        worker._task_service.get_task.return_value = task

        worker._on_idle_timeout("task-001")

        worker._task_service.fail_task.assert_called_once()


# ── _on_task_state_changed ──


class TestOnTaskStateChanged:
    """测试任务状态变更事件处理。"""

    @pytest.mark.asyncio
    async def test_child_completed_resumes_parent(self, worker):
        """子任务完成时唤醒父任务管道。"""
        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        mock_engine.resume = AsyncMock()
        worker._suspended_engines["parent-001"] = mock_engine

        event = MagicMock()
        event.data = {
            "task_id": "child-001",
            "new_status": "completed",
            "task": {"parent_task_id": "parent-001", "title": "子任务"},
        }

        await worker._on_task_state_changed(event)

    @pytest.mark.asyncio
    async def test_child_failed_resumes_parent(self, worker):
        """子任务失败时也唤醒父任务管道。"""
        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        mock_engine.resume = AsyncMock()
        worker._suspended_engines["parent-001"] = mock_engine

        event = MagicMock()
        event.data = {
            "task_id": "child-001",
            "new_status": "failed",
            "task": {"parent_task_id": "parent-001", "title": "子任务"},
        }

        await worker._on_task_state_changed(event)

    @pytest.mark.asyncio
    async def test_non_terminal_status_no_resume(self, worker):
        """非终态状态不触发 resume。"""
        mock_engine = MagicMock()
        worker._suspended_engines["parent-001"] = mock_engine

        event = MagicMock()
        event.data = {
            "task_id": "child-001",
            "new_status": "running",
            "task": {"parent_task_id": "parent-001"},
        }

        await worker._on_task_state_changed(event)

        mock_engine.resume.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_parent_no_resume(self, worker):
        """根任务终态不触发 resume（无父任务）。"""
        mock_engine = MagicMock()
        worker._suspended_engines["parent-001"] = mock_engine

        event = MagicMock()
        event.data = {
            "task_id": "child-001",
            "new_status": "completed",
            "task": {"title": "根任务"},
        }

        await worker._on_task_state_changed(event)

        mock_engine.resume.assert_not_called()

    @pytest.mark.asyncio
    async def test_parent_not_suspended_no_crash(self, worker):
        """父任务没有挂起管道时不报错。"""
        event = MagicMock()
        event.data = {
            "task_id": "child-001",
            "new_status": "completed",
            "task": {"parent_task_id": "parent-001"},
        }

        await worker._on_task_state_changed(event)
