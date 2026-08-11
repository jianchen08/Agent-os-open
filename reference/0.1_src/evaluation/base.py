"""
评估系统基础模块

定义评估系统的核心抽象类、数据模型和枚举类型。
提供与现有代码兼容的评估框架基础。

迁移说明：
- EvaluationStatus 已迁移到 src.core.results.evaluation
- EvaluationResult 已迁移到 src.core.results.evaluation (EvaluationExecutionResult)
- 此处保留类型别名以保持向后兼容
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.results import EvaluationExecutionResult, EvaluationStatus
from src.evaluation.types import EvaluatorType

logger = logging.getLogger(__name__)


class MetricResult(BaseModel):
    """
    单个指标评估结果

    表示一个评估指标的详细结果，包含评分、通过状态和改进建议。

    Attributes:
        metric_id: 指标唯一标识符
        name: 指标名称
        passed: 是否通过评估
        score: 评分 (0-100)
        weight: 权重，用于加权计算
        is_red_line: 是否为红线指标（必须通过的指标）
        message: 评估结果消息
        evidence: 证据列表
        details: 详细结果数据

    Examples:
        >>> result = MetricResult(
        ...     metric_id="file_exists",
        ...     name="文件存在性检查",
        ...     passed=True,
        ...     score=100,
        ...     message="文件存在且大小符合要求",
        ... )
    """

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    metric_id: str = Field(..., description="指标唯一标识符")
    name: str = Field(default="", description="指标名称")
    passed: bool = Field(default=False, description="是否通过评估")
    score: float = Field(
        default=0.0,
        ge=0,
        le=100,
        description="评分 (0-100)",
    )
    weight: float = Field(
        default=1.0,
        ge=0,
        description="权重，用于加权计算",
    )
    is_red_line: bool = Field(
        default=False,
        description="是否为红线指标（必须通过的指标）",
    )
    message: str = Field(default="", description="评估结果消息")
    evidence: list[str] = Field(
        default_factory=list,
        description="证据列表",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="详细结果数据",
    )

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: float) -> float:
        """验证评分在有效范围内"""
        if not 0 <= v <= 100:
            raise ValueError("评分必须在 0-100 范围内")
        return v

    @field_validator("weight")
    @classmethod
    def validate_weight(cls, v: float) -> float:
        """验证权重为非负数"""
        if v < 0:
            raise ValueError("权重不能为负数")
        return v


@dataclass
class EvaluationContext:
    """
    评估上下文数据类

    封装评估执行所需的所有上下文信息，包括任务信息、
    预期输出、验收标准等。

    Attributes:
        task_id: 任务ID
        task_name: 任务名称
        task_description: 任务描述
        expected_output: 预期输出
        actual_output: 实际输出
        acceptance_criteria: 验收标准列表
        reference_docs: 参考文档列表
        metadata: 扩展元数据
        timestamp: 评估时间戳

    Examples:
        >>> context = EvaluationContext(
        ...     task_id="task_001",
        ...     task_name="代码生成",
        ...     expected_output={"type": "object"},
        ...     actual_output={"result": "success"},
        ... )
    """

    # 任务信息
    task_id: str = ""
    task_name: str = ""
    task_description: str = ""

    # 输入输出
    expected_output: Any = None
    actual_output: Any = None

    # 评估标准
    acceptance_criteria: list[str] = field(default_factory=list)
    reference_docs: list[str] = field(default_factory=list)

    # 扩展数据
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """
        转换为字典格式

        Returns:
            评估上下文的字典表示
        """
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "task_description": self.task_description,
            "expected_output": self.expected_output,
            "actual_output": self.actual_output,
            "acceptance_criteria": self.acceptance_criteria,
            "reference_docs": self.reference_docs,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvaluationContext:
        """
        从字典创建评估上下文

        Args:
            data: 包含上下文信息的字典

        Returns:
            评估上下文对象
        """
        timestamp_str = data.get("timestamp", "")
        timestamp = datetime.fromisoformat(timestamp_str) if timestamp_str else datetime.now(UTC)

        return cls(
            task_id=data.get("task_id", ""),
            task_name=data.get("task_name", ""),
            task_description=data.get("task_description", ""),
            expected_output=data.get("expected_output"),
            actual_output=data.get("actual_output"),
            acceptance_criteria=data.get("acceptance_criteria", []),
            reference_docs=data.get("reference_docs", []),
            metadata=data.get("metadata", {}),
            timestamp=timestamp,
        )


class Evaluator(ABC):
    """
    评估器抽象基类

    所有评估器的基类，定义评估的标准接口。
    子类必须实现 evaluate 方法。

    支持的评估器类型:
        - ProgrammaticEvaluator: 程序化评估（L1）
        - SemanticEvaluator: 语义评估（L2）
        - UnifiedComparator: 综合对比（L3）

    Examples:
        >>> class MyEvaluator(Evaluator):
        ...     @property
        ...     def evaluator_type(self) -> EvaluatorType:
        ...         return EvaluatorType.PROGRAMMATIC
        ...
        ...     async def evaluate(self, context: EvaluationContext) -> EvaluationExecutionResult:
        ...         # 实现评估逻辑
        ...         return EvaluationExecutionResult(
        ...             metric_id="my_metric",
        ...             passed=True,
        ...             score=100,
        ...             status=EvaluationStatus.PASSED,
        ...         )
    """

    def __init__(self, evaluator_id: str = "", config: dict[str, Any] | None = None):
        """
        初始化评估器

        Args:
            evaluator_id: 评估器唯一标识
            config: 评估器配置
        """
        self._evaluator_id = evaluator_id or self.__class__.__name__
        self._config = config or {}
        self._logger = logging.getLogger(self.__class__.__name__)

    @property
    def evaluator_id(self) -> str:
        """获取评估器ID"""
        return self._evaluator_id

    @property
    @abstractmethod
    def evaluator_type(self) -> EvaluatorType:
        """
        获取评估器类型

        Returns:
            评估器类型枚举值
        """
        ...

    @abstractmethod
    async def evaluate(self, context: EvaluationContext) -> EvaluationExecutionResult:
        """
        执行评估

        子类必须实现此方法，根据评估上下文执行具体的评估逻辑。

        Args:
            context: 评估上下文，包含任务信息、预期输出、实际输出等

        Returns:
            评估结果对象

        Raises:
            EvaluationError: 评估过程中发生错误
        """
        ...

    async def pre_evaluate(self, context: EvaluationContext) -> EvaluationContext:
        """
        评估前预处理

        子类可以重写此方法进行预处理。

        Args:
            context: 原始评估上下文

        Returns:
            处理后的评估上下文
        """
        return context

    async def post_evaluate(
        self,
        context: EvaluationContext,
        result: EvaluationExecutionResult,
    ) -> EvaluationExecutionResult:
        """
        评估后处理

        子类可以重写此方法进行后处理。

        Args:
            context: 评估上下文
            result: 原始评估结果

        Returns:
            处理后的评估结果
        """
        return result

    async def run(self, context: EvaluationContext) -> EvaluationExecutionResult:
        """
        运行完整评估流程

        包括预处理、评估、后处理三个步骤。

        Args:
            context: 评估上下文

        Returns:
            最终评估结果
        """
        # 预处理
        processed_context = await self.pre_evaluate(context)

        # 执行评估
        start_time = datetime.now(UTC)
        try:
            result = await self.evaluate(processed_context)
        except Exception as e:
            self._logger.exception("评估执行失败")
            result = EvaluationExecutionResult(
                metric_id="evaluation_error",
                metric_name="评估错误",
                passed=False,
                score=0,
                status=EvaluationStatus.ERROR,
                message=f"评估执行失败: {str(e)}",
                evaluator_type=self.evaluator_type,
                evaluator_id=self.evaluator_id,
                error=str(e),
            )

        # 计算执行时间
        end_time = datetime.now(UTC)
        execution_time_ms = (end_time - start_time).total_seconds() * 1000
        result.execution_time_ms = execution_time_ms

        # 确保评估器类型和ID正确
        result.evaluator_type = self.evaluator_type
        result.evaluator_id = self.evaluator_id

        # 后处理
        final_result = await self.post_evaluate(processed_context, result)

        return final_result


class EvaluationError(Exception):
    """评估错误异常"""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
