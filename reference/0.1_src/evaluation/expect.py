"""期望值评估器 — 根据指标定义的 expect 条件判断评估结果。

支持的操作符：
- is_true / is_false: 布尔判断
- equals / not_equals: 等值比较
- in / not_in: 集合包含判断
- contains: 字符串/列表包含判断
- gt / lt / gte / lte: 数值比较

字段路径支持点号分隔的嵌套访问（如 "data.exit_code"）。
"""

from __future__ import annotations

import logging
from typing import Any

from evaluation.types import ExpectCondition, ExpectSpec, MetricResult

logger = logging.getLogger(__name__)


class ExpectEvaluator:
    """期望值评估器。

    根据指标定义的 expect 条件对评估输出进行判定，
    生成 MetricResult。

    用法：
        evaluator = ExpectEvaluator()
        result = evaluator.evaluate(
            metric_id="format_valid",
            expect=metric_def.expect,
            output={"success": True, "data": {"exit_code": 0, "status": "completed"}},
        )
    """

    def evaluate(
        self,
        metric_id: str,
        expect: ExpectSpec,
        output: dict[str, Any],
    ) -> MetricResult:
        """评估输出是否满足期望条件。

        Args:
            metric_id: 指标 ID
            expect: 期望判断标准
            output: 评估器输出的结果字典

        Returns:
            MetricResult 包含通过/失败判定和详情
        """
        if not expect.conditions:
            # 无条件定义时，默认通过
            return MetricResult(
                metric_id=metric_id,
                passed=True,
                message=expect.pass_message,
                details=output,
            )

        condition_results: list[bool] = []
        failed_conditions: list[str] = []

        for cond in expect.conditions:
            try:
                actual = self._resolve_field(output, cond.field)
                passed = self._check_condition(actual, cond)
                condition_results.append(passed)
                if not passed:
                    failed_conditions.append(f"{cond.field} {cond.operator} {cond.value!r} (actual: {actual!r})")
            except Exception as e:
                logger.warning(
                    "Condition check failed for %s.%s: %s",
                    metric_id,
                    cond.field,
                    e,
                )
                condition_results.append(False)
                failed_conditions.append(f"{cond.field}: {e}")

        # 组合逻辑
        if expect.logic == "or":  # noqa: SIM108
            overall = any(condition_results)
        else:  # and
            overall = all(condition_results)

        return MetricResult(
            metric_id=metric_id,
            passed=overall,
            message=expect.pass_message if overall else expect.fail_message,
            details={
                "output": output,
                "failed_conditions": failed_conditions,
                "condition_results": condition_results,
            },
        )

    def _resolve_field(self, data: dict[str, Any], field_path: str) -> Any:
        """解析嵌套字段路径。

        支持点号分隔的路径，如 "data.exit_code" → data["exit_code"]

        Args:
            data: 数据字典
            field_path: 字段路径

        Returns:
            字段值，路径不存在时返回 None
        """
        parts = field_path.split(".")
        current: Any = data
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current

    def _check_condition(self, actual: Any, cond: ExpectCondition) -> bool:  # noqa: PLR0911
        """检查单个条件是否满足。

        Args:
            actual: 实际值
            cond: 期望条件

        Returns:
            条件是否满足
        """
        op = cond.operator

        if op == "is_true":
            return bool(actual) is True
        if op == "is_false":
            return bool(actual) is False
        if op == "equals":
            return actual == cond.value
        if op == "not_equals":
            return actual != cond.value
        if op == "in":
            return actual in cond.value
        if op == "not_in":
            return actual not in cond.value
        if op == "contains":
            if isinstance(actual, (str, list)):
                return cond.value in actual
            return False
        if op == "gt":
            return actual is not None and actual > cond.value
        if op == "lt":
            return actual is not None and actual < cond.value
        if op == "gte":
            return actual is not None and actual >= cond.value
        if op == "lte":
            return actual is not None and actual <= cond.value

        logger.warning("Unknown operator: %s, treating as false", op)
        return False
