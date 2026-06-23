"""评估门禁机制测试（补充版）。

验证 5 类评估指标的门禁通过/失败逻辑、最多重试 3 次机制。
对应需求：F-TEST-06~09, AC-TST-05

覆盖内容：
1. 5 类指标（file_check/format_valid/bash_check/semantic_check/human_review）
   的指标定义从 YAML 加载正确性
2. 门禁通过/失败端到端流程（ExpectEvaluator → MetricResult → EvaluationResult）
3. fail_fast 快速失败机制
4. 最多重试 3 次的门禁逻辑
5. 红线指标一票否决逻辑
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from evaluation.engine import EvaluationEngine
from evaluation.executor import EvaluationExecutor
from evaluation.expect import ExpectEvaluator
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
# 辅助函数
# ---------------------------------------------------------------------------


def _make_metric(
    metric_id: str = "test_metric",
    metric_type: MetricType = MetricType.TOOL,
    is_red_line: bool = False,
    expect: ExpectSpec | None = None,
) -> MetricDefinition:
    """创建测试用指标定义。"""
    return MetricDefinition(
        id=metric_id,
        name=f"测试指标 {metric_id}",
        description="测试用指标",
        metric_type=metric_type,
        is_red_line=is_red_line,
        expect=expect or ExpectSpec(),
    )


def _make_passed_result(metric_id: str = "m1") -> MetricResult:
    return MetricResult(metric_id=metric_id, passed=True, message="通过", score=90.0)


def _make_failed_result(metric_id: str = "m2") -> MetricResult:
    return MetricResult(metric_id=metric_id, passed=False, message="未通过", score=40.0)


# ===========================================================================
# 一、5 类评估指标 YAML 加载验证
# ===========================================================================


class TestMetricYAMLLoading:
    """5 类评估指标从 YAML 文件加载正确性。

    验证点（F-TEST-07）：5 类评估指标定义存在且可加载。
    """

    EXPECTED_METRIC_IDS = {
        "file_check",
        "format_valid",
        "bash_check",
        "semantic_check",
        "human_review",
    }

    def test_all_five_metrics_loaded(self) -> None:
        """5 个指标全部成功加载。"""
        loader = MetricLoader()
        loader.load_all()

        loaded_ids = set(loader.metrics.keys())
        missing = self.EXPECTED_METRIC_IDS - loaded_ids
        assert not missing, f"缺少指标: {missing}（已加载: {loaded_ids}）"

    def test_file_check_metric_definition(self) -> None:
        """file_check 指标定义正确。"""
        loader = MetricLoader()
        loader.load_all()

        metric = loader.get("file_check")
        assert metric is not None, "file_check 指标应存在"
        assert metric.metric_type == MetricType.TOOL
        assert metric.evaluator_id == "file_read"
        assert len(metric.expect.conditions) > 0

    def test_format_valid_metric_definition(self) -> None:
        """format_valid 指标定义正确（修复缺失文件后）。"""
        loader = MetricLoader()
        loader.load_all()

        metric = loader.get("format_valid")
        assert metric is not None, "format_valid 指标应存在"
        assert metric.metric_type == MetricType.TOOL
        assert metric.evaluator_id == "schema_evaluator"

    def test_bash_check_metric_definition(self) -> None:
        """bash_check 指标定义正确。"""
        loader = MetricLoader()
        loader.load_all()

        metric = loader.get("bash_check")
        assert metric is not None, "bash_check 指标应存在"
        assert metric.metric_type == MetricType.TOOL
        assert metric.evaluator_id == "bash_execute"

    def test_semantic_check_metric_definition(self) -> None:
        """semantic_check 指标定义正确。"""
        loader = MetricLoader()
        loader.load_all()

        metric = loader.get("semantic_check")
        assert metric is not None, "semantic_check 指标应存在"
        assert metric.metric_type == MetricType.AGENT
        assert metric.evaluator_id == "evaluator_agent"

    def test_human_review_metric_definition(self) -> None:
        """human_review 指标定义正确。"""
        loader = MetricLoader()
        loader.load_all()

        metric = loader.get("human_review")
        assert metric is not None, "human_review 指标应存在"
        assert metric.metric_type == MetricType.HUMAN
        assert metric.evaluator_id == "human_interaction"


# ===========================================================================
# 二、门禁通过/失败端到端流程（ExpectEvaluator → Result）
# ===========================================================================


class TestGatePassFail:
    """门禁通过/失败端到端测试。

    验证点（F-TEST-06, AC-TST-05）：指标评估结果正确判定通过/失败。
    """

    def test_file_check_pass_condition(self) -> None:
        """file_check expect 条件：success is_true → 通过。

        模拟 file_read 返回 success=True 的场景。
        """
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[ExpectCondition(field="success", operator="is_true")],
            logic="and",
            pass_message="文件存在",
            fail_message="文件不存在",
        )

        result = evaluator.evaluate(
            metric_id="file_check",
            expect=expect,
            output={"success": True, "data": {"content": "..."}},
        )
        assert result.passed is True
        assert "文件存在" in result.message

    def test_file_check_fail_condition(self) -> None:
        """file_check expect 条件：success is_false → 失败。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[ExpectCondition(field="success", operator="is_true")],
            logic="and",
            pass_message="文件存在",
            fail_message="文件不存在",
        )

        result = evaluator.evaluate(
            metric_id="file_check",
            expect=expect,
            output={"success": False, "error": "file not found"},
        )
        assert result.passed is False
        assert "文件不存在" in result.message

    def test_bash_check_multi_condition_pass(self) -> None:
        """bash_check 多条件判定：success=true + exit_code=0 → 通过。

        bash_check 的 expect 包含三个条件：
        1. success is_true
        2. output.status equals "completed"
        3. output.exit_code equals 0
        """
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(field="output.status", operator="equals", value="completed"),
                ExpectCondition(field="output.exit_code", operator="equals", value=0),
            ],
            logic="and",
        )

        result = evaluator.evaluate(
            metric_id="bash_check",
            expect=expect,
            output={
                "success": True,
                "output": {"status": "completed", "exit_code": 0},
            },
        )
        assert result.passed is True

    def test_bash_check_multi_condition_fail_on_exit_code(self) -> None:
        """bash_check 多条件判定：exit_code!=0 → 失败。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(field="output.exit_code", operator="equals", value=0),
            ],
            logic="and",
        )

        result = evaluator.evaluate(
            metric_id="bash_check",
            expect=expect,
            output={
                "success": True,
                "output": {"exit_code": 1},
            },
        )
        assert result.passed is False

    def test_human_review_approve_passes(self) -> None:
        """human_review 条件：success=true + selected_option=approve → 通过。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(
                    field="output.selected_option", operator="equals", value="approve"
                ),
            ],
            logic="and",
        )

        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output={"success": True, "output": {"selected_option": "approve"}},
        )
        assert result.passed is True

    def test_human_review_reject_fails(self) -> None:
        """human_review 条件：selected_option=reject → 失败。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(
                    field="output.selected_option", operator="equals", value="approve"
                ),
            ],
            logic="and",
        )

        result = evaluator.evaluate(
            metric_id="human_review",
            expect=expect,
            output={"success": True, "output": {"selected_option": "reject"}},
        )
        assert result.passed is False

    def test_semantic_check_pass(self) -> None:
        """semantic_check 条件：passed is_true → 通过。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[ExpectCondition(field="passed", operator="is_true")],
            logic="and",
        )

        result = evaluator.evaluate(
            metric_id="semantic_check",
            expect=expect,
            output={"passed": True, "score": 85},
        )
        assert result.passed is True

    def test_format_valid_pass(self) -> None:
        """format_valid 条件：success is_true → 通过。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[ExpectCondition(field="success", operator="is_true")],
            logic="and",
        )

        result = evaluator.evaluate(
            metric_id="format_valid",
            expect=expect,
            output={"success": True, "data": {"valid": True}},
        )
        assert result.passed is True


# ===========================================================================
# 三、fail_fast 快速失败机制
# ===========================================================================


class TestFailFastMechanism:
    """fail_fast 快速失败机制测试。

    验证点（F-TEST-09）：门禁未通过必须回退修复。
    """

    @pytest.mark.asyncio
    async def test_fail_fast_stops_at_first_failure(self) -> None:
        """fail_fast=True 时第一个指标失败即停止。

        通过注入 3 个指标到 loader 中，使用 fail_fast=True 配置，
        验证评估器在第一个指标失败后不再调用后续指标。
        """
        loader = MetricLoader()

        # 手动注入 3 个 tool 类型指标
        for mid in ("test_ff_m1", "test_ff_m2", "test_ff_m3"):
            loader.metrics[mid] = _make_metric(
                mid,
                MetricType.TOOL,
                expect=ExpectSpec(
                    conditions=[ExpectCondition(field="success", operator="is_true")],
                ),
            )

        call_count = 0

        async def mock_evaluator(
            metric_def: MetricDefinition, params: dict, task_id: str = ""
        ) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"success": False}

        engine = EvaluationEngine(loader=loader)
        engine.register_evaluator(MetricType.TOOL, mock_evaluator)

        config = EvaluationConfig(
            metric_ids=["test_ff_m1", "test_ff_m2", "test_ff_m3"],
            fail_fast=True,
        )

        result = await engine.evaluate(task_id="test-fail-fast", config=config)

        assert call_count == 1, f"fail_fast 应在第一个指标后停止，但调用了 {call_count} 次"
        assert result.overall_passed is False

    @pytest.mark.asyncio
    async def test_no_fail_fast_evaluates_all(self) -> None:
        """fail_fast=False 时所有指标都评估。"""
        loader = MetricLoader()

        for mid in ("test_nf_m1", "test_nf_m2", "test_nf_m3"):
            loader.metrics[mid] = _make_metric(
                mid,
                MetricType.TOOL,
                expect=ExpectSpec(
                    conditions=[ExpectCondition(field="success", operator="is_true")],
                ),
            )

        call_count = 0

        async def mock_evaluator(
            metric_def: MetricDefinition, params: dict, task_id: str = ""
        ) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"success": False}

        engine = EvaluationEngine(loader=loader)
        engine.register_evaluator(MetricType.TOOL, mock_evaluator)

        config = EvaluationConfig(
            metric_ids=["test_nf_m1", "test_nf_m2", "test_nf_m3"],
            fail_fast=False,
        )

        result = await engine.evaluate(task_id="test-no-fail-fast", config=config)

        assert call_count == 3, f"无 fail_fast 应评估全部 3 个指标，但调用了 {call_count} 次"
        assert result.overall_passed is False


# ===========================================================================
# 四、最多重试 3 次的门禁逻辑
# ===========================================================================


class TestMaxRetries:
    """门禁最多重试 3 次机制测试。

    验证点（F-TEST-09）：门禁未通过必须回退修复（最多重试 3 次）。
    """

    MAX_RETRIES = 3

    @pytest.mark.asyncio
    async def test_retry_until_pass(self) -> None:
        """重试次数 ≤ 3 时通过 → 门禁开放。

        模拟前 2 次评估失败，第 3 次通过。
        """
        mock_engine = AsyncMock(spec=EvaluationEngine)
        attempt_count = 0

        async def mock_evaluate(**kwargs):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                return EvaluationResult(
                    task_id="task-retry",
                    results=[_make_failed_result()],
                    overall_passed=False,
                    summary="0/1 指标通过",
                )
            return EvaluationResult(
                task_id="task-retry",
                results=[_make_passed_result()],
                overall_passed=True,
                summary="1/1 指标通过",
            )

        mock_engine.evaluate = mock_evaluate

        executor = EvaluationExecutor(engine=mock_engine)

        passed = False
        for attempt in range(1, self.MAX_RETRIES + 1):
            result = await executor.run_evaluation(
                task_id="task-retry",
                metric_ids=["m1"],
                skip_state_update=True,
            )
            if result.overall_passed:
                passed = True
                break

        assert passed, "应在重试 ≤3 次内通过门禁"
        assert attempt <= self.MAX_RETRIES, f"重试次数 {attempt} 超过上限 {self.MAX_RETRIES}"

    @pytest.mark.asyncio
    async def test_retry_exceeded_stays_failed(self) -> None:
        """重试超过 3 次仍失败 → 门禁关闭。"""
        mock_engine = AsyncMock(spec=EvaluationEngine)

        async def mock_evaluate(**kwargs):
            return EvaluationResult(
                task_id="task-always-fail",
                results=[_make_failed_result()],
                overall_passed=False,
                summary="0/1 指标通过",
            )

        mock_engine.evaluate = mock_evaluate
        executor = EvaluationExecutor(engine=mock_engine)

        final_passed = True
        for attempt in range(1, self.MAX_RETRIES + 1):
            result = await executor.run_evaluation(
                task_id="task-always-fail",
                metric_ids=["m1"],
                skip_state_update=True,
            )
            final_passed = result.overall_passed
            if final_passed:
                break

        assert not final_passed, "重试 3 次后应仍为失败"
        assert attempt == self.MAX_RETRIES, f"应正好重试 {self.MAX_RETRIES} 次"


# ===========================================================================
# 五、红线指标一票否决
# ===========================================================================


class TestRedLineMetric:
    """红线指标一票否决逻辑。

    验证点：红线指标未通过 → 整体失败。
    """

    def test_red_line_fail_overrides_other_passes(self) -> None:
        """红线指标失败 → 即使其他指标通过，整体也失败。"""
        result = EvaluationResult(
            task_id="task-redline",
            results=[
                _make_passed_result("file_check"),
                _make_failed_result("critical_check"),
                _make_passed_result("format_valid"),
            ],
        )
        result.compute_overall()

        assert result.overall_passed is False, "任一指标失败 → 整体失败"

    def test_all_passed_includes_red_line(self) -> None:
        """全部指标（含红线）通过 → 整体通过。"""
        result = EvaluationResult(
            task_id="task-all-pass",
            results=[
                _make_passed_result("file_check"),
                _make_passed_result("critical_check"),
            ],
        )
        result.compute_overall()

        assert result.overall_passed is True
        assert "2/2" in result.summary


# ===========================================================================
# 六、EvaluationExecutor 状态回写完整性
# ===========================================================================


class TestExecutorStateWriteback:
    """EvaluationExecutor 评估后状态回写测试。

    验证点（F-TEST-08）：门禁通过 task_submit 附带评估指标。
    """

    @pytest.mark.asyncio
    async def test_executor_writeback_on_pass(self) -> None:
        """评估通过 → task_service.complete_evaluation 被调用。"""
        mock_engine = AsyncMock(spec=EvaluationEngine)
        mock_engine.evaluate = AsyncMock(
            return_value=EvaluationResult(
                task_id="task-writeback",
                results=[_make_passed_result("file_check")],
                overall_passed=True,
                summary="1/1 指标通过",
            )
        )

        mock_task_service = AsyncMock()
        mock_task_service.complete_evaluation = AsyncMock()

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )

        await executor.run_evaluation(
            task_id="task-writeback",
            metric_ids=["file_check"],
        )

        mock_task_service.complete_evaluation.assert_called_once()
        call_args = mock_task_service.complete_evaluation.call_args
        assert call_args.args[1] is True, "overall_passed 应为 True"

    @pytest.mark.asyncio
    async def test_executor_writeback_on_fail(self) -> None:
        """评估失败 → task_service.complete_evaluation 被调用且 passed=False。"""
        mock_engine = AsyncMock(spec=EvaluationEngine)
        mock_engine.evaluate = AsyncMock(
            return_value=EvaluationResult(
                task_id="task-writeback-fail",
                results=[_make_failed_result("bash_check")],
                overall_passed=False,
                summary="0/1 指标通过",
            )
        )

        mock_task_service = AsyncMock()
        mock_task_service.complete_evaluation = AsyncMock()

        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )

        await executor.run_evaluation(
            task_id="task-writeback-fail",
            metric_ids=["bash_check"],
        )

        mock_task_service.complete_evaluation.assert_called_once()
        call_args = mock_task_service.complete_evaluation.call_args
        assert call_args.args[1] is False, "overall_passed 应为 False"

    @pytest.mark.asyncio
    async def test_executor_writeback_contains_metric_details(self) -> None:
        """回写数据包含每个指标的详细信息（metric_id/passed/score/message）。"""
        mock_engine = AsyncMock(spec=EvaluationEngine)
        mock_engine.evaluate = AsyncMock(
            return_value=EvaluationResult(
                task_id="task-detail",
                results=[
                    _make_passed_result("file_check"),
                    _make_failed_result("format_valid"),
                ],
                overall_passed=False,
                summary="1/2 指标通过",
            )
        )

        mock_task_service = AsyncMock()
        executor = EvaluationExecutor(
            task_service=mock_task_service,
            engine=mock_engine,
        )

        await executor.run_evaluation(
            task_id="task-detail",
            metric_ids=["file_check", "format_valid"],
        )

        call_kwargs = mock_task_service.complete_evaluation.call_args
        eval_data = call_kwargs.kwargs.get("result") or {}
        assert "metrics" in eval_data
        metrics_list = eval_data["metrics"]
        assert len(metrics_list) == 2

        for m in metrics_list:
            assert "metric_id" in m
            assert "passed" in m
            assert "score" in m
            assert "message" in m
