"""
评估结果包装器

提供统一的评估结果格式和转换功能
"""

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from src.core.results import EvaluationExecutionResult

logger = logging.getLogger(__name__)


class ResultWrapper:
    """
    结果包装器

    负责将不同评估器的原始结果包装成标准格式
    """

    @staticmethod
    def wrap(
        raw_result: dict[str, Any],
        metric_id: str,
        metric_name: str = "",
        evaluator_type: str = "tool",
        evaluator_id: str = "",
        execution_time_ms: float | None = None,  # noqa: ARG004
    ) -> EvaluationExecutionResult:
        """
        包装原始评估结果

        Args:
            raw_result: 原始评估结果字典
            metric_id: 指标 ID
            metric_name: 指标名称
            evaluator_type: 评估器类型
            evaluator_id: 评估器 ID
            execution_time_ms: 执行时间

        Returns:
            标准化的评估结果
        """
        # 检查是否已经是标准格式
        if "metric_id" in raw_result and "metric_name" in raw_result:
            try:
                return EvaluationExecutionResult.from_dict(raw_result)
            except Exception as e:
                logger.warning(f"尝试解析为标准格式失败: {e}，使用默认转换")

        # 使用默认转换
        return EvaluationExecutionResult.from_dict(
            raw_result,
            metric_id=metric_id,
            metric_name=metric_name,
            evaluator_type=evaluator_type,
            evaluator_id=evaluator_id,
        )

    @staticmethod
    def wrap_multiple(
        raw_results: list[dict[str, Any]],
        metric_id_prefix: str = "",
        evaluator_type: str = "tool",
        evaluator_id: str = "",
    ) -> list[EvaluationExecutionResult]:
        """
        包装多个评估结果

        Args:
            raw_results: 原始评估结果列表
            metric_id_prefix: 指标 ID 前缀
            evaluator_type: 评估器类型
            evaluator_id: 评估器 ID

        Returns:
            标准化的评估结果列表
        """
        wrapped = []
        for i, raw_result in enumerate(raw_results):
            metric_id = raw_result.get("metric_id", f"{metric_id_prefix}_{i}")
            metric_name = raw_result.get("metric_name", f"指标 {i + 1}")
            wrapped.append(
                ResultWrapper.wrap(
                    raw_result,
                    metric_id=metric_id,
                    metric_name=metric_name,
                    evaluator_type=evaluator_type,
                    evaluator_id=evaluator_id,
                )
            )
        return wrapped


class EvaluationSummary(BaseModel):
    """
    评估摘要

    用于汇总多个评估结果
    """

    # 基本信息
    total_metrics: int = Field(..., description="总指标数")
    passed_metrics: int = Field(..., description="通过的指标数")
    failed_metrics: int = Field(..., description="失败的指标数")

    # 评分
    total_score: float = Field(..., description="总分 (0-100)")
    passed: bool = Field(..., description="是否通过")

    # 红线指标
    red_line_failed: bool = Field(default=False, description="红线指标是否失败")

    # 详细信息
    results: list[EvaluationExecutionResult] = Field(default_factory=list, description="详细结果列表")

    # 时间
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), description="评估时间")

    def add_result(self, result: EvaluationExecutionResult) -> None:
        """添加评估结果"""
        self.results.append(result)
        self._recalculate()

    def _recalculate(self) -> None:
        """重新计算汇总信息"""
        self.total_metrics = len(self.results)
        self.passed_metrics = sum(1 for r in self.results if r.passed)
        self.failed_metrics = self.total_metrics - self.passed_metrics

        # 计算平均分
        if self.results:
            self.total_score = sum(r.score for r in self.results) / len(self.results)
        else:
            self.total_score = 0.0

        # 检查是否通过（所有指标都通过）
        self.passed = self.failed_metrics == 0

        # 检查红线指标
        self.red_line_failed = any(r.is_red_line and not r.passed for r in self.results)

    @classmethod
    def from_results(
        cls,
        results: list[EvaluationExecutionResult],
    ) -> "EvaluationSummary":
        """
        从评估结果列表创建摘要

        Args:
            results: 评估结果列表

        Returns:
            评估摘要
        """
        summary = cls(
            total_metrics=len(results),
            passed_metrics=0,
            failed_metrics=0,
            total_score=0.0,
            passed=False,
        )

        for result in results:
            summary.add_result(result)

        return summary


# 兼容性别名
EvaluationResult = EvaluationExecutionResult
