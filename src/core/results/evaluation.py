"""
评估执行结果

暴露接口：
- success(self) -> bool：success功能
- message(self) -> str：message功能
- message(self, value: str) -> None：message功能
- execution_time_ms(self) -> float | None：execution_time_ms功能
- execution_time_ms(self, value: float | None) -> None：execution_time_ms功能
- details(self) -> dict[str, Any]：details功能
- details(self, value: dict[str, Any]) -> None：details功能
- is_success(self) -> bool：is_success功能
- has_error(self) -> bool：has_error功能
- executed_at(self) -> 'datetime | None'：executed_at功能
- executed_at(self, value: 'datetime | None') -> None：executed_at功能
- to_dict(self) -> dict[str, Any]：to_dict功能
- from_legacy_format(cls, data: dict[str, Any], metric_id: str, metric_name: str) -> 'EvaluationExecutionResult'：from_legacy_format功能
- from_dict(cls, data: dict[str, Any], metric_id: str, metric_name: str, evaluator_type: str, evaluator_id: str) -> 'EvaluationExecutionResult'：from_dict功能
- EvaluationStatus：EvaluationStatus类
- EvaluationExecutionResult：EvaluationExecutionResult类
"""

from datetime import datetime  # noqa: F401
from enum import Enum
from typing import Any

from pydantic import Field

from core.results.base import ExecutionResult


class EvaluationStatus(str, Enum):
    """评估状态枚举

    表示评估结果的通过/未通过状态，与 ExecutionStatus 不同：
    - ExecutionStatus 关注执行过程的生命周期
    - EvaluationStatus 关注评估结果的判定状态

    Attributes:
        PENDING: 待评估
        EVALUATING: 评估中
        PASSED: 已通过
        FAILED: 未通过
        TIMEOUT: 超时
        ERROR: 错误
    """

    PENDING = "pending"  # 待评估
    EVALUATING = "evaluating"  # 评估中
    PASSED = "passed"  # 已通过
    FAILED = "failed"  # 未通过
    TIMEOUT = "timeout"  # 超时
    ERROR = "error"  # 错误


class EvaluationExecutionResult(ExecutionResult[Any]):
    """评估执行结果

    继承自 ExecutionResult 基类，添加评估特有字段。

    特有字段：
    - metric_id: 指标 ID
    - metric_name: 指标名称
    - passed: 是否通过
    - score: 评分
    - weight: 权重
    - is_red_line: 是否红线指标
    - evidence: 证据列表
    - suggestions: 改进建议
    - evaluator_type: 评估器类型
    - evaluator_id: 评估器 ID

    Attributes:
        status: 评估状态（使用 EvaluationStatus）
        metric_id: 指标 ID
        metric_name: 指标名称
        passed: 是否通过
        score: 评分 (0-100)
        weight: 权重
        is_red_line: 是否为红线指标（必须通过）
        evidence: 证据列表
        suggestions: 改进建议列表
        evaluator_type: 评估器类型
        evaluator_id: 评估器 ID
    """

    # 评估状态（覆盖基类）
    status: EvaluationStatus = Field(default=EvaluationStatus.PENDING, description="评估状态")

    # 指标标识
    metric_id: str = Field(..., description="指标 ID")
    metric_name: str = Field(default="", description="指标名称")

    # 评估结果
    passed: bool = Field(default=False, description="是否通过")
    score: float = Field(default=0.0, ge=0.0, le=100.0, description="评分 (0-100)")
    weight: float = Field(default=1.0, ge=0.0, description="权重")

    # 红线指标
    is_red_line: bool = Field(default=False, description="是否为红线指标（必须通过）")

    # 证据和建议
    evidence: list[str] = Field(default_factory=list, description="证据列表")
    suggestions: list[str] = Field(default_factory=list, description="改进建议")

    # 评估器信息
    evaluator_type: str = Field(default="tool", description="评估器类型")
    evaluator_id: str = Field(default="", description="评估器 ID")

    @property
    def success(self) -> bool:
        """是否成功（覆盖基类）"""
        return self.passed

    def is_success(self) -> bool:
        """检查评估是否成功"""
        return self.status == EvaluationStatus.PASSED and self.passed

    def has_error(self) -> bool:
        """检查评估是否有错误"""
        return self.status == EvaluationStatus.ERROR or self.error is not None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        # 处理 status 字段（可能是枚举或字符串）
        status_value = self.status.value if isinstance(self.status, EvaluationStatus) else self.status

        result: dict[str, Any] = {
            "metric_id": self.metric_id,
            "metric_name": self.metric_name,
            "status": status_value,
            "passed": self.passed,
            "score": self.score,
            "success": self.passed,
        }

        if self.weight != 1.0:
            result["weight"] = self.weight

        if self.is_red_line:
            result["is_red_line"] = self.is_red_line

        if self.evidence:
            result["evidence"] = self.evidence

        if self.suggestions:
            result["suggestions"] = self.suggestions

        if self.error:
            result["error"] = self.error

        # 兼容性字段
        if self.message:
            result["message"] = self.message

        if self.execution_time_ms is not None:
            result["execution_time_ms"] = self.execution_time_ms

        if self.duration_ms is not None:
            result["duration_ms"] = self.duration_ms

        if self.details:
            result["details"] = self.details

        if self.evaluator_type:
            result["evaluator_type"] = self.evaluator_type
        if self.evaluator_id:
            result["evaluator_id"] = self.evaluator_id

        return result

    @classmethod
    def from_legacy_format(
        cls,
        data: dict[str, Any],
        metric_id: str = "",
        metric_name: str = "",
    ) -> "EvaluationExecutionResult":
        """从旧格式创建（兼容 0-1 分数）"""
        raw_score = data.get("score", 0.0)

        # 自动转换 0-1 分数为 0-100
        score = raw_score * 100 if isinstance(raw_score, (int, float)) and raw_score <= 1.0 else raw_score

        # 解析状态
        status_str = data.get("status", "")
        try:
            status = (
                EvaluationStatus(status_str)
                if status_str
                else (EvaluationStatus.PASSED if data.get("passed") else EvaluationStatus.FAILED)
            )
        except ValueError:
            status = EvaluationStatus.PASSED if data.get("passed") else EvaluationStatus.FAILED

        # 解析 message -> output
        message = data.get("message", "")
        output = message if message else data.get("output")

        # 解析 details -> metadata
        details = data.get("details", {})
        metadata = details if details else data.get("metadata", {})

        return cls(
            metric_id=metric_id or data.get("metric_id", ""),
            metric_name=metric_name or data.get("metric_name", ""),
            status=status,
            passed=data.get("passed", False),
            score=score,
            weight=data.get("weight", 1.0),
            is_red_line=data.get("is_red_line", False),
            evidence=data.get("evidence", []),
            suggestions=data.get("suggestions", []),
            error=data.get("error"),
            output=output,
            metadata=metadata,
            duration_ms=data.get("execution_time_ms") or data.get("duration_ms"),
            evaluator_type=data.get("evaluator_type", "tool"),
            evaluator_id=data.get("evaluator_id", ""),
        )

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        metric_id: str = "",
        metric_name: str = "",
        evaluator_type: str = "tool",
        evaluator_id: str = "",
    ) -> "EvaluationExecutionResult":
        """从字典创建（兼容 EvaluationResult.from_dict）"""
        # 合并参数到 data 中
        merged_data = {**data}
        if metric_id:
            merged_data["metric_id"] = metric_id
        if metric_name:
            merged_data["metric_name"] = metric_name
        if evaluator_type:
            merged_data["evaluator_type"] = evaluator_type
        if evaluator_id:
            merged_data["evaluator_id"] = evaluator_id

        return cls.from_legacy_format(merged_data)
