"""
任务阶段和评估相关 Schema
"""

from typing import Any

from pydantic import BaseModel, Field

# ============================================================================
# 任务阶段相关
# ============================================================================


class PhaseStatusInfo(BaseModel):
    """阶段状态信息"""

    status: str = Field(..., description="阶段状态")
    start_time: str | None = Field(None, description="开始时间")
    end_time: str | None = Field(None, description="结束时间")
    output: dict[str, Any] | None = Field(None, description="阶段产物")
    error: str | None = Field(None, description="错误信息")


class TaskPhaseStatusResponse(BaseModel):
    """任务阶段状态响应"""

    task_id: str = Field(..., description="任务 ID")
    current_phase: str | None = Field(None, description="当前阶段")
    task_status: str = Field(..., description="任务状态")
    phases: dict[str, PhaseStatusInfo] = Field(
        default_factory=dict, description="各阶段状态"
    )


class PreparePhaseCompleteRequest(BaseModel):
    """完成准备阶段请求"""

    output: dict[str, Any] = Field(..., description="准备阶段产物")


class ExecutePhaseCompleteRequest(BaseModel):
    """完成执行阶段请求"""

    result: dict[str, Any] | None = Field(None, description="执行结果")


class EvaluatePhaseCompleteRequest(BaseModel):
    """完成评估阶段请求"""

    eval_result: dict[str, Any] = Field(..., description="评估结果")


class PhaseCompleteResponse(BaseModel):
    """阶段完成响应"""

    task_id: str = Field(..., description="任务 ID")
    current_phase: str = Field(..., description="当前阶段")
    task_status: str = Field(..., description="任务状态")
    completed_at: str = Field(..., description="完成时间")


class PhaseOutputResponse(BaseModel):
    """阶段产物响应"""

    task_id: str = Field(..., description="任务 ID")
    phase: str = Field(..., description="阶段名称")
    status: str = Field(..., description="阶段状态")
    output: dict[str, Any] | None = Field(None, description="阶段产物")
    start_time: str | None = Field(None, description="开始时间")
    end_time: str | None = Field(None, description="结束时间")


# ============================================================================
# AC 评估相关
# ============================================================================


class AcceptanceCriterionStatus(BaseModel):
    """验收标准状态"""

    id: str = Field(..., description="AC ID")
    description: str = Field(..., description="AC 描述")
    type: str | None = Field(None, description="AC 类型")
    is_red_line: bool = Field(default=False, description="是否红线指标")
    weight: float = Field(default=1.0, description="权重")
    status: str = Field(..., description="AC 状态")
    evaluator_type: str | None = Field(None, description="评估器类型")
    evaluator_id: str | None = Field(None, description="评估器 ID")
    evaluated_at: str | None = Field(None, description="评估时间")
    retry_count: int = Field(default=0, description="重试次数")
    evaluation_result: dict[str, Any] | None = Field(None, description="评估结果详情")


class TaskACListResponse(BaseModel):
    """任务 AC 列表响应"""

    task_id: str = Field(..., description="任务 ID")
    total: int = Field(..., description="总数", ge=0)
    passed: int = Field(..., description="通过数", ge=0)
    failed: int = Field(..., description="失败数", ge=0)
    pending: int = Field(..., description="待评估数", ge=0)
    acceptance_criteria: list[AcceptanceCriterionStatus] = Field(
        ..., description="验收标准列表"
    )


class ACEvaluateRequest(BaseModel):
    """评估 AC 请求"""

    evidence: dict[str, Any] | None = Field(None, description="评估证据")


class ACEvaluationResult(BaseModel):
    """AC 评估结果"""

    task_id: str = Field(..., description="任务 ID")
    ac_id: str = Field(..., description="AC ID")
    passed: bool = Field(..., description="是否通过")
    score: float = Field(..., description="评分", ge=0, le=100)
    feedback: str = Field(..., description="反馈说明")
    details: dict[str, Any] | None = Field(None, description="详细信息")
    execution_time: float = Field(..., description="执行耗时（秒）", ge=0)
    evaluated_at: str = Field(..., description="评估时间")


class TaskACResultResponse(BaseModel):
    """任务 AC 评估结果响应"""

    task_id: str = Field(..., description="任务 ID")
    ac_id: str = Field(..., description="AC ID")
    status: str = Field(..., description="AC 状态")
    evaluation_result: dict[str, Any] | None = Field(None, description="评估结果")
    evaluated_at: str | None = Field(None, description="评估时间")
