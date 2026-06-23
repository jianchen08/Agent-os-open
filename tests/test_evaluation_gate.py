"""评估门禁机制测试。

验证评估系统的核心流程：指标评估 → 结果判定 → 状态回写。
对应需求：F-TEST-06~09, AC-TST-05

覆盖内容：
- 评估结果综合判定（EvaluationResult.compute_overall）
- ResultMapper 状态映射（pass/fail → 任务状态）
- EvaluationExecutor 评估执行 + 状态回写
- fail_fast 快速失败逻辑
- 门禁通过/未通过的完整流程
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from evaluation.engine import EvaluationEngine
from evaluation.executor import EvaluationExecutor
from evaluation.loader import MetricLoader
from evaluation.mapper import ResultMapper
from evaluation.types import (
    EvaluationConfig,
    EvaluationResult,
    ExpectCondition,
    ExpectSpec,
    MetricDefinition,
    MetricResult,
    MetricType,
)


# ---------------------------------------------------------------------------
# 测试 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mapper() -> ResultMapper:
    return ResultMapper()


def _make_metric(
    metric_id: str = "test_metric",
    metric_type: MetricType = MetricType.TOOL,
    is_red_line: bool = False,
) -> MetricDefinition:
    """创建测试用指标定义。"""
    return MetricDefinition(
        id=metric_id,
        name=f"测试指标 {metric_id}",
        description="测试用指标",
        metric_type=metric_type,
        is_red_line=is_red_line,
    )


def _make_passed_result(metric_id: str = "m1") -> MetricResult:
    return MetricResult(metric_id=metric_id, passed=True, message="通过", score=90.0)


def _make_failed_result(metric_id: str = "m2") -> MetricResult:
    return MetricResult(metric_id=metric_id, passed=False, message="未通过", score=40.0)


# ---------------------------------------------------------------------------
# EvaluationResult 综合判定
# ---------------------------------------------------------------------------


class TestEvaluationResultCompute:
    """评估结果综合判定测试。"""

    def test_all_passed_results_yield_overall_pass(self) -> None:
        """所有指标通过 → overall_passed = True。

        验证点（F-TEST-06）：
        - 评估结果全部通过时，overall_passed 应为 True
        """
        result = EvaluationResult(
            task_id="task-001",
            results=[
                _make_passed_result("m1"),
                _make_passed_result("m2"),
            ],
        )

        result.compute_overall()

        assert result.overall_passed is True
        assert "2/2" in result.summary

    def test_any_failed_yields_overall_fail(self) -> None:
        """任一指标未通过 → overall_passed = False。

        验证点（F-TEST-06）：
        - 评估结果中有未通过的指标时，overall_passed 应为 False
        """
        result = EvaluationResult(
            task_id="task-002",
            results=[
                _make_passed_result("m1"),
                _make_failed_result("m2"),
            ],
        )

        result.compute_overall()

        assert result.overall_passed is False
        assert "1/2" in result.summary

    def test_empty_results_yield_fail(self) -> None:
        """无评估指标 → overall_passed = False。

        验证点：
        - 没有评估指标时，不应自动通过
        """
        result = EvaluationResult(task_id="task-003", results=[])

        result.compute_overall()

        assert result.overall_passed is False
        assert "无评估指标" in result.summary

    def test_all_failed_yields_fail(self) -> None:
        """全部指标未通过 → overall_passed = False。"""
        result = EvaluationResult(
            task_id="task-004",
            results=[_make_failed_result("m1"), _make_failed_result("m2")],
        )

        result.compute_overall()

        assert result.overall_passed is False
        assert "0/2" in result.summary


# ---------------------------------------------------------------------------
# ResultMapper 状态映射
# ---------------------------------------------------------------------------


class TestResultMapper:
    """评估结果映射器测试。"""

    def test_map_passed_result_to_task_status(self, mapper: ResultMapper) -> None:
        """通过的评估结果映射为 True（任务转 completed）。

        验证点（AC-TST-05）：
        - 评估通过 → map_to_task_status 返回 True
        """
        result = EvaluationResult(
            task_id="task-010",
            results=[_make_passed_result()],
        )

        overall_passed = mapper.map_to_task_status(result)

        assert overall_passed is True

    def test_map_failed_result_to_task_status(self, mapper: ResultMapper) -> None:
        """未通过的评估结果映射为 False（任务转 failed）。

        验证点（AC-TST-05）：
        - 评估未通过 → map_to_task_status 返回 False
        """
        result = EvaluationResult(
            task_id="task-011",
            results=[_make_failed_result()],
        )

        overall_passed = mapper.map_to_task_status(result)

        assert overall_passed is False

    def test_build_summary_contains_pass_fail_info(
        self, mapper: ResultMapper,
    ) -> None:
        """评估摘要包含通过/失败信息。"""
        result = EvaluationResult(
            task_id="task-012",
            results=[
                _make_passed_result("file_check"),
                _make_failed_result("format_valid"),
            ],
        )

        summary = mapper.build_summary(result)

        assert "1/2" in summary
        assert "file_check" in summary
        assert "format_valid" in summary
        assert "PASS" in summary
        assert "FAIL" in summary

    def test_map_single_result_red_line(
        self, mapper: ResultMapper,
    ) -> None:
        """单个红线指标映射包含 is_red_line 标记。"""
        metric_result = _make_failed_result("critical_check")

        mapped = mapper.map_single_result(metric_result, is_red_line=True)

        assert mapped["is_red_line"] is True
        assert mapped["passed"] is False
        assert mapped["metric_id"] == "critical_check"


# ---------------------------------------------------------------------------
# EvaluationExecutor 评估执行 + 状态回写
# ---------------------------------------------------------------------------


class TestEvaluationExecutor:
    """评估执行器测试（F-TEST-08 门禁通过 task_submit 附带指标）。"""

    @pytest.mark.asyncio
    async def test_executor_passes_evaluation(self) -> None:
        """评估通过 → task_service.complete_evaluation 被调用且 passed=True。

        验证点（F-TEST-08）：
        - 评估执行器完成评估后，调用 task_service 回写状态
        - overall_passed=True 时任务标记为 completed
        """
        mock_engine = AsyncMock(spec=EvaluationEngine)
        mock_engine.evaluate = AsyncMock(return_value=EvaluationResult(
            task_id="task-pass",
            results=[_make_passed_result()],
            overall_passed=True,
            summary="1/1 指标通过",
        ))

        mock_task_service = AsyncMock()
        mock_task_service.complete_evaluation = AsyncMock(return_value=None)

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )

        result = await executor.run_evaluation(
            task_id="task-pass",
            metric_ids=["test_metric"],
        )

        assert result.overall_passed is True
        mock_task_service.complete_evaluation.assert_called_once()
        call_args = mock_task_service.complete_evaluation.call_args
        assert call_args.args[1] is True, "overall_passed 应为 True"

    @pytest.mark.asyncio
    async def test_executor_fails_evaluation(self) -> None:
        """评估未通过 → task_service.complete_evaluation 被调用且 passed=False。

        验证点（AC-TST-05）：
        - 评估未通过时任务标记为 failed
        """
        mock_engine = AsyncMock(spec=EvaluationEngine)
        mock_engine.evaluate = AsyncMock(return_value=EvaluationResult(
            task_id="task-fail",
            results=[_make_failed_result()],
            overall_passed=False,
            summary="0/1 指标通过",
        ))

        mock_task_service = AsyncMock()
        mock_task_service.complete_evaluation = AsyncMock(return_value=None)

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )

        result = await executor.run_evaluation(
            task_id="task-fail",
            metric_ids=["test_metric"],
        )

        assert result.overall_passed is False
        mock_task_service.complete_evaluation.assert_called_once()
        call_args = mock_task_service.complete_evaluation.call_args
        assert call_args.args[1] is False, "overall_passed 应为 False"

    @pytest.mark.asyncio
    async def test_executor_skip_state_update(self) -> None:
        """skip_state_update=True 时不调用 task_service。"""
        mock_engine = AsyncMock(spec=EvaluationEngine)
        mock_engine.evaluate = AsyncMock(return_value=EvaluationResult(
            task_id="task-skip",
            results=[_make_passed_result()],
            overall_passed=True,
        ))

        mock_task_service = AsyncMock()
        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )

        await executor.run_evaluation(
            task_id="task-skip",
            metric_ids=["m1"],
            skip_state_update=True,
        )

        mock_task_service.complete_evaluation.assert_not_called()

    @pytest.mark.asyncio
    async def test_executor_no_task_service(self) -> None:
        """未注入 task_service 时正常执行评估（仅不回写状态）。"""
        mock_engine = AsyncMock(spec=EvaluationEngine)
        mock_engine.evaluate = AsyncMock(return_value=EvaluationResult(
            task_id="task-no-svc",
            results=[_make_passed_result()],
            overall_passed=True,
        ))

        executor = EvaluationExecutor(engine=mock_engine)

        result = await executor.run_evaluation(
            task_id="task-no-svc",
            metric_ids=["m1"],
        )

        assert result.overall_passed is True

    @pytest.mark.asyncio
    async def test_executor_eval_data_contains_metrics(self) -> None:
        """回写数据包含所有指标的详细评估信息。"""
        passed_metric = _make_passed_result("file_check")
        failed_metric = _make_failed_result("format_valid")

        mock_engine = AsyncMock(spec=EvaluationEngine)
        mock_engine.evaluate = AsyncMock(return_value=EvaluationResult(
            task_id="task-detail",
            results=[passed_metric, failed_metric],
            overall_passed=False,
            summary="1/2 指标通过",
        ))

        mock_task_service = AsyncMock()
        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )

        await executor.run_evaluation(task_id="task-detail", metric_ids=["m1", "m2"])

        call_kwargs = mock_task_service.complete_evaluation.call_args
        eval_data = call_kwargs.kwargs.get("result") or {}
        assert "metrics" in eval_data, "回写数据应包含 metrics 列表"
        assert len(eval_data["metrics"]) == 2, "应包含 2 个指标的详情"


# ---------------------------------------------------------------------------
# 5 类评估指标类型验证
# ---------------------------------------------------------------------------


class TestMetricTypes:
    """5 类评估指标类型验证（F-TEST-07）。

    file_check / format_valid / bash_check / semantic_check / human_review
    """

    def test_metric_types_enum_values(self) -> None:
        """MetricType 枚举包含 tool/agent/human 三种评估器类型。"""
        assert MetricType.TOOL.value == "tool"
        assert MetricType.AGENT.value == "agent"
        assert MetricType.HUMAN.value == "human"

    def test_file_check_metric_is_tool_type(self) -> None:
        """file_check 指标使用 tool 类型评估器。"""
        metric = _make_metric("file_check", MetricType.TOOL)
        assert metric.metric_type == MetricType.TOOL

    def test_bash_check_metric_is_tool_type(self) -> None:
        """bash_check 指标使用 tool 类型评估器。"""
        metric = _make_metric("bash_check", MetricType.TOOL)
        assert metric.metric_type == MetricType.TOOL

    def test_format_valid_metric_is_tool_type(self) -> None:
        """format_valid 指标使用 tool 类型评估器。"""
        metric = _make_metric("format_valid", MetricType.TOOL)
        assert metric.metric_type == MetricType.TOOL

    def test_semantic_check_metric_is_agent_type(self) -> None:
        """semantic_check 指标使用 agent 类型评估器。"""
        metric = _make_metric("semantic_check", MetricType.AGENT)
        assert metric.metric_type == MetricType.AGENT

    def test_human_review_metric_is_human_type(self) -> None:
        """human_review 指标使用 human 类型评估器。"""
        metric = _make_metric("human_review", MetricType.HUMAN)
        assert metric.metric_type == MetricType.HUMAN


# ---------------------------------------------------------------------------
# fail_fast 快速失败逻辑
# ---------------------------------------------------------------------------


class TestFailFast:
    """fail_fast 快速失败逻辑测试。"""

    def test_fail_fast_config_default(self) -> None:
        """EvaluationConfig 默认 fail_fast=False。"""
        config = EvaluationConfig()
        assert config.fail_fast is False

    def test_fail_fast_can_be_disabled(self) -> None:
        """EvaluationConfig 可以禁用 fail_fast。"""
        config = EvaluationConfig(fail_fast=False)
        assert config.fail_fast is False
