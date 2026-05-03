"""task_manage 单元测试 — 各操作（Mock TaskService）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tasks.types import TaskModel, TaskStatus
from tools.builtin.task_manage import task_manage_func


# ── 辅助 ──────────────────────────────────────────────


def _make_task(
    task_id: str = "task_001",
    title: str = "Test Task",
    status: TaskStatus = TaskStatus.RUNNING,
    **kwargs: Any,
) -> TaskModel:
    """构造测试任务。"""
    return TaskModel(
        id=task_id,
        title=title,
        status=status,
        **kwargs,
    )


def _mock_task_service(tasks: dict[str, TaskModel] | None = None) -> MagicMock:
    """创建 Mock TaskService。

    Args:
        tasks: 预设任务字典 {task_id: TaskModel}

    Returns:
        配置好的 Mock TaskService
    """
    if tasks is None:
        tasks = {}

    svc = MagicMock()

    def get_task(task_id: str) -> TaskModel | None:
        return tasks.get(task_id)

    svc.get_task.side_effect = get_task
    svc._storage = MagicMock()

    def save(task: TaskModel) -> None:
        tasks[task.id] = task

    svc._storage.save.side_effect = save

    return svc


def _reset_task_service_singleton():
    """重置 task_manage 模块的 TaskService 单例缓存，确保 mock 生效。"""
    import tools.builtin.task_manage as tm
    tm._task_service_instance = None


# ── action=get ─────────────────────────────────────────


class TestActionGet:
    """get 操作测试。"""

    @patch("tools.builtin.task_manage._get_task_service")
    def test_get_existing_task(self, mock_get_svc: MagicMock) -> None:
        """获取存在的任务。"""
        _reset_task_service_singleton()
        task = _make_task()
        mock_svc = _mock_task_service({"task_001": task})
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "get", "task_id": "task_001"})
        assert result["success"] is True
        assert result["task"]["id"] == "task_001"

    @patch("tools.builtin.task_manage._get_task_service")
    def test_get_nonexistent_task(self, mock_get_svc: MagicMock) -> None:
        """获取不存在的任务。"""
        _reset_task_service_singleton()
        mock_svc = _mock_task_service()
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "get", "task_id": "nonexistent"})
        assert result["success"] is False
        assert result["error_code"] == "TASK_NOT_FOUND"

    def test_get_missing_task_id(self) -> None:
        """缺少 task_id。"""
        result = task_manage_func({"action": "get"})
        assert result["success"] is False
        assert result["error_code"] == "MISSING_TASK_ID"


# ── action=list ────────────────────────────────────────


class TestActionList:
    """list 操作测试。"""

    @patch("tools.builtin.task_manage._get_task_service")
    def test_list_all(self, mock_get_svc: MagicMock) -> None:
        """列出所有任务。"""
        _reset_task_service_singleton()
        task1 = _make_task(task_id="t1", status=TaskStatus.RUNNING)
        task2 = _make_task(task_id="t2", status=TaskStatus.PENDING)
        mock_svc = _mock_task_service({"t1": task1, "t2": task2})

        def list_by_status(status: TaskStatus) -> list[TaskModel]:
            return [t for t in [task1, task2] if t.status == status]

        mock_svc.list_by_status.side_effect = list_by_status
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "list"})
        assert result["success"] is True
        assert result["count"] == 2

    @patch("tools.builtin.task_manage._get_task_service")
    def test_list_by_status(self, mock_get_svc: MagicMock) -> None:
        """按状态列出任务。"""
        _reset_task_service_singleton()
        task1 = _make_task(task_id="t1", status=TaskStatus.RUNNING)
        mock_svc = _mock_task_service({"t1": task1})

        def list_by_status(status: TaskStatus) -> list[TaskModel]:
            if status == TaskStatus.RUNNING:
                return [task1]
            return []

        mock_svc.list_by_status.side_effect = list_by_status
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "list", "status_filter": "running"})
        assert result["success"] is True
        assert result["count"] == 1


# ── action=status ──────────────────────────────────────


class TestActionStatus:
    """status 操作测试。"""

    @patch("tools.builtin.task_manage._get_task_service")
    def test_status_existing(self, mock_get_svc: MagicMock) -> None:
        """获取任务状态。"""
        _reset_task_service_singleton()
        task = _make_task()
        mock_svc = _mock_task_service({"task_001": task})
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "status", "task_id": "task_001"})
        assert result["success"] is True
        assert result["status"] == "running"


# ── action=pause ───────────────────────────────────────


class TestActionPause:
    """pause 操作测试。"""

    @patch("tools.builtin.task_manage._get_task_service")
    def test_pause_running_task(self, mock_get_svc: MagicMock) -> None:
        """暂停运行中的任务。"""
        _reset_task_service_singleton()
        task = _make_task(status=TaskStatus.PAUSED)
        mock_svc = _mock_task_service()
        mock_svc.pause_task.return_value = task
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "pause", "task_id": "task_001"})
        assert result["success"] is True
        assert result["status"] == "paused"

    @patch("tools.builtin.task_manage._get_task_service")
    def test_pause_invalid_transition(self, mock_get_svc: MagicMock) -> None:
        """暂停不支持的状态。"""
        _reset_task_service_singleton()
        from tasks.state_machine import InvalidTransitionError

        mock_svc = _mock_task_service()
        mock_svc.pause_task.side_effect = InvalidTransitionError(
            TaskStatus.COMPLETED, TaskStatus.PAUSED
        )
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "pause", "task_id": "task_001"})
        assert result["success"] is False
        assert result["error_code"] == "INVALID_TRANSITION"


# ── action=resume ──────────────────────────────────────


class TestActionResume:
    """resume 操作测试。"""

    @patch("tools.builtin.task_manage._get_task_service")
    def test_resume_paused_task(self, mock_get_svc: MagicMock) -> None:
        """恢复暂停的任务。"""
        _reset_task_service_singleton()
        task = _make_task(status=TaskStatus.RUNNING)
        mock_svc = _mock_task_service()
        mock_svc.resume_task.return_value = task
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "resume", "task_id": "task_001"})
        assert result["success"] is True
        assert result["status"] == "running"


# ── action=cancel ──────────────────────────────────────


class TestActionCancel:
    """cancel 操作测试。"""

    @patch("tools.builtin.task_manage._get_task_service")
    def test_cancel_with_reason(self, mock_get_svc: MagicMock) -> None:
        """带原因取消任务。"""
        _reset_task_service_singleton()
        task = _make_task(status=TaskStatus.FAILED)
        mock_svc = _mock_task_service()
        mock_svc.fail_task.return_value = task
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({
            "action": "cancel",
            "task_id": "task_001",
            "reason": "测试取消",
        })
        assert result["success"] is True
        mock_svc.fail_task.assert_called_once_with("task_001", error="测试取消")

    @patch("tools.builtin.task_manage._get_task_service")
    def test_cancel_default_reason(self, mock_get_svc: MagicMock) -> None:
        """取消任务默认原因。"""
        _reset_task_service_singleton()
        task = _make_task(status=TaskStatus.FAILED)
        mock_svc = _mock_task_service()
        mock_svc.fail_task.return_value = task
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "cancel", "task_id": "task_001"})
        assert result["success"] is True
        mock_svc.fail_task.assert_called_once_with("task_001", error="用户取消")


# ── action=retry ───────────────────────────────────────


class TestActionRetry:
    """retry 操作测试。"""

    @patch("tools.builtin.task_manage._get_task_service")
    def test_retry_failed_task(self, mock_get_svc: MagicMock) -> None:
        """重试失败任务。"""
        _reset_task_service_singleton()
        failed_task = _make_task(status=TaskStatus.FAILED)
        running_task = _make_task(status=TaskStatus.RUNNING)
        mock_svc = _mock_task_service({"task_001": failed_task})
        mock_svc.start_task.return_value = running_task
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "retry", "task_id": "task_001"})
        assert result["success"] is True
        assert result["status"] == "running"

    @patch("tools.builtin.task_manage._get_task_service")
    def test_retry_non_failed_task(self, mock_get_svc: MagicMock) -> None:
        """重试非失败任务。"""
        _reset_task_service_singleton()
        task = _make_task(status=TaskStatus.RUNNING)
        mock_svc = _mock_task_service({"task_001": task})
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "retry", "task_id": "task_001"})
        assert result["success"] is False
        assert result["error_code"] == "INVALID_STATUS"


# ── action=inject ──────────────────────────────────────


class TestActionInject:
    """inject 操作测试。"""

    @patch("tools.builtin.task_manage._get_task_service")
    def test_inject_success(self, mock_get_svc: MagicMock) -> None:
        """成功注入消息。"""
        _reset_task_service_singleton()
        task = _make_task()
        mock_svc = _mock_task_service({"task_001": task})
        mock_get_svc.return_value = mock_svc

        # push 是异步方法，需要返回 coroutine
        async def mock_push(message):
            return True

        mock_queue = MagicMock()
        mock_queue.push = mock_push

        result = task_manage_func({
            "action": "inject",
            "task_id": "task_001",
            "message": "请检查进度",
            "session_id": "session_001",
            "_message_queue": mock_queue,
        })
        assert result["success"] is True
        assert result["message_id"] is not None

    @patch("tools.builtin.task_manage._get_task_service")
    def test_inject_no_queue(self, mock_get_svc: MagicMock) -> None:
        """inject 无 MessageQueue 服务。"""
        _reset_task_service_singleton()
        task = _make_task()
        mock_svc = _mock_task_service({"task_001": task})
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({
            "action": "inject",
            "task_id": "task_001",
            "message": "请检查进度",
            "session_id": "session_001",
        })
        assert result["success"] is False
        assert result["error_code"] == "SERVICE_UNAVAILABLE"

    def test_inject_missing_message(self) -> None:
        """inject 缺少 message。"""
        result = task_manage_func({
            "action": "inject",
            "task_id": "task_001",
            "session_id": "session_001",
        })
        assert result["success"] is False
        assert result["error_code"] == "MISSING_MESSAGE"

    def test_inject_missing_session_id(self) -> None:
        """inject 缺少 session_id。"""
        result = task_manage_func({
            "action": "inject",
            "task_id": "task_001",
            "message": "hello",
        })
        assert result["success"] is False
        assert result["error_code"] == "MISSING_SESSION_ID"


# ── action=reactivate ──────────────────────────────────


class TestActionReactivate:
    """reactivate 操作测试。"""

    @patch("tools.builtin.task_manage._retry_emit_event")
    @patch("tools.builtin.task_manage._get_task_service")
    def test_reactivate_completed_task(self, mock_get_svc: MagicMock, mock_emit: MagicMock) -> None:
        """重新激活已完成的任务。"""
        _reset_task_service_singleton()
        completed_task = _make_task(
            status=TaskStatus.COMPLETED,
            pipeline_run_id="pipe_old_001",
        )
        running_task = _make_task(status=TaskStatus.RUNNING)

        tasks = {"task_001": completed_task}
        mock_svc = _mock_task_service(tasks)
        mock_svc.start_task.return_value = running_task
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({
            "action": "reactivate",
            "task_id": "task_001",
            "message": "追加修复：dark mode 颜色不对",
        })
        assert result["success"] is True
        assert result["status"] == "running"
        assert "追加修复" in result["message"]
        mock_emit.assert_called_once_with("task_001")

    @patch("tools.builtin.task_manage._get_task_service")
    def test_reactivate_non_completed_task(self, mock_get_svc: MagicMock) -> None:
        """不能重新激活非完成任务。"""
        _reset_task_service_singleton()
        running_task = _make_task(status=TaskStatus.RUNNING)
        mock_svc = _mock_task_service({"task_001": running_task})
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "reactivate", "task_id": "task_001"})
        assert result["success"] is False
        assert result["error_code"] == "INVALID_STATUS"

    @patch("tools.builtin.task_manage._retry_emit_event")
    @patch("tools.builtin.task_manage._get_task_service")
    def test_reactivate_preserves_pipeline_history(self, mock_get_svc: MagicMock, mock_emit: MagicMock) -> None:
        """reactivate 保留旧 pipeline_run_id 到 history。"""
        _reset_task_service_singleton()
        completed_task = _make_task(
            status=TaskStatus.COMPLETED,
            pipeline_run_id="pipe_old_002",
        )
        running_task = _make_task(status=TaskStatus.RUNNING)

        tasks = {"task_001": completed_task}
        mock_svc = _mock_task_service(tasks)
        mock_svc.start_task.return_value = running_task
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "reactivate", "task_id": "task_001"})
        assert result["success"] is True
        # 验证 reactivate_task 被调用
        mock_svc.reactivate_task.assert_called_once()

    @patch("tools.builtin.task_manage._retry_emit_event")
    @patch("tools.builtin.task_manage._get_task_service")
    def test_reactivate_without_message(self, mock_get_svc: MagicMock, mock_emit: MagicMock) -> None:
        """不带 message 也能 reactivate。"""
        _reset_task_service_singleton()
        completed_task = _make_task(status=TaskStatus.COMPLETED)
        running_task = _make_task(status=TaskStatus.RUNNING)

        tasks = {"task_001": completed_task}
        mock_svc = _mock_task_service(tasks)
        mock_svc.start_task.return_value = running_task
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "reactivate", "task_id": "task_001"})
        assert result["success"] is True
        assert "重新激活" in result["message"]

    def test_reactivate_missing_task_id(self) -> None:
        """缺少 task_id。"""
        result = task_manage_func({"action": "reactivate"})
        assert result["success"] is False
        assert result["error_code"] == "MISSING_TASK_ID"

    @patch("tools.builtin.task_manage._get_task_service")
    def test_reactivate_not_found(self, mock_get_svc: MagicMock) -> None:
        """任务不存在。"""
        _reset_task_service_singleton()
        mock_svc = _mock_task_service()
        mock_get_svc.return_value = mock_svc

        result = task_manage_func({"action": "reactivate", "task_id": "nonexistent"})
        assert result["success"] is False
        assert result["error_code"] == "TASK_NOT_FOUND"


# ── 参数校验 ──────────────────────────────────────────


class TestValidation:
    """参数校验测试。"""

    def test_missing_action(self) -> None:
        """缺少 action。"""
        result = task_manage_func({})
        assert result["success"] is False
        assert result["error_code"] == "MISSING_ACTION"

    def test_invalid_action(self) -> None:
        """无效 action。"""
        result = task_manage_func({"action": "delete"})
        assert result["success"] is False
        assert result["error_code"] == "INVALID_ACTION"
