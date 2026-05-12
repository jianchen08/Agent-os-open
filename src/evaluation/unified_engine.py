"""
统一评估引擎

提供基于 EvaluationMetric 配置的统一评估流程，支持多种评估器类型：
- tool: 调用 ToolExecutor 执行内置评估工具
- workflow: 调用 Workflow 执行复杂评估流程（包括语义评估）
- human: 调用 human_evaluator 工具进行人工评估

核心特性：统一评估接口、基于 Jinja2 的参数映射、超时控制、并行批量评估。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.results import (
    EvaluationExecutionResult,
    EvaluationStatus,
    ToolExecutionResult,
)

# 兼容性别名
EvaluationResult = EvaluationExecutionResult
from src.evaluation.types import EvaluatorType
from src.evaluation.mapper import ContextBuilder, MappingError, ParameterMapper
from src.tools.executor import ExecutionContext, ToolExecutor
from src.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class EvaluationNode:
    """
    评估节点

    表示一个带依赖关系的评估单元，用于构建依赖图。
    """

    evaluator: Any
    context: Any
    node_id: str
    dependencies: list[str] = field(default_factory=list)
    level: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchEvaluationResult:
    """
    批量评估结果

    包含多个评估结果的汇总信息。
    """

    results: list[EvaluationExecutionResult] = field(default_factory=list)
    total_count: int = 0
    passed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    all_passed: bool = False
    execution_time_ms: float = 0.0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    def __post_init__(self):
        """初始化后计算统计信息"""
        if self.results:
            self._update_statistics()

    def _update_statistics(self) -> None:
        """更新统计信息"""
        self.total_count = len(self.results)
        self.passed_count = sum(1 for r in self.results if r.passed)
        self.failed_count = sum(
            1 for r in self.results if not r.passed and r.status == EvaluationStatus.FAILED
        )
        self.error_count = sum(
            1 for r in self.results if r.status == EvaluationStatus.ERROR
        )
        self.skipped_count = sum(
            1 for r in self.results if r.status == EvaluationStatus.PENDING and not r.passed
        )
        self.all_passed = self.passed_count == self.total_count and self.total_count > 0

    def add_result(self, result: EvaluationExecutionResult) -> None:
        """
        添加评估结果

        Args:
            result: 评估结果
        """
        self.results.append(result)
        self._update_statistics()

    def to_dict(self) -> dict[str, Any]:
        """
        转换为字典格式

        Returns:
            批量评估结果的字典表示
        """
        return {
            "results": [r.model_dump() for r in self.results],
            "total_count": self.total_count,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "error_count": self.error_count,
            "all_passed": self.all_passed,
            "execution_time_ms": self.execution_time_ms,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass
class BatchEvaluationSummary:
    """
    批量评估汇总结果

    Attributes:
        total: 总评估数
        passed: 通过数
        failed: 失败数
        skipped: 跳过数
        error: 错误数
        all_passed: 是否全部通过
        execution_time_ms: 总执行时间（毫秒）
        results: 详细结果列表
    """

    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    error: int = 0
    all_passed: bool = False
    execution_time_ms: float = 0.0
    results: list[EvaluationExecutionResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "error": self.error,
            "all_passed": self.all_passed,
            "execution_time_ms": self.execution_time_ms,
            "results": [r.model_dump() for r in self.results],
        }


class UnifiedEvaluationEngine:
    """
    统一评估引擎

    基于 EvaluationMetric 配置执行评估，支持 tool/workflow/human 三种评估器类型。
    使用 Jinja2 模板进行参数映射，支持超时控制和并行评估。
    """

    # 默认配置
    DEFAULT_TIMEOUT = 300.0  # 默认超时时间（秒）
    MAX_CONCURRENT = 5  # 最大并发评估数

    def __init__(
        self,
        session: AsyncSession | None = None,
        tool_registry: ToolRegistry | None = None,
        workflow_executor: Callable | None = None,
        default_timeout: float = DEFAULT_TIMEOUT,
        max_concurrent: int = MAX_CONCURRENT,
    ):
        """
        初始化统一评估引擎

        Args:
            session: 数据库会话（可选）
            tool_registry: 工具注册表（可选，默认创建新实例）
            workflow_executor: 工作流执行回调函数（可选）
            default_timeout: 默认超时时间（秒）
            max_concurrent: 最大并发评估数
        """
        self.session = session
        self.workflow_executor = workflow_executor
        self.default_timeout = default_timeout
        self.max_concurrent = max(1, max_concurrent)  # 确保至少为 1

        # 初始化参数映射器
        self.mapper = ParameterMapper(strict_mode=False)
        self.context_builder = ContextBuilder()

        # 初始化工具执行器
        if tool_registry is None:
            tool_registry = ToolRegistry()
        self.tool_executor = ToolExecutor(tool_registry)

        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def evaluate(
        self,
        metric_config: dict[str, Any],
        context: dict[str, Any],
        timeout: float | None = None,
    ) -> EvaluationExecutionResult:
        """
        执行单个评估

        根据 evaluator_type 分发到 tool/workflow/human 执行器。

        Args:
            metric_config: 评估指标配置，包含 evaluator_type、evaluator_id、input_mapping 等
            context: 评估上下文，包含 criteria、evidence、task_goal、metadata
            timeout: 超时时间（秒）

        Returns:
            标准化的 EvaluationResult
        """
        evaluator_type = metric_config.get("evaluator_type", "tool")
        evaluator_id = metric_config.get("evaluator_id", "")
        timeout = timeout or metric_config.get("timeout_seconds", self.default_timeout)

        if not evaluator_id:
            return EvaluationExecutionResult(
                metric_id=metric_config.get("id", ""),
                metric_name=metric_config.get("name", ""),
                passed=False,
                score=0.0,
                status=EvaluationStatus.ERROR,
                output="评估器 ID 不能为空",
                evaluator_type=evaluator_type,
                error="Missing evaluator_id",
            )

        logger.info(f"开始评估: {metric_config.get('name', evaluator_id)} (类型: {evaluator_type})")
        start_time = datetime.now(UTC)

        try:
            # 根据评估器类型分发执行
            if evaluator_type == "tool":
                result = await self._evaluate_tool(metric_config, context, timeout)
            elif evaluator_type == "workflow":
                result = await self._evaluate_workflow(metric_config, context, timeout)
            elif evaluator_type == "human":
                result = await self._evaluate_human(metric_config, context, timeout)
            else:
                raise ValueError(f"不支持的评估器类型: {evaluator_type}")

            # 计算执行时间
            execution_time = (datetime.now(UTC) - start_time).total_seconds()
            result.execution_time_ms = execution_time * 1000

            logger.info(
                f"评估完成: {metric_config.get('name', evaluator_id)} "
                f"(结果: {'通过' if result.passed else '失败'}, 耗时: {execution_time:.2f}s)"
            )

            return result

        except TimeoutError:
            execution_time = (datetime.now(UTC) - start_time).total_seconds()
            logger.warning(f"评估超时: {evaluator_id} (耗时: {execution_time:.2f}s)")

            return EvaluationResult(
                metric_id=metric_config.get("id", ""),
                metric_name=metric_config.get("name", ""),
                passed=False,
                score=0.0,
                status=EvaluationStatus.TIMEOUT,
                output=f"评估超时（限制: {timeout}s）",
                evaluator_type=evaluator_type,
                evaluator_id=evaluator_id,
                duration_ms=int(execution_time * 1000),
                error=f"Timeout after {timeout}s",
            )

        except Exception as e:
            execution_time = (datetime.now(UTC) - start_time).total_seconds()
            logger.exception(f"评估异常: {evaluator_id} - {e}")

            return EvaluationResult(
                metric_id=metric_config.get("id", ""),
                metric_name=metric_config.get("name", ""),
                passed=False,
                score=0.0,
                status=EvaluationStatus.ERROR,
                output=f"评估执行异常: {str(e)}",
                evaluator_type=evaluator_type,
                evaluator_id=evaluator_id,
                duration_ms=int(execution_time * 1000),
                error=str(e),
            )

    async def evaluate_batch(
        self,
        metrics: list[dict[str, Any]],
        context: dict[str, Any],
        parallel: bool = True,
        timeout: float | None = None,
    ) -> BatchEvaluationSummary:
        """
        批量评估

        支持并行或串行执行多个评估指标。

        Args:
            metrics: 评估指标配置列表
            context: 评估上下文
            parallel: 是否并行执行，默认 True
            timeout: 单个评估的超时时间

        Returns:
            批量评估汇总结果
        """
        if not metrics:
            return BatchEvaluationSummary()

        timeout = timeout or self.default_timeout
        start_time = datetime.now(UTC)

        logger.info(f"开始批量评估: {len(metrics)} 个指标 (并行: {parallel})")

        if parallel:
            results = await self._parallel_evaluate(metrics, context, timeout)
        else:
            results = await self._serial_evaluate(metrics, context, timeout)

        # 计算汇总结果
        execution_time = (datetime.now(UTC) - start_time).total_seconds()
        summary = self._create_summary(results, execution_time * 1000)

        logger.info(
            f"批量评估完成: {summary.passed}/{summary.total} 通过, "
            f"{summary.failed} 失败, {summary.error} 错误 "
            f"(耗时: {execution_time:.2f}s)"
        )

        return summary

    async def _evaluate_tool(
        self,
        metric_config: dict[str, Any],
        context: dict[str, Any],
        timeout: float,
    ) -> EvaluationResult:
        """
        执行工具评估

        Args:
            metric_config: 指标配置
            context: 评估上下文
            timeout: 超时时间

        Returns:
            评估结果
        """
        tool_id = metric_config.get("evaluator_id", "")

        # 检查工具是否存在
        if not self.tool_executor._registry.has(tool_id):
            return EvaluationResult(
                metric_id=metric_config.get("id", ""),
                metric_name=metric_config.get("name", ""),
                passed=False,
                score=0.0,
                status=EvaluationStatus.ERROR,
                output=f"工具不存在: {tool_id}",
                evaluator_type="tool",
                evaluator_id=tool_id,
                error=f"Tool not found: {tool_id}",
            )

        # 构建工具输入参数
        tool_inputs = self._build_tool_inputs(metric_config, context)

        # 从 context.metadata 获取 evaluation_record_id（用于嵌套执行记录）
        metadata = context.get("metadata", {})
        evaluation_record_id = metadata.get("evaluation_record_id")

        # 构建执行上下文，传递 evaluation_record_id
        exec_context = ExecutionContext(
            session_id=context.get("metadata", {}).get("task_id", "evaluation"),
            user_id=context.get("metadata", {}).get("user_id"),
            metadata={
                "evaluation": True,
                "evaluation_record_id": evaluation_record_id,
            },
        )

        # 执行工具
        async with self._semaphore:
            tool_result: ToolExecutionResult = await asyncio.wait_for(
                self.tool_executor.execute(
                    tool_name=tool_id,
                    inputs=tool_inputs,
                    context=exec_context,
                ),
                timeout=timeout,
            )

        # 映射输出结果
        output_mapping = metric_config.get("output_mapping", {})
        if tool_result.success:
            result_data = tool_result.data or {}
            return self.mapper.map_outputs(output_mapping, result_data)
        else:
            return EvaluationResult(
                metric_id=metric_config.get("id", ""),
                metric_name=metric_config.get("name", ""),
                passed=False,
                score=0.0,
                status=EvaluationStatus.ERROR,
                output=f"工具执行失败: {tool_result.error}",
                evaluator_type="tool",
                evaluator_id=tool_id,
                error=tool_result.error,
                metadata={"error_code": tool_result.error_code},
            )

    async def _evaluate_workflow(
        self,
        metric_config: dict[str, Any],
        context: dict[str, Any],
        timeout: float,
    ) -> EvaluationResult:
        """
        执行工作流评估

        Args:
            metric_config: 指标配置
            context: 评估上下文
            timeout: 超时时间

        Returns:
            评估结果
        """
        workflow_id = metric_config.get("evaluator_id", "")

        # 检查是否配置了工作流执行器
        if not self.workflow_executor:
            return EvaluationResult(
                metric_id=metric_config.get("id", ""),
                metric_name=metric_config.get("name", ""),
                passed=False,
                score=0.0,
                status=EvaluationStatus.ERROR,
                output="工作流执行器未配置",
                evaluator_type="workflow",
                evaluator_id=workflow_id,
                error="Workflow executor not configured",
            )

        # 构建工作流输入
        workflow_inputs = self._build_workflow_inputs(metric_config, context)

        # 从 context.metadata 获取 evaluation_record_id（用于嵌套执行记录）
        metadata = context.get("metadata", {})
        evaluation_record_id = metadata.get("evaluation_record_id")

        # 执行工作流，传递 evaluation_record_id
        async with self._semaphore:
            workflow_result = await asyncio.wait_for(
                self.workflow_executor(
                    workflow_id=workflow_id,
                    inputs=workflow_inputs,
                    context={
                        "task_id": context.get("metadata", {}).get("task_id"),
                        "evaluation": True,
                        "evaluation_record_id": evaluation_record_id,
                    },
                ),
                timeout=timeout,
            )

        # 映射输出结果
        output_mapping = metric_config.get("output_mapping", {})
        if isinstance(workflow_result, dict):
            return self.mapper.map_outputs(output_mapping, workflow_result)
        else:
            return EvaluationResult(
                metric_id=metric_config.get("id", ""),
                metric_name=metric_config.get("name", ""),
                passed=False,
                score=0.0,
                status=EvaluationStatus.ERROR,
                output=f"工作流返回格式无效: {type(workflow_result)}",
                evaluator_type="workflow",
                evaluator_id=workflow_id,
                error="Invalid workflow result format",
            )

    async def _evaluate_human(
        self,
        metric_config: dict[str, Any],
        context: dict[str, Any],
        timeout: float,
    ) -> EvaluationResult:
        """
        执行人工评估

        通过调用 human_evaluator 工具创建人工审核任务。

        Args:
            metric_config: 指标配置
            context: 评估上下文
            timeout: 超时时间

        Returns:
            评估结果（通常是待审核状态）
        """
        # 构建人工评估输入
        human_inputs = self._build_human_inputs(metric_config, context)

        # 从 context.metadata 获取 evaluation_record_id（用于嵌套执行记录）
        metadata = context.get("metadata", {})
        evaluation_record_id = metadata.get("evaluation_record_id")

        # 构建执行上下文，传递 evaluation_record_id
        exec_context = ExecutionContext(
            session_id=context.get("metadata", {}).get("task_id", "evaluation"),
            user_id=context.get("metadata", {}).get("user_id"),
            metadata={
                "evaluation": True,
                "evaluation_record_id": evaluation_record_id,
            },
        )

        # 执行 human_evaluator 工具
        async with self._semaphore:
            tool_result: ToolExecutionResult = await asyncio.wait_for(
                self.tool_executor.execute(
                    tool_name="human_evaluator",
                    inputs=human_inputs,
                    context=exec_context,
                ),
                timeout=timeout,
            )

        # 映射输出结果
        output_mapping = metric_config.get("output_mapping", {})
        if tool_result.success:
            result_data = tool_result.data or {}
            return self.mapper.map_outputs(output_mapping, result_data)
        else:
            return EvaluationResult(
                metric_id=metric_config.get("id", ""),
                metric_name=metric_config.get("name", ""),
                passed=False,
                score=0.0,
                status=EvaluationStatus.ERROR,
                output=f"人工评估创建失败: {tool_result.error}",
                evaluator_type="human",
                evaluator_id="human_evaluator",
                error=tool_result.error,
            )

    def _build_tool_inputs(
        self,
        metric_config: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """
        构建工具输入参数

        Args:
            metric_config: 指标配置
            context: 评估上下文

        Returns:
            工具输入参数
        """
        # 获取输入映射模板
        input_mapping = metric_config.get("input_mapping", {})

        # 获取默认配置
        default_config = metric_config.get("default_config", {})

        # 构建完整上下文
        full_context = self.context_builder.build_from_metric_config(
            metric_config, context
        )

        # 如果有输入映射，使用模板渲染
        if input_mapping:
            try:
                tool_inputs = self.mapper.map_inputs(input_mapping, full_context)
            except MappingError as e:
                logger.warning(f"输入映射失败，使用默认配置: {e}")
                tool_inputs = default_config.copy()
        else:
            # 无映射时使用默认配置
            tool_inputs = default_config.copy()

        # 添加上下文信息（供工具内部使用）
        tool_inputs["_context"] = {
            "task_id": context.get("metadata", {}).get("task_id"),
            "task_goal": context.get("task_goal", {}),
            "evidence": context.get("evidence", {}),
            "criteria": context.get("criteria", {}),
        }

        return tool_inputs

    def _build_workflow_inputs(
        self,
        metric_config: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """
        构建工作流输入参数

        Args:
            metric_config: 指标配置
            context: 评估上下文

        Returns:
            工作流输入参数
        """
        # 获取输入映射模板
        input_mapping = metric_config.get("input_mapping", {})

        # 构建完整上下文
        full_context = self.context_builder.build_from_metric_config(
            metric_config, context
        )

        # 如果有输入映射，使用模板渲染
        if input_mapping:
            try:
                workflow_inputs = self.mapper.map_inputs(input_mapping, full_context)
            except MappingError as e:
                logger.warning(f"工作流输入映射失败: {e}")
                workflow_inputs = {
                    "criteria": context.get("criteria", {}),
                    "evidence": context.get("evidence", {}),
                    "task_goal": context.get("task_goal", {}),
                }
        else:
            # 默认工作流输入
            workflow_inputs = {
                "criteria": context.get("criteria", {}),
                "evidence": context.get("evidence", {}),
                "task_goal": context.get("task_goal", {}),
            }

        return workflow_inputs

    def _build_human_inputs(
        self,
        metric_config: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """
        构建人工评估输入参数

        Args:
            metric_config: 指标配置
            context: 评估上下文

        Returns:
            人工评估输入参数
        """
        # 获取输入映射模板
        input_mapping = metric_config.get("input_mapping", {})

        # 构建完整上下文
        full_context = self.context_builder.build_from_metric_config(
            metric_config, context
        )

        # 默认人工评估参数
        default_inputs = {
            "title": metric_config.get("name", "人工审核"),
            "description": metric_config.get("description", "请审核以下任务完成结果"),
            "type": "approval",
            "timeout_hours": 24,
        }

        # 如果有输入映射，使用模板渲染
        if input_mapping:
            try:
                human_inputs = self.mapper.map_inputs(input_mapping, full_context)
                # 合并默认值
                for key, value in default_inputs.items():
                    if key not in human_inputs or human_inputs[key] is None:
                        human_inputs[key] = value
            except MappingError as e:
                logger.warning(f"人工评估输入映射失败: {e}")
                human_inputs = default_inputs
        else:
            human_inputs = default_inputs

        return human_inputs

    async def _parallel_evaluate(
        self,
        metrics: list[dict[str, Any]],
        context: dict[str, Any],
        timeout: float,
    ) -> list[EvaluationResult]:
        """
        并行评估多个指标

        Args:
            metrics: 评估指标列表
            context: 评估上下文
            timeout: 超时时间

        Returns:
            评估结果列表
        """
        tasks = [
            self.evaluate(metric, context, timeout)
            for metric in metrics
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 处理异常结果
        processed_results: list[EvaluationResult] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"评估任务异常: {metrics[i].get('name', 'unknown')} - {result}")
                processed_results.append(
                    EvaluationResult(
                        metric_id=metrics[i].get("id", ""),
                        metric_name=metrics[i].get("name", ""),
                        passed=False,
                        score=0.0,
                        status=EvaluationStatus.ERROR,
                        output=f"评估任务异常: {str(result)}",
                        evaluator_type=metrics[i].get("evaluator_type", "tool"),
                        evaluator_id=metrics[i].get("evaluator_id", ""),
                        error=str(result),
                    )
                )
            else:
                processed_results.append(result)

        return processed_results

    async def _serial_evaluate(
        self,
        metrics: list[dict[str, Any]],
        context: dict[str, Any],
        timeout: float,
    ) -> list[EvaluationResult]:
        """
        串行评估多个指标

        Args:
            metrics: 评估指标列表
            context: 评估上下文
            timeout: 超时时间

        Returns:
            评估结果列表
        """
        results: list[EvaluationResult] = []

        for metric in metrics:
            result = await self.evaluate(metric, context, timeout)
            results.append(result)

        return results

    def _create_summary(
        self,
        results: list[EvaluationExecutionResult],
        execution_time_ms: float,
    ) -> BatchEvaluationSummary:
        """
        创建批量评估汇总

        Args:
            results: 评估结果列表
            execution_time_ms: 总执行时间

        Returns:
            批量评估汇总
        """
        total = len(results)
        passed = sum(1 for r in results if r.passed and r.status == EvaluationStatus.PASSED)
        failed = sum(1 for r in results if not r.passed and r.status == EvaluationStatus.FAILED)
        error = sum(1 for r in results if r.status == EvaluationStatus.ERROR)
        skipped = sum(1 for r in results if r.status == EvaluationStatus.PENDING)

        return BatchEvaluationSummary(
            total=total,
            passed=passed,
            failed=failed,
            skipped=skipped,
            error=error,
            all_passed=passed == total and total > 0,
            execution_time_ms=execution_time_ms,
            results=results,
        )

    def calculate_score(
        self,
        metrics: list,
        strategy,
        weights: dict[str, float] | None = None,
        threshold: float = 60.0,
    ) -> float:
        """
        计算综合评分

        根据指定的评分策略计算多个指标的综合评分。

        Args:
            metrics: 指标结果列表（MetricResult 或 EvaluationExecutionResult）
            strategy: 评分策略（ScoringStrategy 枚举）
            weights: 权重配置（用于加权策略）
            threshold: 通过阈值（用于阈值策略）

        Returns:
            综合评分 (0-100)
        """
        from src.evaluation.types import ScoringStrategy

        if not metrics:
            return 0.0

        weights = weights or {}

        if strategy == ScoringStrategy.AVERAGE:
            return sum(m.score for m in metrics) / len(metrics)

        elif strategy == ScoringStrategy.WEIGHTED_AVERAGE:
            total_weight = 0.0
            weighted_sum = 0.0

            for metric in metrics:
                weight = weights.get(getattr(metric, "metric_id", ""), getattr(metric, "weight", 1.0))
                weighted_sum += metric.score * weight
                total_weight += weight

            return weighted_sum / total_weight if total_weight > 0 else 0.0

        elif strategy == ScoringStrategy.MINIMUM:
            return min(m.score for m in metrics) if metrics else 0.0

        elif strategy == ScoringStrategy.MAXIMUM:
            return max(m.score for m in metrics) if metrics else 0.0

        elif strategy == ScoringStrategy.PRODUCT:
            result = 1.0
            for metric in metrics:
                result *= metric.score / 100.0
            return result * 100.0

        elif strategy == ScoringStrategy.THRESHOLD:
            passed_count = sum(1 for m in metrics if m.score >= threshold)
            return (passed_count / len(metrics)) * 100.0 if metrics else 0.0

        elif strategy == ScoringStrategy.RED_LINE_PRIORITY:
            red_line_passed = all(
                m.score >= threshold for m in metrics if getattr(m, "is_red_line", False)
            )
            if not red_line_passed:
                return 0.0
            return sum(m.score for m in metrics) / len(metrics) if metrics else 0.0

        else:
            return sum(m.score for m in metrics) / len(metrics) if metrics else 0.0
