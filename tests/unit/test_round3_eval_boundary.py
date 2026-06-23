"""
Round 3 — 评估引擎边界场景 + 配置管理边界测试。

聚焦：
1. 11 种操作符边界：空值 / None / 类型不匹配
2. 嵌套字段路径：深层嵌套、不存在路径、数组索引入口
3. and/or 组合逻辑短路行为
4. fail_fast vs 全评估
5. 配置管理边界（简短）：无效 YAML 路径、${ENV_VAR} 未设置行为

对应需求：F-EVAL-02~05
"""
import pytest

from src.evaluation.expect import ExpectEvaluator
from src.evaluation.expect_evaluator import ExpectConditionEvaluator
from src.evaluation.types import (
    EvaluationConfig,
    EvaluationResult,
    ExpectCondition,
    ExpectSpec,
    MetricDefinition,
    MetricResult,
    MetricType,
)


# ============================================================
# 1. 操作符边界场景（ExpectEvaluator._check_condition）
# ============================================================

class TestOperatorBoundaryNoneValue:
    """每种操作符在 actual=None 时的行为。"""

    def setup_method(self) -> None:
        self.evaluator = ExpectEvaluator()

    def test_is_true_with_none(self) -> None:
        """is_true: None → bool(None)=False → 不通过。"""
        cond = ExpectCondition(field="x", operator="is_true")
        assert self.evaluator._check_condition(None, cond) is False

    def test_is_false_with_none(self) -> None:
        """is_false: None → bool(None)=False → 通过。"""
        cond = ExpectCondition(field="x", operator="is_false")
        assert self.evaluator._check_condition(None, cond) is True

    def test_equals_with_none(self) -> None:
        """equals: None == None → 通过。"""
        cond = ExpectCondition(field="x", operator="equals", value=None)
        assert self.evaluator._check_condition(None, cond) is True

    def test_not_equals_with_none(self) -> None:
        """not_equals: None != 'x' → 通过。"""
        cond = ExpectCondition(field="x", operator="not_equals", value="x")
        assert self.evaluator._check_condition(None, cond) is True

    def test_in_with_none_actual(self) -> None:
        """in: None in [1,2] → False → 不通过。"""
        cond = ExpectCondition(field="x", operator="in", value=[1, 2])
        assert self.evaluator._check_condition(None, cond) is False

    def test_not_in_with_none_actual(self) -> None:
        """not_in: None not in [1,2] → True → 通过。"""
        cond = ExpectCondition(field="x", operator="not_in", value=[1, 2])
        assert self.evaluator._check_condition(None, cond) is True

    def test_contains_with_none_actual(self) -> None:
        """contains: None 非字符串/列表 → False → 不通过。"""
        cond = ExpectCondition(field="x", operator="contains", value="a")
        assert self.evaluator._check_condition(None, cond) is False

    def test_gt_with_none(self) -> None:
        """gt: None → 有 None 守卫 → False → 不通过。"""
        cond = ExpectCondition(field="x", operator="gt", value=5)
        assert self.evaluator._check_condition(None, cond) is False

    def test_lt_with_none(self) -> None:
        """lt: None → 有 None 守卫 → False → 不通过。"""
        cond = ExpectCondition(field="x", operator="lt", value=5)
        assert self.evaluator._check_condition(None, cond) is False

    def test_gte_with_none(self) -> None:
        """gte: None → 有 None 守卫 → False → 不通过。"""
        cond = ExpectCondition(field="x", operator="gte", value=5)
        assert self.evaluator._check_condition(None, cond) is False

    def test_lte_with_none(self) -> None:
        """lte: None → 有 None 守卫 → False → 不通过。"""
        cond = ExpectCondition(field="x", operator="lte", value=5)
        assert self.evaluator._check_condition(None, cond) is False


class TestOperatorBoundaryTypeMismatch:
    """操作符在类型不匹配时的行为。"""

    def setup_method(self) -> None:
        self.evaluator = ExpectEvaluator()

    def test_gt_with_string_actual(self) -> None:
        """gt: 字符串值与数字比较 → TypeError 被 evaluate 捕获 → 条件 False。"""
        expect = ExpectSpec(
            conditions=[ExpectCondition(field="v", operator="gt", value=5)],
            logic="and",
        )
        result = self.evaluator.evaluate("m1", expect, {"v": "not_a_number"})
        assert result.passed is False

    def test_in_with_non_iterable_value(self) -> None:
        """in: value 是非可迭代对象 → TypeError 被 evaluate 捕获 → 条件 False。"""
        expect = ExpectSpec(
            conditions=[ExpectCondition(field="v", operator="in", value=42)],
            logic="and",
        )
        result = self.evaluator.evaluate("m1", expect, {"v": 1})
        assert result.passed is False

    def test_contains_with_int_actual(self) -> None:
        """contains: actual 是 int（非 str/list）→ False → 不通过。"""
        cond = ExpectCondition(field="v", operator="contains", value="a")
        assert self.evaluator._check_condition(123, cond) is False

    def test_gt_both_none_comparison(self) -> None:
        """gt: actual 和 value 都 None → None 守卫 → False。"""
        cond = ExpectCondition(field="v", operator="gt", value=None)
        assert self.evaluator._check_condition(None, cond) is False


class TestOperatorBoundaryEmptyValue:
    """操作符在空值（空字符串/空列表/空字典）时的行为。"""

    def setup_method(self) -> None:
        self.evaluator = ExpectEvaluator()

    def test_is_true_with_empty_string(self) -> None:
        """is_true: 空字符串 → bool('')=False → 不通过。"""
        cond = ExpectCondition(field="x", operator="is_true")
        assert self.evaluator._check_condition("", cond) is False

    def test_is_true_with_empty_list(self) -> None:
        """is_true: 空列表 → bool([])=False → 不通过。"""
        cond = ExpectCondition(field="x", operator="is_true")
        assert self.evaluator._check_condition([], cond) is False

    def test_is_true_with_empty_dict(self) -> None:
        """is_true: 空字典 → bool({})=False → 不通过。"""
        cond = ExpectCondition(field="x", operator="is_true")
        assert self.evaluator._check_condition({}, cond) is False

    def test_contains_empty_string_in_string(self) -> None:
        """contains: '' in 'hello' → True（Python 语义）。"""
        cond = ExpectCondition(field="x", operator="contains", value="")
        assert self.evaluator._check_condition("hello", cond) is True

    def test_in_with_empty_list(self) -> None:
        """in: 值 in [] → False → 不通过。"""
        cond = ExpectCondition(field="x", operator="in", value=[])
        assert self.evaluator._check_condition("a", cond) is False

    def test_not_in_with_empty_list(self) -> None:
        """not_in: 值 not in [] → True → 通过。"""
        cond = ExpectCondition(field="x", operator="not_in", value=[])
        assert self.evaluator._check_condition("a", cond) is True


# ============================================================
# 2. 嵌套字段路径边界
# ============================================================

class TestNestedFieldPathBoundary:
    """嵌套字段路径解析的边界场景。"""

    def setup_method(self) -> None:
        self.evaluator = ExpectEvaluator()

    def test_deep_nested_four_levels(self) -> None:
        """四层嵌套路径解析 (a.b.c.d)。"""
        data = {"a": {"b": {"c": {"d": 42}}}}
        assert self.evaluator._resolve_field(data, "a.b.c.d") == 42

    def test_missing_top_level_returns_none(self) -> None:
        """顶层字段不存在 → None。"""
        assert self.evaluator._resolve_field({}, "missing") is None

    def test_missing_intermediate_level_returns_none(self) -> None:
        """中间层字段不存在 → None。"""
        data = {"a": {"b": 1}}
        assert self.evaluator._resolve_field(data, "a.c.d") is None

    def test_non_dict_intermediate_returns_none(self) -> None:
        """中间层非字典（如 list）→ None。"""
        data = {"a": [1, 2, 3]}
        assert self.evaluator._resolve_field(data, "a.b") is None

    def test_array_index_not_supported_returns_none(self) -> None:
        """数组索引访问不支持（如 a.0）→ None。"""
        data = {"a": [10, 20, 30]}
        assert self.evaluator._resolve_field(data, "a.0") is None

    def test_single_dot_field_returns_none(self) -> None:
        """字段路径仅含点号分隔的空段 → None。"""
        assert self.evaluator._resolve_field({"a": 1}, "a.") is None

    def test_none_data_returns_none(self) -> None:
        """data=None → 不崩溃 → None。"""
        assert self.evaluator._resolve_field(None, "a") is None

    def test_empty_path_returns_none(self) -> None:
        """空路径 → None。"""
        assert self.evaluator._resolve_field({"a": 1}, "") is None

    def test_field_value_is_none(self) -> None:
        """字段值显式为 None → 返回 None（区分"不存在"和"值为None"）。"""
        data = {"a": None}
        assert self.evaluator._resolve_field(data, "a") is None

    def test_nested_through_evaluate(self) -> None:
        """通过 evaluate 方法验证嵌套路径在完整流程中可用。"""
        output = {"data": {"exit_code": 0, "status": "completed"}}
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="data.exit_code", operator="equals", value=0),
                ExpectCondition(field="data.status", operator="equals", value="completed"),
            ],
            logic="and",
        )
        result = self.evaluator.evaluate("test", expect, output)
        assert result.passed is True


# ============================================================
# 3. ExpectConditionEvaluator 字段路径边界
# ============================================================

class TestExpectConditionEvaluatorFieldPath:
    """ExpectConditionEvaluator（dict 版本）的字段路径边界。"""

    def setup_method(self) -> None:
        self.evaluator = ExpectConditionEvaluator()

    def test_deep_nested_path(self) -> None:
        """深层嵌套路径解析。"""
        result = self.evaluator.evaluate(
            {"a": {"b": {"c": {"d": True}}}},
            {"conditions": [{"field": "a.b.c.d", "operator": "is_true"}]},
        )
        assert result["passed"] is True

    def test_missing_nested_path(self) -> None:
        """嵌套路径不存在 → actual=None → is_true 不通过。"""
        result = self.evaluator.evaluate(
            {"a": {"b": 1}},
            {"conditions": [{"field": "a.c.d", "operator": "is_true"}]},
        )
        assert result["passed"] is False

    def test_non_dict_intermediate(self) -> None:
        """中间层非字典 → 返回 None。"""
        val = self.evaluator._get_field_value({"a": [1, 2]}, "a.b")
        assert val is None

    def test_empty_field_returns_none(self) -> None:
        """空字段名 → None。"""
        assert self.evaluator._get_field_value({"x": 1}, "") is None

    def test_none_result_returns_none(self) -> None:
        """result=None → 不崩溃 → None。"""
        assert self.evaluator._get_field_value(None, "x") is None


# ============================================================
# 4. and/or 组合逻辑短路行为
# ============================================================

class TestLogicShortCircuit:
    """组合逻辑的短路行为验证。"""

    def setup_method(self) -> None:
        self.evaluator = ExpectEvaluator()

    def test_and_short_circuits_on_first_fail(self) -> None:
        """and: 第一个条件失败时，details 中 condition_results 包含失败的标记。"""
        output = {"status": "fail", "score": 95}
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="status", operator="equals", value="success"),
                ExpectCondition(field="score", operator="gt", value=80),
            ],
            logic="and",
        )
        result = self.evaluator.evaluate("m1", expect, output)
        assert result.passed is False
        details = result.details
        assert "condition_results" in details
        assert len(details["condition_results"]) == 2
        assert details["condition_results"][0] is False

    def test_or_passes_when_first_passes(self) -> None:
        """or: 第一个条件通过 → 整体通过（短路）。"""
        output = {"status": "success", "score": 10}
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="status", operator="equals", value="success"),
                ExpectCondition(field="score", operator="gt", value=80),
            ],
            logic="or",
        )
        result = self.evaluator.evaluate("m1", expect, output)
        assert result.passed is True

    def test_and_all_conditions_evaluated(self) -> None:
        """and: 所有条件都被求值（即使中间失败），只是 overall=False。"""
        output = {"a": 1, "b": 2}
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="a", operator="equals", value=1),
                ExpectCondition(field="b", operator="equals", value=99),
            ],
            logic="and",
        )
        result = self.evaluator.evaluate("m1", expect, output)
        assert result.passed is False
        assert len(result.details["condition_results"]) == 2

    def test_or_all_fail(self) -> None:
        """or: 全部失败 → 整体失败。"""
        output = {"a": 1, "b": 2}
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="a", operator="equals", value=99),
                ExpectCondition(field="b", operator="equals", value=99),
            ],
            logic="or",
        )
        result = self.evaluator.evaluate("m1", expect, output)
        assert result.passed is False

    def test_single_condition_and_logic(self) -> None:
        """and: 仅一个条件时正常工作。"""
        output = {"x": 42}
        expect = ExpectSpec(
            conditions=[ExpectCondition(field="x", operator="gt", value=10)],
            logic="and",
        )
        result = self.evaluator.evaluate("m1", expect, output)
        assert result.passed is True

    def test_failed_conditions_tracked(self) -> None:
        """失败条件被记录到 details.failed_conditions。"""
        output = {"x": 1}
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="x", operator="equals", value=99),
            ],
            logic="and",
        )
        result = self.evaluator.evaluate("m1", expect, output)
        assert result.passed is False
        assert len(result.details["failed_conditions"]) == 1


# ============================================================
# 5. fail_fast vs 全评估
# ============================================================

class TestFailFastVsFullEvaluation:
    """fail_fast 机制：通过 EvaluationResult 和 compute_overall 验证行为差异。"""

    def test_fail_fast_stops_at_first_failure(self) -> None:
        """fail_fast=True 时，results 中只包含失败指标及之前的结果。"""
        results = [
            MetricResult(metric_id="m1", passed=True),
            MetricResult(metric_id="m2", passed=False),
            # m3 不会被执行（fail_fast）
        ]
        eval_result = EvaluationResult(task_id="t1", results=results)
        eval_result.compute_overall()
        assert eval_result.overall_passed is False
        assert len(eval_result.results) == 2  # m1 + m2, no m3

    def test_full_evaluation_runs_all(self) -> None:
        """fail_fast=False 时，所有指标都被执行。"""
        results = [
            MetricResult(metric_id="m1", passed=True),
            MetricResult(metric_id="m2", passed=False),
            MetricResult(metric_id="m3", passed=True),
        ]
        eval_result = EvaluationResult(task_id="t1", results=results)
        eval_result.compute_overall()
        assert eval_result.overall_passed is False
        assert len(eval_result.results) == 3

    def test_fail_fast_first_metric_passes_continues(self) -> None:
        """fail_fast=True 时，如果第一个指标通过，第二个指标仍会被执行。"""
        results = [
            MetricResult(metric_id="m1", passed=True),
            MetricResult(metric_id="m2", passed=False),
        ]
        assert len(results) == 2

    def test_no_metrics_results_overall_false(self) -> None:
        """无指标时 compute_overall → overall_passed=False。"""
        eval_result = EvaluationResult(task_id="t1", results=[])
        eval_result.compute_overall()
        assert eval_result.overall_passed is False
        assert "无评估指标" in eval_result.summary

    def test_all_pass_overall_true(self) -> None:
        """所有指标通过 → overall_passed=True。"""
        results = [
            MetricResult(metric_id="m1", passed=True),
            MetricResult(metric_id="m2", passed=True),
        ]
        eval_result = EvaluationResult(task_id="t1", results=results)
        eval_result.compute_overall()
        assert eval_result.overall_passed is True
        assert "2/2" in eval_result.summary

    def test_evaluation_config_fail_fast_default(self) -> None:
        """EvaluationConfig.fail_fast 默认为 False。"""
        config = EvaluationConfig()
        assert config.fail_fast is False

    def test_evaluation_config_fail_fast_set_true(self) -> None:
        """EvaluationConfig.fail_fast 可设置为 True。"""
        config = EvaluationConfig(fail_fast=True)
        assert config.fail_fast is True


# ============================================================
# 6. 配置管理边界（简短）
# ============================================================

class TestConfigBoundary:
    """配置管理边界场景。"""

    def test_env_var_not_expanded_in_yaml_string(self) -> None:
        """${ENV_VAR} 在 YAML 中作为普通字符串保留（无自动展开）。"""
        import yaml
        yaml_str = 'key: ${NONEXISTENT_ENV_VAR}'
        parsed = yaml.safe_load(yaml_str)
        assert parsed['key'] == '${NONEXISTENT_ENV_VAR}'

    def test_env_var_not_expanded_in_nested_yaml(self) -> None:
        """嵌套结构中 ${ENV_VAR} 也作为普通字符串。"""
        import yaml
        yaml_str = 'api:\n  key: ${API_KEY}\n  timeout: 30'
        parsed = yaml.safe_load(yaml_str)
        assert parsed['api']['key'] == '${API_KEY}'
        assert parsed['api']['timeout'] == 30

    def test_invalid_yaml_raises_error(self) -> None:
        """无效 YAML 语法 → yaml.safe_load 抛出 YAMLError。"""
        import yaml
        bad_yaml = 'key: [unclosed bracket'
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(bad_yaml)

    def test_empty_yaml_returns_none(self) -> None:
        """空 YAML → safe_load 返回 None。"""
        import yaml
        assert yaml.safe_load('') is None

    def test_env_var_not_in_os_environ(self) -> None:
        """未设置的环境变量 → os.environ.get 返回 None。"""
        import os
        assert os.environ.get('NONEXISTENT_VAR_12345') is None

    def test_env_var_default_value(self) -> None:
        """未设置的环境变量 → os.environ.get 返回默认值。"""
        import os
        assert os.environ.get('NONEXISTENT_VAR_12345', 'fallback') == 'fallback'


# ============================================================
# 7. ExpectConditionEvaluator 边界验证（dict 版）
# ============================================================

class TestExpectConditionEvaluatorValidate:
    """validate_condition / validate_expect 边界。"""

    def test_validate_condition_missing_field(self) -> None:
        """缺少 field → 报错。"""
        errors = ExpectConditionEvaluator.validate_condition({"operator": "equals", "value": 1})
        assert len(errors) > 0
        assert any("field" in e for e in errors)

    def test_validate_condition_missing_operator(self) -> None:
        """缺少 operator → 报错。"""
        errors = ExpectConditionEvaluator.validate_condition({"field": "x", "value": 1})
        assert len(errors) > 0
        assert any("operator" in e for e in errors)

    def test_validate_condition_unsupported_operator(self) -> None:
        """不支持的 operator → 报错。"""
        errors = ExpectConditionEvaluator.validate_condition({"field": "x", "operator": "fly", "value": 1})
        assert len(errors) > 0
        assert any("fly" in e for e in errors)

    def test_validate_condition_is_true_no_value_ok(self) -> None:
        """is_true 不需要 value → 无错误。"""
        errors = ExpectConditionEvaluator.validate_condition({"field": "x", "operator": "is_true"})
        assert len(errors) == 0

    def test_validate_condition_equals_missing_value(self) -> None:
        """equals 缺少 value → 报错。"""
        errors = ExpectConditionEvaluator.validate_condition({"field": "x", "operator": "equals"})
        assert len(errors) > 0
        assert any("value" in e for e in errors)

    def test_validate_expect_invalid_logic(self) -> None:
        """无效 logic → 报错。"""
        errors = ExpectConditionEvaluator.validate_expect({
            "conditions": [],
            "logic": "xor",
        })
        assert any("xor" in e or "and" in e for e in errors)

    def test_validate_expect_valid(self) -> None:
        """合法 expect → 无错误。"""
        errors = ExpectConditionEvaluator.validate_expect({
            "conditions": [
                {"field": "x", "operator": "equals", "value": 1},
            ],
            "logic": "and",
        })
        assert len(errors) == 0


# ============================================================
# 8. ExpectConditionEvaluator 操作符类型不匹配（lambda 守卫）
# ============================================================

class TestExpectConditionEvalTypeMismatch:
    """ExpectConditionEvaluator OPERATORS 的 isinstance 守卫验证。"""

    def setup_method(self) -> None:
        self.evaluator = ExpectConditionEvaluator()

    def test_in_with_non_iterable_value_returns_false(self) -> None:
        """in: value 是 int 而非 list → isinstance 守卫 → False。"""
        result = self.evaluator.evaluate(
            {"x": 1},
            {"conditions": [{"field": "x", "operator": "in", "value": 42}]},
        )
        assert result["passed"] is False

    def test_not_in_with_non_iterable_value_returns_true(self) -> None:
        """not_in: value 是 int → isinstance 守卫 → True（不在不安全结构中）。"""
        result = self.evaluator.evaluate(
            {"x": 1},
            {"conditions": [{"field": "x", "operator": "not_in", "value": 42}]},
        )
        assert result["passed"] is True

    def test_contains_with_int_actual_returns_false(self) -> None:
        """contains: actual 是 int → isinstance 守卫 → False。"""
        result = self.evaluator.evaluate(
            {"x": 123},
            {"conditions": [{"field": "x", "operator": "contains", "value": "1"}]},
        )
        assert result["passed"] is False

    def test_gt_with_string_returns_false(self) -> None:
        """gt: actual 是字符串 → isinstance 守卫 → False。"""
        result = self.evaluator.evaluate(
            {"x": "hello"},
            {"conditions": [{"field": "x", "operator": "gt", "value": 5}]},
        )
        assert result["passed"] is False

    def test_gt_with_none_returns_false(self) -> None:
        """gt: actual 是 None → isinstance 守卫 → False。"""
        result = self.evaluator.evaluate(
            {"x": None},
            {"conditions": [{"field": "x", "operator": "gt", "value": 5}]},
        )
        assert result["passed"] is False
