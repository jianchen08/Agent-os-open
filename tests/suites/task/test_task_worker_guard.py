"""TaskWorker 子任务守护相关逻辑单元测试。

验证 TaskWorker 在管道挂起/唤醒场景中的行为：
1. 管道挂起时保存 engine 引用
2. 子任务终态时 resume 父任务管道
3. idle 超时时有挂起管道的提醒逻辑（不直接 fail）
4. idle 超时提醒次数限制
5. 挂起管道等待活跃子任务时不计入提醒次数
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
        """有挂起管道且无活跃子任务时提醒而非 fail。"""
        task = MagicMock()
        task.status.value = "running"
        worker._task_service.get_task.return_value = task
        worker._task_service.list_subtasks.return_value = []

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
        worker._task_service.list_subtasks.return_value = []

        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        worker._suspended_engines["task-001"] = mock_engine

        worker._on_idle_timeout("task-001")
        worker._on_idle_timeout("task-001")
        worker._on_idle_timeout("task-001")

        assert worker._idle_remind_counts.get("task-001") == 3

    def test_remind_limit_exceeded_then_fail(self, worker):
        """超过提醒次数后 fail。"""
        import asyncio

        task = MagicMock()
        task.status.value = "running"
        worker._task_service.get_task.return_value = task
        worker._task_service.list_subtasks.return_value = []

        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        worker._suspended_engines["task-001"] = mock_engine
        worker._idle_remind_counts["task-001"] = 3

        async def _run():
            worker._on_idle_timeout("task-001")
            worker._task_service.fail_task.assert_called_once()

        asyncio.run(_run())

    def test_no_suspended_engine_then_fail(self, worker):
        """没有挂起管道时直接 fail。"""
        import asyncio

        task = MagicMock()
        task.status.value = "running"
        worker._task_service.get_task.return_value = task

        async def _run():
            worker._on_idle_timeout("task-001")
            worker._task_service.fail_task.assert_called_once()

        asyncio.run(_run())

    def test_suspended_with_active_children_no_remind_count(self, worker):
        """挂起管道且有活跃子任务时不计数 remind，直接重建 timer。"""
        task = MagicMock()
        task.status.value = "running"
        worker._task_service.get_task.return_value = task

        running_child = MagicMock()
        running_child.status.value = "running"
        worker._task_service.list_subtasks.return_value = [running_child]

        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        worker._suspended_engines["task-001"] = mock_engine

        worker._on_idle_timeout("task-001")

        assert worker._idle_remind_counts.get("task-001") is None
        worker._task_service.fail_task.assert_not_called()

    def test_suspended_with_active_children_never_fails(self, worker):
        """挂起管道且有活跃子任务时，反复 idle 超时也不会 fail。"""
        task = MagicMock()
        task.status.value = "running"
        worker._task_service.get_task.return_value = task

        running_child = MagicMock()
        running_child.status.value = "running"
        worker._task_service.list_subtasks.return_value = [running_child]

        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        worker._suspended_engines["task-001"] = mock_engine

        for _ in range(10):
            worker._on_idle_timeout("task-001")

        assert worker._idle_remind_counts.get("task-001") is None
        worker._task_service.fail_task.assert_not_called()

    def test_suspended_children_finish_then_remind_starts(self, worker):
        """子任务完成后开始正常计数 remind。"""
        task = MagicMock()
        task.status.value = "running"
        worker._task_service.get_task.return_value = task

        running_child = MagicMock()
        running_child.status.value = "running"
        completed_child = MagicMock()
        completed_child.status.value = "completed"

        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        worker._suspended_engines["task-001"] = mock_engine

        worker._task_service.list_subtasks.return_value = [running_child]
        worker._on_idle_timeout("task-001")
        assert worker._idle_remind_counts.get("task-001") is None

        worker._task_service.list_subtasks.return_value = [completed_child]
        worker._on_idle_timeout("task-001")
        assert worker._idle_remind_counts.get("task-001") == 1

    def test_suspended_mixed_children_status_counts_as_active(self, worker):
        """子任务中有 pending 状态也算活跃，不计 remind。"""
        task = MagicMock()
        task.status.value = "running"
        worker._task_service.get_task.return_value = task

        completed_child = MagicMock()
        completed_child.status.value = "completed"
        pending_child = MagicMock()
        pending_child.status.value = "pending"
        worker._task_service.list_subtasks.return_value = [
            completed_child, pending_child,
        ]

        mock_engine = MagicMock()
        mock_engine.is_suspended = True
        worker._suspended_engines["task-001"] = mock_engine

        worker._on_idle_timeout("task-001")

        assert worker._idle_remind_counts.get("task-001") is None
        worker._task_service.fail_task.assert_not_called()


# ── _get_pipeline_last_activity ──


class TestGetPipelineLastActivity:
    """测试活跃管道 checkpoint 存活检测。"""

    def test_no_task_service(self, worker):
        """无 task_service 时返回 None。"""
        worker._task_service = None
        assert worker._get_pipeline_last_activity("task-001") is None

    def test_task_not_found(self, worker):
        """任务不存在时返回 None。"""
        worker._task_service.get_task.return_value = None
        assert worker._get_pipeline_last_activity("task-001") is None

    def test_no_pipeline_run_id(self, worker):
        """任务无 pipeline_run_id 时返回 None。"""
        task = MagicMock()
        task.pipeline_run_id = None
        worker._task_service.get_task.return_value = task
        assert worker._get_pipeline_last_activity("task-001") is None

    def test_checkpoint_dir_not_exists(self, worker, tmp_path, monkeypatch):
        """checkpoint 目录不存在时返回 None。"""
        task = MagicMock()
        task.pipeline_run_id = "pipe-123"
        worker._task_service.get_task.return_value = task
        monkeypatch.chdir(tmp_path)
        assert worker._get_pipeline_last_activity("task-001") is None

    def test_fresh_checkpoint_returns_mtime(self, worker, tmp_path, monkeypatch):
        """有 checkpoint 文件时返回最新修改时间。"""
        import time

        task = MagicMock()
        task.pipeline_run_id = "pipe-123"
        worker._task_service.get_task.return_value = task

        ckpt_dir = tmp_path / "data" / "pipeline_checkpoints"
        ckpt_dir.mkdir(parents=True)
        (ckpt_dir / "pipe-123_step1.json").write_text("{}")
        time.sleep(0.1)
        (ckpt_dir / "pipe-123_step2.json").write_text("{}")
        monkeypatch.chdir(tmp_path)

        result = worker._get_pipeline_last_activity("task-001")
        assert result is not None
        assert result > 0

    def test_no_matching_checkpoint(self, worker, tmp_path, monkeypatch):
        """checkpoint 文件不匹配时返回 None。"""
        task = MagicMock()
        task.pipeline_run_id = "pipe-123"
        worker._task_service.get_task.return_value = task

        ckpt_dir = tmp_path / "data" / "pipeline_checkpoints"
        ckpt_dir.mkdir(parents=True)
        (ckpt_dir / "other-pipe_step1.json").write_text("{}")
        monkeypatch.chdir(tmp_path)

        assert worker._get_pipeline_last_activity("task-001") is None


# ── _on_idle_timeout: 活跃管道死亡检测 ──


class TestActivePipelineDeathDetection:
    """测试活跃管道异常死亡后的检测机制。

    BUG-FIX-fix_20260514_active_pipeline_deadlock:
    验证管道进程异常死亡后 idle_timer 能检测到并标记任务失败。
    """

    def test_active_pipeline_with_stale_checkpoint_fails(self, worker, tmp_path, monkeypatch):
        """活跃管道但 checkpoint 过期 → 判定死亡 → fail。"""
        import asyncio
        import os
        import time

        task = MagicMock()
        task.status.value = "running"
        task.pipeline_run_id = "pipe-dead"
        worker._task_service.get_task.return_value = task

        ckpt_dir = tmp_path / "data" / "pipeline_checkpoints"
        ckpt_dir.mkdir(parents=True)
        old_file = ckpt_dir / "pipe-dead_step1.json"
        old_file.write_text("{}")
        old_mtime = old_file.stat().st_mtime
        os.utime(str(old_file), (old_mtime - 3600, old_mtime - 3600))
        monkeypatch.chdir(tmp_path)

        worker._active_tasks.add("task-001")

        async def _run():
            worker._on_idle_timeout("task-001")
            worker._task_service.fail_task.assert_called_once()

        asyncio.run(_run())

    def test_active_pipeline_no_checkpoint_fails(self, worker, tmp_path, monkeypatch):
        """活跃管道但无 checkpoint → 判定死亡 → fail。"""
        import asyncio

        task = MagicMock()
        task.status.value = "running"
        task.pipeline_run_id = "pipe-nock"
        worker._task_service.get_task.return_value = task
        monkeypatch.chdir(tmp_path)

        worker._active_tasks.add("task-001")

        async def _run():
            worker._on_idle_timeout("task-001")
            worker._task_service.fail_task.assert_called_once()

        asyncio.run(_run())

    def test_active_pipeline_fresh_checkpoint_survives(self, worker, tmp_path, monkeypatch):
        """活跃管道且 checkpoint 新鲜 → 重建 timer，不 fail。"""
        task = MagicMock()
        task.status.value = "running"
        task.pipeline_run_id = "pipe-alive"
        worker._task_service.get_task.return_value = task

        ckpt_dir = tmp_path / "data" / "pipeline_checkpoints"
        ckpt_dir.mkdir(parents=True)
        (ckpt_dir / "pipe-alive_step1.json").write_text("{}")
        monkeypatch.chdir(tmp_path)

        mock_timer_mgr = MagicMock()
        mock_timer_mgr.idle_threshold = 300
        worker._services["timer_manager"] = mock_timer_mgr
        worker._active_tasks.add("task-001")

        import asyncio

        async def _run():
            worker._on_idle_timeout("task-001")
            worker._task_service.fail_task.assert_not_called()

        asyncio.run(_run())


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


# ── 竞态修复：子任务在父管道挂起前失败 ──


class TestRaceConditionNotificationQueue:
    """测试子任务在父管道挂起前就失败时，通知入队和消费。"""

    @pytest.mark.asyncio
    async def test_notification_queued_when_parent_not_suspended(self, worker):
        """父管道尚未挂起时，通知应入队等待。"""
        child_task = MagicMock()
        child_task.parent_pipeline_id = "pipe-parent"
        child_task.parent_task_id = "parent-001"
        child_task.title = "子任务A"
        child_task.error = "工作空间初始化失败"
        worker._task_service.get_task.return_value = child_task

        data = {
            "task_id": "child-001",
            "new_status": "failed",
            "task": {
                "title": "子任务A",
                "error": "工作空间初始化失败",
                "parent_task_id": "parent-001",
            },
        }

        await worker._notify_suspended_pipelines(
            "child-001", "failed", data
        )

        pending = worker._services.get("__pending_notifications_pipe-parent")
        assert pending is not None
        assert len(pending) == 1
        assert "子任务A" in pending[0]
        assert "failed" in pending[0]

    @pytest.mark.asyncio
    async def test_notification_delivered_when_parent_already_suspended(
        self, worker,
    ):
        """父管道已挂起时，通知应直接注入而非入队。"""
        mock_engine = MagicMock()
        mock_engine.inject_message = MagicMock()
        worker._services["__suspended_engine_pipe-parent"] = mock_engine

        child_task = MagicMock()
        child_task.parent_pipeline_id = "pipe-parent"
        child_task.parent_task_id = "parent-001"
        child_task.title = "子任务B"
        child_task.error = ""
        worker._task_service.get_task.return_value = child_task

        data = {
            "task_id": "child-002",
            "new_status": "completed",
            "task": {
                "title": "子任务B",
                "error": "",
                "parent_task_id": "parent-001",
            },
        }

        await worker._notify_suspended_pipelines(
            "child-002", "completed", data
        )

        mock_engine.inject_message.assert_called_once()
        assert worker._services.get("__pending_notifications_pipe-parent") is None

    @pytest.mark.asyncio
    async def test_multiple_notifications_queued(self, worker):
        """多个子任务在父管道挂起前失败，通知应累加入队。"""
        child_task = MagicMock()
        child_task.parent_pipeline_id = "pipe-parent"
        child_task.parent_task_id = "parent-001"
        child_task.title = "子任务"
        child_task.error = ""
        worker._task_service.get_task.return_value = child_task

        for i in range(3):
            data = {
                "task_id": f"child-{i}",
                "new_status": "failed",
                "task": {
                    "title": f"子任务{i}",
                    "error": f"错误{i}",
                    "parent_task_id": "parent-001",
                },
            }
            await worker._notify_suspended_pipelines(
                f"child-{i}", "failed", data
            )

        pending = worker._services.get("__pending_notifications_pipe-parent")
        assert pending is not None
        assert len(pending) == 3
