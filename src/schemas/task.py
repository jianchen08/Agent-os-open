"""
任务相关的 Pydantic 模式

支持新的数据模型：
- evaluation_metric_ids 替代 acceptance_criteria
- execution_record_id 关联执行记录
"""

from typing import Any

from pydantic import BaseModel, Field

# ============================================================================
# 旧版本模式（保留兼容性）
# ============================================================================


class TaskBase(BaseModel):
    """任务基础模式"""

    title: str = Field(..., description="任务标题")
    agent_id: str | None = Field(None, description="关联的 Agent ID")
    priority: str = Field("medium", description="任务优先级")


class TaskCreate(TaskBase):
    """创建任务模式（旧版，保留兼容性）"""

    goal: dict[str, Any] | None = Field(None, description="任务目标")
    acceptance_criteria: list[dict[str, Any]] | None = Field(
        None, description="验收标准列表（已废弃，使用 evaluation_metric_ids）"
    )
    parent_task_id: str | None = Field(None, description="父任务 ID")


class TaskUpdate(BaseModel):
    """更新任务模式（旧版，保留兼容性）"""

    title: str | None = Field(None, description="任务标题")
    agent_id: str | None = Field(None, description="关联的 Agent ID")
    priority: str | None = Field(None, description="任务优先级")
    status: str | None = Field(None, description="任务状态")
    goal: dict[str, Any] | None = Field(None, description="任务目标")
    acceptance_criteria: list[dict[str, Any]] | None = Field(
        None, description="验收标准列表（已废弃）"
    )
    parent_task_id: str | None = Field(None, description="父任务 ID")


# ============================================================================
# 新版本模式（支持新的数据模型）
# ============================================================================


class EvaluationMetricInfo(BaseModel):
    """评估指标信息"""

    id: str = Field(..., description="指标 ID")
    name: str = Field(..., description="指标名称")
    description: str = Field(..., description="指标描述")
    category: str = Field(..., description="指标分类")
    evaluator_type: str = Field(..., description="评估器类型")
    evaluator_id: str = Field(..., description="评估器 ID")
    includes: list[str] | None = Field(default=None, description="包含的低级指标列表")
    requires: list[str] | None = Field(default=None, description="前置依赖指标列表")
    level: int = Field(1, description="指标层级")


class TaskCreateV2(BaseModel):
    """创建任务模式（V2，支持新的数据模型）"""

    title: str = Field(..., description="任务标题", min_length=1, max_length=255)
    agent_id: str | None = Field(None, description="执行者 ID")
    priority: str = Field("medium", description="任务优先级: low | medium | high")
    goal: dict[str, Any] | None = Field(None, description="任务目标")
    evaluation_metric_ids: list[str] | None = Field(
        default_factory=list, description="评估指标 ID 列表"
    )
    parent_task_id: str | None = Field(None, description="父任务 ID")


class TaskUpdateV2(BaseModel):
    """更新任务模式（V2，支持新的数据模型）"""

    title: str | None = Field(
        None, description="任务标题", min_length=1, max_length=255
    )
    status: str | None = Field(None, description="任务状态")
    goal: dict[str, Any] | None = Field(None, description="任务目标")
    priority: int | None = Field(None, description="任务优先级 (1-10)")
    evaluation_metric_ids: list[str] | None = Field(
        None, description="评估指标 ID 列表"
    )
    metadata: dict[str, Any] | None = Field(
        None, description="元数据（错误信息、扩展信息等）"
    )


class TaskResponse(BaseModel):
    """任务响应模式（符合设计文档规范）"""

    # ==================== 核心标识 ====================
    id: str = Field(..., description="任务 ID")

    # ==================== 层级关系 ====================
    parent_task_id: str | None = Field(None, description="父任务 ID")
    execution_record_id: str | None = Field(None, description="关联的执行记录 ID")

    # ==================== 关联 ====================
    user_id: str | None = Field(None, description="所属用户")
    session_id: str | None = Field(None, description="来源会话")

    # ==================== 定义 ====================
    title: str = Field(..., description="任务标题")
    goal: dict[str, Any] | None = Field(None, description="任务目标")

    # ==================== 执行配置 ====================
    target_type: str | None = Field(
        None, description="目标执行者类型: agent | workflow"
    )
    target_id: str | None = Field(None, description="目标执行者ID")
    target_name: str | None = Field(None, description="目标执行者名称")
    priority: int = Field(5, description="优先级 (1-10)")
    due_date: str | None = Field(None, description="截止日期")
    retry_count: int = Field(0, description="重试次数")
    max_retries: int = Field(3, description="最大重试次数")

    # ==================== 评估指标引用 ====================
    evaluation_metric_ids: list[str] | None = Field(
        default_factory=list, description="评估指标 ID 列表"
    )
    evaluation_metrics: list[EvaluationMetricInfo] | None = Field(
        default_factory=list, description="评估指标详情"
    )

    # ==================== 状态 ====================
    status: str = Field(..., description="任务状态")

    # ==================== 时间 ====================
    started_at: str | None = Field(None, description="开始时间")
    completed_at: str | None = Field(None, description="完成时间")
    created_at: str = Field(..., description="创建时间")

    # ==================== 元数据 ====================
    metadata: dict[str, Any] | None = Field(
        None, description="元数据（错误信息、扩展信息等）"
    )

    # ==================== 子任务列表（递归） ====================
    subtasks: list["TaskResponse"] | None = Field(
        default_factory=list, description="子任务列表"
    )

    class Config:
        from_attributes = True


class TaskDetailResponse(TaskResponse):
    """任务详情响应（V2）

    注意：task_metrics 字段保留以兼容旧接口，
    但当前实现中没有独立的 task_metrics 关联表。
    """

    task_metrics: list[dict[str, Any]] | None = Field(
        default_factory=list, description="任务指标评估状态（保留字段，兼容旧接口）"
    )


class EvaluationStatusResponse(BaseModel):
    """评估状态响应"""

    task_id: str = Field(..., description="任务 ID")
    total_metrics: int = Field(..., description="总指标数")
    pending_metrics: int = Field(..., description="待评估指标数")
    passed_metrics: int = Field(..., description="通过指标数")
    failed_metrics: int = Field(..., description="失败指标数")
    skipped_metrics: int = Field(..., description="跳过指标数")
    progress_percent: float = Field(..., description="进度百分比")
    metrics: list[dict[str, Any]] = Field(..., description="指标状态列表")


# ============================================================================
# 评估指标相关模式
# ============================================================================


class EvaluationMetricCreate(BaseModel):
    """创建评估指标模式（符合设计文档规范）"""

    # ==================== 核心标识 ====================
    name: str = Field(..., description="指标名称（唯一）", min_length=1, max_length=100)

    # ==================== 指标定义 ====================
    description: str = Field(..., description="指标描述", min_length=1)
    category: str = Field(
        ...,
        description="指标分类",
        pattern="^(file|schema|test|code|api|performance|semantic|human)$",
    )

    # ==================== 评估器配置 ====================
    evaluator_type: str = Field(
        ...,
        description="评估器类型",
        pattern="^(tool|agent|workflow|human)$",
    )
    evaluator_id: str = Field(..., description="评估器 ID")
    default_config: dict[str, Any] | None = Field(
        default_factory=dict, description="默认配置"
    )
    input_schema: dict[str, Any] | None = Field(
        default_factory=dict, description="输入参数 Schema"
    )

    # ==================== 使用边界 ====================
    when_to_use: list[str] | None = Field(None, description="适用场景列表")
    when_not_to_use: list[str] | None = Field(None, description="不适用场景列表")
    examples: list[dict[str, Any]] | None = Field(None, description="使用示例列表")
    caveats: list[str] | None = Field(None, description="注意事项列表")

    # ==================== 使用边界属性 ====================
    is_red_line: bool = Field(False, description="是否红线指标")
    default_weight: float = Field(1.0, description="默认权重")

    # ==================== 依赖关系 ====================
    includes: list[str] | None = Field(default=None, description="包含的低级指标列表")
    requires: list[str] | None = Field(default=None, description="前置依赖指标列表")
    level: int = Field(1, description="指标层级")

    # ==================== 元数据 ====================
    source: str = Field("custom", description="来源: builtin | generated | custom")
    status: str = Field("active", description="状态: active | inactive | deprecated")
    tags: list[str] | None = Field(default_factory=list, description="标签")


class EvaluationMetricUpdate(BaseModel):
    """更新评估指标模式（符合设计文档规范）"""

    # ==================== 指标定义 ====================
    description: str | None = Field(None, description="指标描述")
    category: str | None = Field(
        None,
        description="指标分类",
        pattern="^(file|schema|test|code|api|performance|semantic|human)$",
    )

    # ==================== 评估器配置 ====================
    evaluator_type: str | None = Field(
        None,
        description="评估器类型",
        pattern="^(tool|agent|workflow|human)$",
    )
    evaluator_id: str | None = Field(None, description="评估器 ID")
    default_config: dict[str, Any] | None = Field(None, description="默认配置")
    input_schema: dict[str, Any] | None = Field(None, description="输入参数 Schema")

    # ==================== 使用边界 ====================
    when_to_use: list[str] | None = Field(None, description="适用场景列表")
    when_not_to_use: list[str] | None = Field(None, description="不适用场景列表")
    examples: list[dict[str, Any]] | None = Field(None, description="使用示例列表")
    caveats: list[str] | None = Field(None, description="注意事项列表")

    # ==================== 使用边界属性 ====================
    is_red_line: bool | None = Field(None, description="是否红线指标")
    default_weight: float | None = Field(None, description="默认权重")

    # ==================== 依赖关系 ====================
    includes: list[str] | None = Field(default=None, description="包含的低级指标列表")
    requires: list[str] | None = Field(default=None, description="前置依赖指标列表")
    level: int | None = Field(None, description="指标层级")

    # ==================== 元数据 ====================
    status: str | None = Field(
        None,
        description="状态",
        pattern="^(active|inactive|deprecated)$",
    )
    tags: list[str] | None = Field(None, description="标签")


class EvaluationMetricResponse(BaseModel):
    """评估指标响应（符合设计文档规范）"""

    # ==================== 核心标识 ====================
    id: str = Field(..., description="指标 ID")
    name: str = Field(..., description="指标名称")

    # ==================== 指标定义 ====================
    description: str = Field(..., description="指标描述")
    category: str = Field(..., description="指标分类")

    # ==================== 评估器配置 ====================
    evaluator_type: str = Field(..., description="评估器类型")
    evaluator_id: str = Field(..., description="评估器 ID")
    default_config: dict[str, Any] | None = Field(
        default_factory=dict, description="默认配置"
    )
    input_schema: dict[str, Any] | None = Field(
        default_factory=dict, description="输入参数 Schema"
    )

    # ==================== 使用边界 ====================
    when_to_use: list[str] | None = Field(None, description="适用场景列表")
    when_not_to_use: list[str] | None = Field(None, description="不适用场景列表")
    examples: list[dict[str, Any]] | None = Field(None, description="使用示例列表")
    caveats: list[str] | None = Field(None, description="注意事项列表")

    # ==================== 使用边界属性 ====================
    is_red_line: bool = Field(False, description="是否红线指标")
    default_weight: float = Field(1.0, description="默认权重")

    # ==================== 依赖关系 ====================
    includes: list[str] | None = Field(default=None, description="包含的低级指标列表")
    requires: list[str] | None = Field(default=None, description="前置依赖指标列表")
    level: int = Field(1, description="指标层级")

    # ==================== 元数据 ====================
    source: str = Field(..., description="来源")
    status: str = Field(..., description="状态")
    tags: list[str] | None = Field(default_factory=list, description="标签")

    # ==================== 统计 ====================
    usage_count: int = Field(0, description="使用次数")
    success_count: int = Field(0, description="成功次数")
    avg_execution_time: float | None = Field(None, description="平均执行时间（秒）")

    # ==================== 时间 ====================
    created_at: str = Field(..., description="创建时间")
    updated_at: str | None = Field(None, description="更新时间")


# ============================================================================
# ExecutionRecord 相关模式（支持新的 5 字段设计）
# ============================================================================


class ExecutionRecordCreate(BaseModel):
    """创建执行记录模式（V2，支持新的 5 字段设计）"""

    session_id: str = Field(..., description="会话 ID")
    parent_record_id: str | None = Field(None, description="父记录 ID")
    message_data: dict[str, Any] = Field(
        ...,
        description="完整的消息数据，包含所有执行细节",
    )


class ExecutionRecordResponse(BaseModel):
    """执行记录响应（V2，支持新的 5 字段设计）"""

    id: str = Field(..., description="记录 ID")
    session_id: str = Field(..., description="会话 ID")
    parent_record_id: str | None = Field(None, description="父记录 ID")
    message_data: dict[str, Any] = Field(..., description="完整的消息数据")
    created_at: str = Field(..., description="创建时间")


# 解决循环引用
TaskResponse.model_rebuild()
