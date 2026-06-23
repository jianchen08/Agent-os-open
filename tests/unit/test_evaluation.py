"""
评估引擎单元测试。

覆盖 AC：
- AC-EVAL-01: 5 类评估指标正确执行
- AC-EVAL-02: 期望条件判定（11 种操作符）
- AC-EVAL-03: 嵌套字段路径解析正确
- AC-EVAL-04: 评估通过→completed
- AC-EVAL-05: 评估不通过→failed

对应需求：F-EVAL-01~05
"""
import pytest

from src.evaluation.expect import ExpectEvaluator
from src.evaluation.expect_evaluator import ExpectConditionEvaluator
from src.evaluation.mapper import ResultMapper
from src.evaluation.types import (
    EvaluationResult,
    ExpectCondition,
    ExpectSpec,
    MetricDefinition,
    MetricResult,
    MetricType,
)


# ============================================================
# AC-EVAL-02: 期望条件判定 — 11 种操作符（ExpectEvaluator）
# ============================================================

class TestExpectEvaluatorOperators:
    """ExpectEvaluator._check_condition 的 11 种操作符测试。"""

    def setup_method(self) -> None:
        self.evaluator = ExpectEvaluator()

    # ── 布尔判断 ──

    def test_is_true_passes(self) -> None:
        """is_true: 值为真时通过。"""
        cond = ExpectCondition(field="flag", operator="is_true")
        assert self.evaluator._check_condition(True, cond) is True

    def test_is_true_fails_on_false(self) -> None:
        """is_true: 值为假时不通过。"""
        cond = ExpectCondition(field="flag", operator="is_true")
        assert self.evaluator._check_condition(False, cond) is False

    def test_is_false_passes(self) -> None:
        """is_false: 值为假时通过。"""
        cond = ExpectCondition(field="flag", operator="is_false")
        assert self.evaluator._check_condition(False, cond) is True

    def test_is_false_fails_on_true(self) -> None:
        """is_false: 值为真时不通过。"""
        cond = ExpectCondition(field="flag", operator="is_false")
        assert self.evaluator._check_condition(True, cond) is False

    # ── 等值比较 ──

    def test_equals_passes(self) -> None:
        """equals: 值相等时通过。"""
        cond = ExpectCondition(field="status", operator="equals", value="success")
        assert self.evaluator._check_condition("success", cond) is True

    def test_equals_fails(self) -> None:
        """equals: 值不等时不通过。"""
        cond = ExpectCondition(field="status", operator="equals", value="success")
        assert self.evaluator._check_condition("failed", cond) is False

    def test_not_equals_passes(self) -> None:
        """not_equals: 值不等时通过。"""
        cond = ExpectCondition(field="status", operator="not_equals", value="failed")
        assert self.evaluator._check_condition("success", cond) is True

    def test_not_equals_fails(self) -> None:
        """not_equals: 值相等时不通过。"""
        cond = ExpectCondition(field="status", operator="not_equals", value="success")
        assert self.evaluator._check_condition("success", cond) is False

    # ── 集合判断 ──

    def test_in_passes(self) -> None:
        """in: 值在列表中通过。"""
        cond = ExpectCondition(field="code", operator="in", value=[200, 201, 204])
        assert self.evaluator._check_condition(200, cond) is True

    def test_in_fails(self) -> None:
        """in: 值不在列表中不通过。"""
        cond = ExpectCondition(field="code", operator="in", value=[200, 201])
        assert self.evaluator._check_condition(404, cond) is False

    def test_not_in_passes(self) -> None:
        """not_in: 值不在列表中通过。"""
        cond = ExpectCondition(field="code", operator="not_in", value=[404, 500])
        assert self.evaluator._check_condition(200, cond) is True

    def test_not_in_fails(self) -> None:
        """not_in: 值在列表中不通过。"""
        cond = ExpectCondition(field="code", operator="not_in", value=[404, 500])
        assert self.evaluator._check_condition(404, cond) is False

    # ── 包含判断 ──

    def test_contains_passes_for_string(self) -> None:
        """contains: 字符串包含子串时通过。"""
        cond = ExpectCondition(field="output", operator="contains", value="hello")
        assert self.evaluator._check_condition("say hello world", cond) is True

    def test_contains_fails_for_string(self) -> None:
        """contains: 字符串不含子串时不通过。"""
        cond = ExpectCondition(field="output", operator="contains", value="hello")
        assert self.evaluator._check_condition("goodbye world", cond) is False

    def test_contains_passes_for_list(self) -> None:
        """contains: 列表包含元素时通过。"""
        cond = ExpectCondition(field="tags", operator="contains", value="python")
        assert self.evaluator._check_condition(["python", "test"], cond) is True

    # ── 数值比较（修复后支持 gt/lt/gte/lte） ──

    def test_gt_passes(self) -> None:
        """gt: 大于期望值时通过。"""
        cond = ExpectCondition(field="score", operator="gt", value=80)
        assert self.evaluator._check_condition(85, cond) is True

    def test_gt_fails_on_equal(self) -> None:
        """gt: 等于期望值时不通过。"""
        cond = ExpectCondition(field="score", operator="gt", value=80)
        assert self.evaluator._check_condition(80, cond) is False

    def test_lt_passes(self) -> None:
        """lt: 小于期望值时通过。"""
        cond = ExpectCondition(field="count", operator="lt", value=10)
        assert self.evaluator._check_condition(5, cond) is True

    def test_lt_fails_on_equal(self) -> None:
        """lt: 等于期望值时不通过。"""
        cond = ExpectCondition(field="count", operator="lt", value=10)
        assert self.evaluator._check_condition(10, cond) is False

    def test_gte_passes_on_equal(self) -> None:
        """gte: 等于期望值时通过。"""
        cond = ExpectCondition(field="score", operator="gte", value=80)
        assert self.evaluator._check_condition(80, cond) is True

    def test_gte_fails_on_less(self) -> None:
        """gte: 小于期望值时不通过。"""
        cond = ExpectCondition(field="score", operator="gte", value=80)
        assert self.evaluator._check_condition(79, cond) is False

    def test_lte_passes_on_equal(self) -> None:
        """lte: 等于期望值时通过。"""
        cond = ExpectCondition(field="count", operator="lte", value=10)
        assert self.evaluator._check_condition(10, cond) is True

    def test_lte_fails_on_greater(self) -> None:
        """lte: 大于期望值时不通过。"""
        cond = ExpectCondition(field="count", operator="lte", value=10)
        assert self.evaluator._check_condition(11, cond) is False

    # ── 未知操作符 ──

    def test_unknown_operator_returns_false(self) -> None:
        """未知操作符返回 False。"""
        cond = ExpectCondition(field="x", operator="fly", value=None)
        assert self.evaluator._check_condition(42, cond) is False


# ============================================================
# AC-EVAL-03: 嵌套字段路径解析
# ============================================================

class TestNestedFieldResolution:
    """嵌套字段路径解析测试。"""

    def setup_method(self) -> None:
        self.evaluator = ExpectEvaluator()

    def test_top_level_field(self) -> None:
        """单层字段路径解析。"""
        value = self.evaluator._resolve_field({"status": "ok"}, "status")
        assert value == "ok"

    def test_nested_two_levels(self) -> None:
        """两层嵌套路径解析 (data.exit_code)。"""
        data = {"data": {"exit_code": 0, "status": "completed"}}
        value = self.evaluator._resolve_field(data, "data.exit_code")
        assert value == 0

    def test_nested_three_levels(self) -> None:
        """三层嵌套路径解析。"""
        data = {"result": {"data": {"exit_code": 1}}}
        value = self.evaluator._resolve_field(data, "result.data.exit_code")
        assert value == 1

    def test_missing_field_returns_none(self) -> None:
        """字段不存在时返回 None。"""
        value = self.evaluator._resolve_field({"a": 1}, "b")
        assert value is None

    def test_missing_nested_field_returns_none(self) -> None:
        """嵌套字段中间层不存在时返回 None。"""
        data = {"data": {"exit_code": 0}}
        value = self.evaluator._resolve_field(data, "data.missing_field")
        assert value is None

    def test_non_dict_intermediate_returns_none(self) -> None:
        """中间层非字典时返回 None。"""
        data = {"data": "not_a_dict"}
        value = self.evaluator._resolve_field(data, "data.exit_code")
        assert value is None

    def test_empty_field_returns_none(self) -> None:
        """空字段路径返回 None。"""
        value = self.evaluator._resolve_field({"a": 1}, "")
        assert value is None


# ============================================================
# AC-EVAL-02: 组合逻辑（and/or）
# ============================================================

class TestLogicCombinations:
    """条件组合逻辑测试。"""

    def setup_method(self) -> None:
        self.evaluator = ExpectEvaluator()

    def test_and_all_pass(self) -> None:
        """and: 全部条件通过→整体通过。"""
        output = {"status": "success", "score": 95}
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="status", operator="equals", value="success"),
                ExpectCondition(field="score", operator="gt", value=80),
            ],
            logic="and",
        )
        result = self.evaluator.evaluate("test_metric", expect, output)
        assert result.passed is True

    def test_and_one_fail(self) -> None:
        """and: 任一条件失败→整体失败。"""
        output = {"status": "success", "score": 50}
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="status", operator="equals", value="success"),
                ExpectCondition(field="score", operator="gt", value=80),
            ],
            logic="and",
        )
        result = self.evaluator.evaluate("test_metric", expect, output)
        assert result.passed is False

    def test_or_one_pass(self) -> None:
        """or: 任一条件通过→整体通过。"""
        output = {"status": "failed", "score": 95}
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="status", operator="equals", value="success"),
                ExpectCondition(field="score", operator="gt", value=80),
            ],
            logic="or",
        )
        result = self.evaluator.evaluate("test_metric", expect, output)
        assert result.passed is True

    def test_or_all_fail(self) -> None:
        """or: 全部条件失败→整体失败。"""
        output = {"status": "failed", "score": 10}
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="status", operator="equals", value="success"),
                ExpectCondition(field="score", operator="gt", value=80),
            ],
            logic="or",
        )
        result = self.evaluator.evaluate("test_metric", expect, output)
        assert result.passed is False

    def test_no_conditions_default_pass(self) -> None:
        """无条件定义时默认通过。"""
        expect = ExpectSpec(conditions=[])
        result = self.evaluator.evaluate("test_metric", expect, {"x": 1})
        assert result.passed is True


# ============================================================
# AC-EVAL-01: 评估指标类型
# ============================================================

class TestMetricTypes:
    """5 类评估指标类型测试。"""

    def test_all_metric_types_defined(self) -> None:
        """MetricType 包含 tool/agent/human 三种。"""
        values = {t.value for t in MetricType}
        assert "tool" in values
        assert "agent" in values
        assert "human" in values

    def test_file_check_metric_definition(self) -> None:
        """file_check 指标定义正确。"""
        metric = MetricDefinition(
            id="file_check",
            name="文件检查",
            metric_type=MetricType.TOOL,
            evaluator_id="file_read",
        )
        assert metric.metric_type == MetricType.TOOL
        assert metric.evaluator_id == "file_read"

    def test_format_valid_metric_definition(self) -> None:
        """format_valid 指标定义正确。"""
        metric = MetricDefinition(
            id="format_valid",
            name="格式校验",
            metric_type=MetricType.TOOL,
            evaluator_id="schema_evaluator",
        )
        assert metric.metric_type == MetricType.TOOL

    def test_bash_check_metric_definition(self) -> None:
        """bash_check 指标定义正确。"""
        metric = MetricDefinition(
            id="bash_check",
            name="命令检查",
            metric_type=MetricType.TOOL,
            evaluator_id="bash_execute",
        )
        assert metric.metric_type == MetricType.TOOL

    def test_semantic_check_metric_definition(self) -> None:
        """semantic_check 指标定义正确（agent 类型）。"""
        metric = MetricDefinition(
            id="semantic_check",
            name="语义检查",
            metric_type=MetricType.AGENT,
            evaluator_id="evaluator_agent",
        )
        assert metric.metric_type == MetricType.AGENT

    def test_human_review_metric_definition(self) -> None:
        """human_review 指标定义正确（human 类型）。"""
        metric = MetricDefinition(
            id="human_review",
            name="人工审核",
            metric_type=MetricType.HUMAN,
            evaluator_id="human_interaction",
        )
        assert metric.metric_type == MetricType.HUMAN


# ============================================================
# AC-EVAL-04/05: 评估结果映射任务状态
# ============================================================

class TestResultMapper:
    """评估结果→任务状态映射测试。"""

    def setup_method(self) -> None:
        self.mapper = ResultMapper()

    def test_all_pass_maps_to_completed(self) -> None:
        """AC-EVAL-04: 所有指标通过→映射为 True（→completed）。"""
        result = EvaluationResult(
            task_id="task-1",
            results=[
                MetricResult(metric_id="m1", passed=True),
                MetricResult(metric_id="m2", passed=True),
            ],
        )
        assert self.mapper.map_to_task_status(result) is True

    def test_any_fail_maps_to_failed(self) -> None:
        """AC-EVAL-05: 任一指标失败→映射为 False（→failed）。"""
        result = EvaluationResult(
            task_id="task-1",
            results=[
                MetricResult(metric_id="m1", passed=True),
                MetricResult(metric_id="m2", passed=False),
            ],
        )
        assert self.mapper.map_to_task_status(result) is False

    def test_no_metrics_maps_to_failed(self) -> None:
        """无评估指标→映射为 False。"""
        result = EvaluationResult(task_id="task-1", results=[])
        assert self.mapper.map_to_task_status(result) is False

    def test_all_fail_maps_to_failed(self) -> None:
        """全部指标失败→映射为 False。"""
        result = EvaluationResult(
            task_id="task-1",
            results=[
                MetricResult(metric_id="m1", passed=False),
                MetricResult(metric_id="m2", passed=False),
            ],
        )
        assert self.mapper.map_to_task_status(result) is False

    def test_single_pass_maps_to_completed(self) -> None:
        """单个指标通过→映射为 True。"""
        result = EvaluationResult(
            task_id="task-1",
            results=[MetricResult(metric_id="m1", passed=True)],
        )
        assert self.mapper.map_to_task_status(result) is True

    def test_build_summary_contains_counts(self) -> None:
        """评估摘要包含通过数/总数。"""
        result = EvaluationResult(
            task_id="task-1",
            results=[
                MetricResult(metric_id="m1", passed=True),
                MetricResult(metric_id="m2", passed=False, message="失败原因"),
            ],
        )
        summary = self.mapper.build_summary(result)
        assert "1/2" in summary
        assert "PASS" in summary
        assert "FAIL" in summary


# ============================================================
# AC-EVAL-02: ExpectConditionEvaluator 操作符验证（修复后）
# ============================================================

class TestExpectConditionEvaluatorOperators:
    """ExpectConditionEvaluator 的 11 种操作符测试（验证 bug 修复）。"""

    def setup_method(self) -> None:
        self.evaluator = ExpectConditionEvaluator()

    def test_supported_operators_count(self) -> None:
        """支持 11 种操作符。"""
        ops = self.evaluator.get_supported_operators()
        assert len(ops) == 11

    def test_gt_operator_works(self) -> None:
        """gt 操作符正确工作（修复前是 greater_than，现已改为 gt）。"""
        result = self.evaluator.evaluate(
            {"score": 95},
            {"conditions": [{"field": "score", "operator": "gt", "value": 80}]},
        )
        assert result["passed"] is True

    def test_gte_operator_works(self) -> None:
        """gte 操作符正确工作（新增的支持）。"""
        result = self.evaluator.evaluate(
            {"score": 80},
            {"conditions": [{"field": "score", "operator": "gte", "value": 80}]},
        )
        assert result["passed"] is True

    def test_lte_operator_works(self) -> None:
        """lte 操作符正确工作（新增的支持）。"""
        result = self.evaluator.evaluate(
            {"count": 10},
            {"conditions": [{"field": "count", "operator": "lte", "value": 10}]},
        )
        assert result["passed"] is True

    def test_all_11_operators_listed(self) -> None:
        """11 种操作符完整定义。"""
        expected = {
            "is_true", "is_false", "equals", "not_equals",
            "in", "not_in", "contains", "gt", "lt", "gte", "lte",
        }
        actual = set(self.evaluator.get_supported_operators())
        assert expected == actual
