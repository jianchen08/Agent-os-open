"""
预期条件判断器

根据评估指标配置的 expect.conditions 进行条件判断，支持多种运算符和逻辑组合。

核心功能：
1. 支持运算符（含长名/短名别名）：equals, not_equals, contains, not_contains,
   is_true, is_false, greater_than/gt, less_than/lt, gte, lte, in, not_in, matches
2. 支持嵌套字段路径访问（如 result.success）
3. 支持 and/or 逻辑组合

运算符命名兼容两套写法：长名（greater_than）与需求文档短名（gt）均可使用，
便于历史验收标准配置与需求文档约定共存。

使用示例:
    >>> from src.evaluation.expect_evaluator import ExpectConditionEvaluator
    >>>
    >>> evaluator = ExpectConditionEvaluator()
    >>> result = evaluator.evaluate(
    ...     {"status": "success", "score": 95},
    ...     {
    ...         "conditions": [
    ...             {"field": "status", "operator": "equals", "value": "success"},
    ...             {"field": "score", "operator": "greater_than", "value": 80}
    ...         ],
    ...         "logic": "and"
    ...     }
    ... )
    >>> print(result["passed"])  # True
"""

import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class ExpectConditionEvaluator:
    """
    预期条件判断器

    根据评估指标配置的 expect.conditions 进行条件判断。

    支持的运算符（长名与短名别名等价）：
    - equals: 等于
    - not_equals: 不等于
    - contains: 包含（字符串/列表）
    - not_contains: 不包含
    - is_true: 为真
    - is_false: 为假
    - greater_than / gt: 大于
    - less_than / lt: 小于
    - gte: 大于等于
    - lte: 小于等于
    - in: 在列表中
    - not_in: 不在列表中
    - matches: 正则匹配

    Example:
        >>> evaluator = ExpectConditionEvaluator()
        >>> result = evaluator.evaluate(
        ...     {"status_code": 200},
        ...     {"conditions": [{"field": "status_code", "operator": "in", "value": [200, 201, 204]}]}
        ... )
        >>> assert result["passed"] is True
    """

    # 运算符映射表（长名 + 短名别名共存；contains 兼容字符串与列表）
    OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
        "equals": lambda a, b: a == b,
        "not_equals": lambda a, b: a != b,
        "contains": lambda a, b: b in a if isinstance(a, (str, list)) else False,
        "not_contains": lambda a, b: b not in a if isinstance(a, str) else True,
        "is_true": lambda a, b: bool(a) is True,  # noqa: ARG005
        "is_false": lambda a, b: bool(a) is False,  # noqa: ARG005
        "greater_than": lambda a, b: a > b if isinstance(a, (int, float)) and isinstance(b, (int, float)) else False,
        "gt": lambda a, b: a > b if isinstance(a, (int, float)) and isinstance(b, (int, float)) else False,
        "less_than": lambda a, b: a < b if isinstance(a, (int, float)) and isinstance(b, (int, float)) else False,
        "lt": lambda a, b: a < b if isinstance(a, (int, float)) and isinstance(b, (int, float)) else False,
        "gte": lambda a, b: a >= b if isinstance(a, (int, float)) and isinstance(b, (int, float)) else False,
        "lte": lambda a, b: a <= b if isinstance(a, (int, float)) and isinstance(b, (int, float)) else False,
        "in": lambda a, b: a in b if isinstance(b, (list, tuple, set)) else False,
        "not_in": lambda a, b: a not in b if isinstance(b, (list, tuple, set)) else True,
        "matches": lambda a, b: bool(re.match(b, str(a))) if isinstance(a, str) else False,
    }

    def evaluate(self, result: dict[str, Any], expect: dict[str, Any]) -> dict[str, Any]:
        """
        评估结果是否符合预期

        Args:
            result: 评估工具返回的结果
            expect: 预期配置，包含 conditions 和 logic

        Returns:
            评估结果，包含：
            - passed: 是否通过
            - score: 评分（0-100）
            - message: 结果消息
            - details: 详细信息

        Example:
            >>> evaluator = ExpectConditionEvaluator()
            >>> result = evaluator.evaluate(
            ...     {"status": "success"},
            ...     {"conditions": [{"field": "status", "operator": "equals", "value": "success"}]}
            ... )
            >>> print(result["passed"])  # True
        """
        conditions = expect.get("conditions", [])
        logic = expect.get("logic", "and")

        # 无条件配置时，使用默认判断
        if not conditions:
            return self._default_evaluate(result, expect)

        # 执行所有条件判断
        condition_results = []
        for condition in conditions:
            condition_result = self._evaluate_single_condition(result, condition)
            condition_results.append(condition_result)

        # 组合条件结果
        if logic == "and":
            all_passed = all(r["passed"] for r in condition_results)
        else:  # or
            all_passed = any(r["passed"] for r in condition_results)

        # 生成消息
        message = expect.get("pass_message" if all_passed else "fail_message", "")

        return {
            "passed": all_passed,
            "score": 100 if all_passed else 0,
            "message": message,
            "details": {
                "conditions": condition_results,
                "logic": logic,
            },
        }

    def _evaluate_single_condition(
        self,
        result: dict[str, Any],
        condition: dict[str, Any],
    ) -> dict[str, Any]:
        """
        评估单个条件

        Args:
            result: 评估工具返回的结果
            condition: 单个条件配置，包含 field, operator, value

        Returns:
            条件评估结果，包含：
            - field: 字段名
            - operator: 运算符
            - expected: 期望值
            - actual: 实际值
            - passed: 是否通过
        """
        field = condition.get("field", "")
        operator = condition.get("operator", "")
        expected_value = condition.get("value")

        # 从结果中获取字段值
        actual_value = self._get_field_value(result, field)

        # 获取运算符函数
        op_func = self.OPERATORS.get(operator)
        if not op_func:
            logger.warning(f"不支持的运算符: {operator}")
            return {
                "field": field,
                "operator": operator,
                "expected": expected_value,
                "actual": actual_value,
                "passed": False,
                "error": f"不支持的运算符: {operator}",
            }

        # 执行判断
        try:
            passed = op_func(actual_value, expected_value)
        except Exception as e:
            logger.warning(f"条件判断执行失败: {e}")
            passed = False

        return {
            "field": field,
            "operator": operator,
            "expected": expected_value,
            "actual": actual_value,
            "passed": passed,
        }

    def _get_field_value(self, result: dict[str, Any], field: str) -> Any:
        """
        从结果中获取字段值，支持嵌套路径

        Args:
            result: 结果字典
            field: 字段路径，支持点号分隔的嵌套路径（如 "result.success"）

        Returns:
            字段值，如果路径不存在则返回 None

        Example:
            >>> evaluator = ExpectConditionEvaluator()
            >>> value = evaluator._get_field_value(
            ...     {"result": {"success": True}},
            ...     "result.success"
            ... )
            >>> print(value)  # True
        """
        if not field:
            return None

        keys = field.split(".")
        value = result

        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return None

        return value

    def _default_evaluate(
        self,
        result: dict[str, Any],
        expect: dict[str, Any],
    ) -> dict[str, Any]:
        """
        默认判断逻辑（无条件配置时使用）

        兼容旧的 expect.success 和 expect.approved 格式。

        Args:
            result: 评估工具返回的结果
            expect: 预期配置

        Returns:
            评估结果
        """
        # 检查 min_score 字段（质量评估）
        min_score = expect.get("min_score")
        if min_score is not None:
            actual_score = result.get("score", 0)
            passed = actual_score >= min_score
            return {
                "passed": passed,
                "score": actual_score,
                "message": f"分数 {actual_score} {'>=' if passed else '<'} 阈值 {min_score}",
                "details": {
                    "field": "score",
                    "expected": min_score,
                    "actual": actual_score,
                },
            }

        # 无任何预期配置，根据工具返回的 success 判断
        return {
            "passed": result.get("success", False),
            "score": 100 if result.get("success", False) else 0,
            "message": "",
            "details": {},
        }

    @classmethod
    def get_supported_operators(cls) -> list[str]:
        """
        获取支持的运算符列表

        Returns:
            运算符名称列表
        """
        return list(cls.OPERATORS.keys())

    @classmethod
    def validate_condition(cls, condition: dict[str, Any]) -> list[str]:
        """
        验证条件配置的有效性

        Args:
            condition: 条件配置

        Returns:
            错误消息列表（空列表表示验证通过）
        """
        errors = []

        # 检查必需字段
        if not condition.get("field"):
            errors.append("条件缺少 field 字段")

        operator = condition.get("operator")
        if not operator:
            errors.append("条件缺少 operator 字段")
        elif operator not in cls.OPERATORS:
            errors.append(f"不支持的运算符: {operator}")

        # 检查 value 字段（is_true 和 is_false 不需要 value）
        if operator not in ("is_true", "is_false") and "value" not in condition:
            errors.append(f"运算符 {operator} 需要 value 字段")

        return errors

    @classmethod
    def validate_expect(cls, expect: dict[str, Any]) -> list[str]:
        """
        验证预期配置的有效性

        Args:
            expect: 预期配置

        Returns:
            错误消息列表（空列表表示验证通过）
        """
        errors = []

        conditions = expect.get("conditions", [])
        logic = expect.get("logic", "and")

        # 验证逻辑运算符
        if logic not in ("and", "or"):
            errors.append(f"不支持的逻辑运算: {logic}，只支持 and 或 or")

        # 验证每个条件
        for i, condition in enumerate(conditions):
            condition_errors = cls.validate_condition(condition)
            for error in condition_errors:
                errors.append(f"条件 {i + 1}: {error}")

        return errors


# 全局单例
_expect_evaluator: ExpectConditionEvaluator | None = None


def get_expect_evaluator() -> ExpectConditionEvaluator:
    """
    获取预期条件判断器单例

    Returns:
        ExpectConditionEvaluator 实例
    """
    global _expect_evaluator  # noqa: PLW0603
    if _expect_evaluator is None:
        _expect_evaluator = ExpectConditionEvaluator()
    return _expect_evaluator


def reset_expect_evaluator() -> None:
    """
    重置预期条件判断器单例

    主要用于测试场景。
    """
    global _expect_evaluator  # noqa: PLW0603
    _expect_evaluator = None
