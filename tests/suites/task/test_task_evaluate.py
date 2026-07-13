"""task_evaluate 单元测试 — 评估逻辑（Mock TaskService）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


from tasks.types import TaskModel, TaskStatus
from tools.builtin.task_evaluate import (
    _simple_evaluate,
    task_evaluate_func,
)


# ── 辅助 ──────────────────────────────────────────────


def _make_task(
    task_id: str = "task_001",
    title: str = "Test Task",
    status: TaskStatus = TaskStatus.EVALUATING,
    result: Any = None,
    metadata: dict[str, Any] | None = None,
) -> TaskModel:
    """构造测试任务。"""
    return TaskModel(
        id=task_id,
        title=title,
        status=status,
        result=result,
        metadata=metadata or {},
    )


def _mock_task_service(tasks: dict[str, TaskModel] | None = None) -> MagicMock:
    """创建 Mock TaskService。"""
    if tasks is None:
        tasks = {}

    svc = MagicMock()

    def get_task(task_id: str) -> TaskModel | None:
        return tasks.get(task_id)

    svc.get_task.side_effect = get_task
    svc._storage = MagicMock()
    svc.move_to_evaluating = AsyncMock()
    svc.complete_evaluation = AsyncMock()

    def save(task: TaskModel) -> None:
        tasks[task.id] = task

    svc._storage.save.side_effect = save

    return svc


# ── _simple_evaluate ───────────────────────────────────


class TestSimpleEvaluate:
    """简化评估逻辑测试。"""

    def test_no_criteria_passes(self) -> None:
        """无验收标准 → 默认通过。"""
        task = _make_task()
        passed, detail = _simple_evaluate(task, "")
        assert passed is True
        assert "无验收标准" in detail

    def test_empty_criteria_passes(self) -> None:
        """空验收标准 → 默认通过。"""
        task = _make_task(metadata={"acceptance_criteria": {}})
        passed, detail = _simple_evaluate(task, "")
        assert passed is True

    def test_has_criteria_and_result_passes(self) -> None:
        """有验收标准且有结果 → 通过。"""
        task = _make_task(
            result="任务已完成",
            metadata={"acceptance_criteria": {"quality": {"threshold": 80}}},
        )
        passed, detail = _simple_evaluate(task, "")
        assert passed is True
        assert "1 项" in detail

    def test_has_criteria_no_result_fails(self) -> None:
        """有验收标准但无结果 → 不通过。"""
        task = _make_task(
            metadata={"acceptance_criteria": {"quality": {"threshold": 80}}},
        )
        passed, detail = _simple_evaluate(task, "")
        assert passed is False
        assert "无执行结果" in detail

    def test_notes_appended(self) -> None:
        """评估备注附加到详情。"""
        task = _make_task()
        _, detail = _simple_evaluate(task, "人工审核")
        assert "人工审核" in detail


# ── action=evaluate_single ─────────────────────────────


class TestEvaluateSingle:
    """evaluate_single 操作测试。"""

    @patch("tasks.service.TaskService")
    async def test_evaluate_evaluating_task_pass(self, MockTS: MagicMock) -> None:
        """评估 evaluating 状态任务通过。"""
        task = _make_task(status=TaskStatus.EVALUATING)
        completed_task = _make_task(status=TaskStatus.COMPLETED)
        mock_svc = _mock_task_service({"task_001": task})
        mock_svc.complete_evaluation.return_value = completed_task
        MockTS.return_value = mock_svc

        result = await task_evaluate_func({
            "action": "evaluate_single",
            "task_id": "task_001",
        })
        assert result["success"] is True
        assert result["status"] == "completed"

    @patch("tasks.service.TaskService")
    async def test_evaluate_running_task_moves_to_evaluating(self, MockTS: MagicMock) -> None:
        """running 状态任务先移入 evaluating。"""
        running_task = _make_task(status=TaskStatus.RUNNING)
        evaluating_task = _make_task(status=TaskStatus.EVALUATING)
        completed_task = _make_task(status=TaskStatus.COMPLETED)
        mock_svc = _mock_task_service({"task_001": running_task})
        mock_svc.move_to_evaluating.return_value = evaluating_task

        def complete_evaluation(task_id: str, passed: bool) -> TaskModel:
            return completed_task

        mock_svc.complete_evaluation.side_effect = complete_evaluation
        MockTS.return_value = mock_svc

        result = await task_evaluate_func({
            "action": "evaluate_single",
            "task_id": "task_001",
        })
        assert result["success"] is True
        mock_svc.move_to_evaluating.assert_called_once_with("task_001")

    @patch("tasks.service.TaskService")
    async def test_evaluate_with_result_text(self, MockTS: MagicMock) -> None:
        """带执行结果的评估。"""
        task = _make_task(status=TaskStatus.EVALUATING)
        completed_task = _make_task(status=TaskStatus.COMPLETED)
        mock_svc = _mock_task_service({"task_001": task})
        mock_svc.complete_evaluation.return_value = completed_task
        MockTS.return_value = mock_svc

        result = await task_evaluate_func({
            "action": "evaluate_single",
            "task_id": "task_001",
            "result": "任务执行结果",
        })
        assert result["success"] is True

    @patch("tasks.service.TaskService")
    async def test_evaluate_nonexistent_task(self, MockTS: MagicMock) -> None:
        """评估不存在的任务。"""
        mock_svc = _mock_task_service()
        MockTS.return_value = mock_svc

        result = await task_evaluate_func({
            "action": "evaluate_single",
            "task_id": "nonexistent",
        })
        assert result["success"] is False
        assert result["error_code"] == "TASK_NOT_FOUND"

    @patch("tasks.service.TaskService")
    async def test_evaluate_invalid_status(self, MockTS: MagicMock) -> None:
        """评估不支持的状态。"""
        task = _make_task(status=TaskStatus.PENDING)
        mock_svc = _mock_task_service({"task_001": task})
        MockTS.return_value = mock_svc

        result = await task_evaluate_func({
            "action": "evaluate_single",
            "task_id": "task_001",
        })
        assert result["success"] is False
        assert result["error_code"] in ("INVALID_TRANSITION", "INVALID_STATUS")


# ── action=auto_complete ───────────────────────────────


class TestAutoComplete:
    """auto_complete 操作测试。"""

    @patch("tasks.service.TaskService")
    async def test_auto_complete_running_task(self, MockTS: MagicMock) -> None:
        """自动完成 running 状态任务。"""
        running_task = _make_task(status=TaskStatus.RUNNING)
        evaluating_task = _make_task(status=TaskStatus.EVALUATING)
        completed_task = _make_task(status=TaskStatus.COMPLETED)
        mock_svc = _mock_task_service({"task_001": running_task})
        mock_svc.move_to_evaluating.return_value = evaluating_task
        mock_svc.complete_evaluation.return_value = completed_task
        MockTS.return_value = mock_svc

        result = await task_evaluate_func({
            "action": "auto_complete",
            "task_id": "task_001",
        })
        assert result["success"] is True
        mock_svc.move_to_evaluating.assert_called_once()

    @patch("tasks.service.TaskService")
    async def test_auto_complete_invalid_status(self, MockTS: MagicMock) -> None:
        """自动完成不支持的状态。"""
        task = _make_task(status=TaskStatus.PAUSED)
        mock_svc = _mock_task_service({"task_001": task})
        MockTS.return_value = mock_svc

        result = await task_evaluate_func({
            "action": "auto_complete",
            "task_id": "task_001",
        })
        assert result["success"] is False
        assert result["error_code"] == "INVALID_STATUS"

    @patch("tasks.service.TaskService")
    async def test_auto_complete_nonexistent(self, MockTS: MagicMock) -> None:
        """自动完成不存在的任务。"""
        mock_svc = _mock_task_service()
        MockTS.return_value = mock_svc

        result = await task_evaluate_func({
            "action": "auto_complete",
            "task_id": "nonexistent",
        })
        assert result["success"] is False
        assert result["error_code"] == "TASK_NOT_FOUND"


# ── 参数校验 ──────────────────────────────────────────


class TestValidation:
    """参数校验测试。"""

    async def test_missing_action(self) -> None:
        """缺少 action。"""
        result = await task_evaluate_func({"task_id": "t1"})
        assert result["success"] is False
        assert result["error_code"] == "MISSING_ACTION"

    async def test_missing_task_id(self) -> None:
        """缺少 task_id。"""
        result = await task_evaluate_func({"action": "evaluate_single"})
        assert result["success"] is False
        assert result["error_code"] == "MISSING_TASK_ID"

    async def test_invalid_action(self) -> None:
        """无效 action。"""
        result = await task_evaluate_func({"action": "invalid", "task_id": "t1"})
        assert result["success"] is False
        assert result["error_code"] == "INVALID_ACTION"
