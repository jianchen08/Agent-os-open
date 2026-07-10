"""评估结果映射器 — 将评估结果映射到任务状态转换 + 模板评估维度映射。

评估结果 pass/fail 需要映射到任务系统的状态：
- evaluating → completed（评估全部通过）
- evaluating → failed（评估未通过）

映射规则：
1. 所有指标通过 → passed=True → 任务状态转 completed
2. 任一红线指标未通过 → passed=False → 任务状态转 failed
3. 非红线指标未通过 → 根据 fail_fast 配置决定

模板评估维度映射：
- 将 TemplateSpec.evaluation_dimensions 转换为 MetricDefinition 列表
- 模板 = 输出规范 = 评估标准（三位一体）
"""

from __future__ import annotations

import logging
from typing import Any

from evaluation.types import (
    EvaluationResult,
    ExpectCondition,
    ExpectSpec,
    MetricDefinition,
    MetricResult,
    MetricType,
)
from templates.types import EvaluationDimension

logger = logging.getLogger(__name__)


class ResultMapper:
    """评估结果映射器。

    将评估引擎的 MetricResult 列表映射为综合判定，
    供 TaskService 完成状态转换。

    用法：
        mapper = ResultMapper()
        overall_passed = mapper.map_to_task_status(eval_result)
        # overall_passed=True → TaskService.complete_evaluation(task_id, passed=True)
    """

    def map_to_task_status(self, result: EvaluationResult) -> bool:
        """将评估结果映射为任务状态转换判定。

        判定逻辑：
        1. 红线指标未通过 → 整体失败
        2. 所有指标通过 → 整体通过
        3. 部分非红线指标未通过 → 整体失败（当前简化版）

        Args:
            result: 评估结果

        Returns:
            True 表示评估通过（任务转 completed），False 表示不通过（任务转 failed）
        """
        result.compute_overall()
        return result.overall_passed

    def map_single_result(
        self,
        result: MetricResult,
        is_red_line: bool = False,
    ) -> dict[str, Any]:
        """将单个指标评估结果映射为状态转换信息。

        Args:
            result: 单个指标评估结果
            is_red_line: 是否为红线指标

        Returns:
            映射后的状态信息字典，包含：
            - passed: 是否通过
            - is_red_line: 是否红线指标
            - metric_id: 指标 ID
            - message: 结果消息
        """
        return {
            "passed": result.passed,
            "is_red_line": is_red_line,
            "metric_id": result.metric_id,
            "message": result.message,
            "score": result.score,
        }

    def build_summary(self, result: EvaluationResult) -> str:
        """构建评估结果摘要。

        Args:
            result: 评估结果

        Returns:
            人类可读的评估摘要字符串
        """
        lines: list[str] = []
        total = len(result.results)
        passed = sum(1 for r in result.results if r.passed)
        lines.append(f"评估结果: {passed}/{total} 指标通过")

        for r in result.results:
            status = "✅ PASS" if r.passed else "❌ FAIL"
            lines.append(f"  {status} {r.metric_id}: {r.message}")

        return "\n".join(lines)

    def template_dimensions_to_metrics(
        self,
        template_id: str,
        dimensions: list[EvaluationDimension],
    ) -> list[MetricDefinition]:
        """将模板评估维度转换为评估指标定义列表。

        模板 = 输出规范 = 评估标准（三位一体）。
        每个评估维度对应一个 MetricDefinition，
        检查输出中是否包含该维度的关键内容。

        Args:
            template_id: 模板 ID，用于生成 metric_id 前缀
            dimensions: 评估维度列表

        Returns:
            MetricDefinition 列表
        """
        metrics: list[MetricDefinition] = []

        for i, dim in enumerate(dimensions):
            metric_id = f"{template_id}_dim_{i + 1}"

            # 构建期望条件：检查输出中是否包含 check_content 关键内容
            # 使用 agent 类型评估（让 LLM 判断是否满足维度要求）
            conditions: list[ExpectCondition] = []
            if dim.pass_criteria:
                conditions.append(
                    ExpectCondition(
                        field="passed",
                        operator="is_true",
                    )
                )

            expect = ExpectSpec(
                conditions=conditions,
                logic="and",
                pass_message=f"维度 '{dim.name}' 评估通过",
                fail_message=f"维度 '{dim.name}' 评估未通过: {dim.pass_criteria}",
            )

            metric = MetricDefinition(
                id=metric_id,
                name=dim.name,
                description=dim.check_content,
                metric_type=MetricType.AGENT,
                evaluator_id=f"template_{template_id}",
                expect=expect,
                is_red_line=dim.required,
                default_weight=1.0 if dim.required else 0.5,
                tags=["template", template_id],
            )
            metrics.append(metric)

        logger.debug(
            "Template dimensions mapped: template_id=%s, %d dimensions → %d metrics",
            template_id,
            len(dimensions),
            len(metrics),
        )

        return metrics
