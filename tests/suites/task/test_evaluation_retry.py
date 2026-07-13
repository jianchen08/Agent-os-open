"""评估重试与 criteria 自动填充测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from evaluation.executor import EvaluationExecutor
from evaluation.types import EvaluationResult, MetricResult
from tasks.types import TaskStatus
from tools.builtin.task_evaluate import TaskEvaluateTool


class TestCriteriaAutoFill:
    """criteria 自动填充测试套件（B2）。"""

    @pytest.mark.task
    @pytest.mark.unit
    def test_criteria_auto_filled_from_task_description(self) -> None:
        """当 input_params 中无 criteria 时，从 task.description 自动填充。"""
        tool = TaskEvaluateTool()
        task = MagicMock()
        task.id = "test_task_001"
        task.metadata = {
            "acceptance_criteria": {
                "semantic_check": {
                    "input_params": {},
                },
            },
        }
        task.description = "写一份关于 Python 异步编程的报告"

        params = tool._get_input_params(task)

        assert params["semantic_check"]["criteria"] == "写一份关于 Python 异步编程的报告"

    @pytest.mark.task
    @pytest.mark.unit
    def test_criteria_not_overwritten_if_provided(self) -> None:
        """当 input_params 中已有 criteria 时，不覆盖。"""
        tool = TaskEvaluateTool()
        task = MagicMock()
        task.id = "test_task_001"
        task.metadata = {
            "acceptance_criteria": {
                "semantic_check": {
                    "input_params": {
                        "criteria": "自定义标准",
                    },
                },
            },
        }
        task.description = "写一份关于 Python 异步编程的报告"

        params = tool._get_input_params(task)

        assert params["semantic_check"]["criteria"] == "自定义标准"


class TestRetryLoop:
    """重试闭环测试套件（B1）。"""

    @pytest.mark.task
    @pytest.mark.unit
    async def test_retry_then_pass(self) -> None:
        """第一次评估失败返回 retry，第二次通过返回 completed。"""
        tool = TaskEvaluateTool()
        tool._save_task = AsyncMock()

        task = MagicMock()
        task.id = "test_task_001"
        task.status = TaskStatus.RUNNING
        task.metadata = {"max_eval_retries": 3}

        task_service = MagicMock()
        task_service.complete_evaluation = AsyncMock()

        # 第一次评估：失败 → retry
        eval_fail = EvaluationResult(
            task_id="test_task_001",
            results=[MetricResult(metric_id="semantic_check", passed=False)],
        )
        result1 = await tool._handle_evaluation_result(
            inputs={"action": "auto_complete"},
            task_service=task_service,
            task=task,
            eval_result=eval_fail,
        )
        assert result1.metadata["result"] == "retry"

        # 第二次评估：通过 → completed
        eval_pass = EvaluationResult(
            task_id="test_task_001",
            results=[MetricResult(metric_id="semantic_check", passed=True)],
        )
        result2 = await tool._handle_evaluation_result(
            inputs={"action": "auto_complete"},
            task_service=task_service,
            task=task,
            eval_result=eval_pass,
        )
        assert result2.metadata["result"] == "completed"

    @pytest.mark.task
    @pytest.mark.unit
    async def test_retry_exhausted(self) -> None:
        """连续 3 次评估失败后触发 exhausted，返回 failed。"""
        tool = TaskEvaluateTool()
        tool._save_task = AsyncMock()

        task = MagicMock()
        task.id = "test_task_001"
        task.status = TaskStatus.RUNNING
        task.metadata = {"max_eval_retries": 3}

        task_service = MagicMock()
        task_service.complete_evaluation = AsyncMock()
        result = None

        for _ in range(3):
            eval_fail = EvaluationResult(
                task_id="test_task_001",
                results=[MetricResult(metric_id="semantic_check", passed=False)],
            )
            result = await tool._handle_evaluation_result(
                inputs={"action": "auto_complete"},
                task_service=task_service,
                task=task,
                eval_result=eval_fail,
            )

        assert result is not None
        assert result.metadata["result"] == "failed"
        assert task.metadata["eval_retry_count"]["semantic_check"] == 3

    @pytest.mark.task
    @pytest.mark.unit
    async def test_retry_feedback_contains_details(self) -> None:
        """retry 反馈消息包含指标 ID 和剩余次数。"""
        tool = TaskEvaluateTool()
        tool._save_task = AsyncMock()

        task = MagicMock()
        task.id = "test_task_001"
        task.metadata = {"max_eval_retries": 3}

        task_service = MagicMock()

        eval_fail = EvaluationResult(
            task_id="test_task_001",
            results=[MetricResult(metric_id="semantic_check", passed=False)],
        )
        result = await tool._handle_evaluation_result(
            inputs={"action": "auto_complete"},
            task_service=task_service,
            task=task,
            eval_result=eval_fail,
        )

        message = result.metadata["message"]
        assert "[semantic_check] 未通过" in message
        assert "剩余重试" in message

    @pytest.mark.task
    @pytest.mark.unit
    def test_skip_state_update_on_retry(self) -> None:
        """skip_state_update=True 时 executor 不调用 complete_evaluation。"""
        mock_task_service = MagicMock()
        mock_engine = MagicMock()
        mock_engine.evaluate.return_value = EvaluationResult(
            task_id="test_task_001",
            results=[MetricResult(metric_id="semantic_check", passed=True)],
            overall_passed=True,
            summary="1/1 指标通过",
        )

        mock_loader = MagicMock()
        mock_loader.metrics = {"semantic_check": MagicMock()}

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            loader=mock_loader,
            engine=mock_engine,
        )

        executor.run_evaluation(
            task_id="test_task_001",
            metric_ids=["semantic_check"],
            skip_state_update=True,
        )

        mock_task_service.complete_evaluation.assert_not_called()
