"""Pydantic 数据模型。

定义 API 请求/响应的数据模型。
内存存储实现已拆分到 memory_store.py。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ============================================================
# 请求/响应模型
# ============================================================


class RefreshRequest(BaseModel):
    """刷新令牌请求模型。"""

    refresh_token: str


class LoginRequest(BaseModel):
    """登录请求模型。"""

    username: str
    password: str


class RegisterRequest(BaseModel):
    """注册请求模型。"""

    username: str
    password: str
    email: str | None = None


class TokenResponse(BaseModel):
    """Token 响应模型。"""

    access_token: str
    refresh_token: str
    expires_in: int = Field(description="access token 有效期（秒）")
    token_type: str = "bearer"


class UserResponse(BaseModel):
    """用户信息响应模型。"""

    id: str
    username: str
    email: str | None = None
    role: str = "user"
    created_at: str


class ThreadCreate(BaseModel):
    """创建线程请求模型。"""

    title: str | None = None
    agent_id: str | None = None
    intent: str | None = None
    metadata: dict[str, Any] | None = None


class ThreadUpdate(BaseModel):
    """更新线程请求模型。"""

    title: str | None = None
    agent_id: str | None = None
    intent: str | None = None
    metadata: dict[str, Any] | None = None


class ThreadResponse(BaseModel):
    """线程响应模型，字段名与前端 mapThreadToSession 对齐。"""

    thread_id: str
    title: str | None = None
    intent: str | None = None
    current_state: str = "active"
    created_at: str
    updated_at: str
    agent_id: str | None = None
    message_count: int = 0
    pipeline_ids: list[str] = Field(default_factory=list, description="关联的管道执行 ID 列表")
    active_pipeline_id: str | None = Field(default=None, description="当前活跃的管道执行 ID")
    metadata: dict[str, Any] | None = Field(default=None, description="线程元数据，含 pinned/starred 等前端状态")


class ThreadListResponse(BaseModel):
    """线程列表分页响应模型。"""

    threads: list[ThreadResponse] = Field(default_factory=list, description="线程列表")
    total: int = Field(default=0, description="线程总数")
    skip: int = Field(default=0, description="当前偏移量")
    limit: int = Field(default=20, description="每页数量")


class MessageListResponse(BaseModel):
    """消息列表分页响应模型。"""

    messages: list[MessageResponse] = Field(default_factory=list, description="消息列表")
    total: int = Field(default=0, description="消息总数")
    has_more: bool = Field(default=False, description="是否还有更多历史消息")


class ToolCallItem(BaseModel):
    """工具调用项（toolCalls[] 子项），字段统一 camelCase 与前端对齐。

    消除历史契约混乱：后端构造子项曾用 snake_case（call_id/tool_name/tool_args），
    前端被迫用 ``tc.callId || tc.call_id`` hack 兼容。统一为 camelCase 单一命名。
    """

    model_config = ConfigDict(populate_by_name=True)

    callId: str = ""  # noqa: N815
    toolName: str = ""  # noqa: N815
    toolArgs: dict[str, Any] | None = None  # noqa: N815
    status: str = "completed"
    result: Any = None
    error: str | None = None
    durationMs: int | None = None  # noqa: N815
    containerTaskId: str | None = None  # noqa: N815


class MessageResponse(BaseModel):
    """消息响应模型，字段名与前端 mapBackendMessageToMessage 对齐。"""

    id: str
    thread_id: str
    role: str
    content: str
    timestamp: str
    sequence: int = 0
    metadata: dict[str, Any] | None = None
    toolCalls: list[ToolCallItem] | None = None  # noqa: N815
    toolCallId: str | None = None  # noqa: N815
    toolName: str | None = None  # noqa: N815
    toolArgs: dict[str, Any] | None = None  # noqa: N815
    toolResult: Any = None  # noqa: N815
    toolError: str | None = None  # noqa: N815
    status: str | None = None
    agentId: str | None = None  # noqa: N815
    agentName: str | None = None  # noqa: N815
    durationMs: int | None = None  # noqa: N815
    attachments: list[dict[str, Any]] | None = None


# ============================================================
# Agent 相关模型
# ============================================================


class AgentResponse(BaseModel):
    """Agent 配置响应模型。"""

    config_id: str
    name: str
    display_name: str = ""
    description: str = ""
    agent_type: str = "specialized"
    category: str = ""
    level: str = "L3"
    system_prompt: str = ""
    tool_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    is_active: bool = True
    version: str = "1.0.0"
    # 解析后的实际模型标识：model_tier 解析优先，model_name 兜底
    # 与运行时 apply_agent_model_override 解析逻辑保持一致，供前端显示当前管道模型
    model: str = ""


class AgentListResponse(BaseModel):
    """Agent 列表响应模型。"""

    items: list[AgentResponse]
    total: int


# ============================================================
# Task 相关模型
# ============================================================


class TaskCreate(BaseModel):
    """创建任务请求模型。"""

    title: str
    description: str | None = None
    agent_id: str | None = None
    priority: int = 5
    tags: list[str] = Field(default_factory=list)
    input_data: dict[str, Any] = Field(default_factory=dict)


class TaskRootCreate(BaseModel):
    """手动创建根任务请求模型。

    用户以 L1 身份手动发起一项工作（等价于 L1 主 agent 调 task_submit 提根任务），
    为 L2+ 子 agent 提供合法的任务上下文。acceptance_criteria 默认空，继承规则
    与 task_submit 一致。
    """

    title: str
    description: str = ""
    task_scope: str = "non_container"  # "container" | "non_container"
    target_id: str = ""  # 非容器必填（执行 agent）；容器为空
    workspace: str = ""
    isolation_level: str = ""  # plain/worktree/shared
    inherit: dict[str, Any] | None = None
    thread_id: str  # 复用当前会话 → 取主管道 + 作 session_id
    parent_task_id: str | None = None  # 父容器任务 ID；有值则挂为子任务，workspace 继承父容器


class TaskUpdate(BaseModel):
    """更新任务请求模型。"""

    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: int | None = None
    tags: list[str] | None = None


class TaskResponse(BaseModel):
    """任务响应模型。"""

    # ── 核心标识 ──
    id: str
    title: str
    description: str | None = None
    status: str = "pending"
    priority: int = 5

    # ── 层级关系 ──
    parent_task_id: str | None = None

    # ── 执行者信息 ──
    agent_id: str | None = None
    agent_name: str | None = None
    agent_level: str | None = None
    thread_id: str | None = None
    created_by: str | None = None

    # ── 管道关联 ──
    pipeline_run_id: str | None = None
    execution_record_id: str | None = None

    # ── 标签与数据 ──
    tags: list[str] = Field(default_factory=list)
    input_data: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None

    # ── 时间 ──
    created_at: str = ""
    updated_at: str = ""

    # ── 元数据 ──
    metadata: dict[str, Any] | None = None


class TaskListResponse(BaseModel):
    """任务列表响应模型。"""

    items: list[TaskResponse]
    total: int


class TaskSubmitResponse(BaseModel):
    """任务提交响应模型。"""

    task_id: str
    status: str
    message: str


class TaskEvaluateRequest(BaseModel):
    """任务评估请求模型。"""

    metric_ids: list[str] = Field(default_factory=list)
    input_params: dict[str, dict[str, Any]] = Field(default_factory=dict)


class TaskEvaluateResponse(BaseModel):
    """任务评估响应模型。"""

    task_id: str
    overall_passed: bool
    summary: str
    results: list[dict[str, Any]] = Field(default_factory=list)


# ============================================================
# Tool 相关模型
# ============================================================


class ToolResponse(BaseModel):
    """工具响应模型，包含工具的完整信息供前端展示。"""

    name: str
    description: str = ""
    category: str = ""
    source: str = ""
    level: str = "all"
    status: str = "active"
    parameters: dict[str, Any] = Field(default_factory=dict)
    when_to_use: list[str] = Field(default_factory=list)
    when_not_to_use: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    version: str = ""
    tags: list[str] = Field(default_factory=list)
    requires_approval: bool = False


class ToolListResponse(BaseModel):
    """工具列表响应模型。"""

    items: list[ToolResponse]
    total: int


# ============================================================
# Memory 相关模型
# ============================================================


class MemorySearchRequest(BaseModel):
    """记忆搜索请求模型。"""

    query: str
    memory_type: str | None = None
    top_k: int = 5
    method: str = "keyword"


class MemoryResponse(BaseModel):
    """记忆条目响应模型。"""

    id: str
    content: str = ""
    memory_type: str = ""
    tags: list[str] = Field(default_factory=list)
    score: float = 0.0
    created_at: str = ""


class MemoryListResponse(BaseModel):
    """记忆列表响应模型。"""

    items: list[MemoryResponse]
    total: int


# ============================================================
# Evaluation 相关模型
# ============================================================


class MetricResponse(BaseModel):
    """评估指标响应模型。"""

    id: str
    name: str = ""
    description: str = ""
    metric_type: str = "tool"
    evaluator_id: str = ""
    is_red_line: bool = False
    default_weight: float = 1.0
    level: int = 1
    tags: list[str] = Field(default_factory=list)
    status: str = "active"


class MetricDetailResponse(MetricResponse):
    """评估指标详情响应模型。"""

    default_config: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    includes: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)


class MetricListResponse(BaseModel):
    """评估指标列表响应模型。"""

    items: list[MetricResponse]
    total: int


# ============================================================
# 通用分页和列表模型
# ============================================================


class PaginatedQuery(BaseModel):
    """分页查询参数。"""

    limit: int = Field(default=20, ge=1, le=100, description="每页数量")
    offset: int = Field(default=0, ge=0, description="偏移量")


class ErrorResponse(BaseModel):
    """标准错误响应模型。"""

    error: dict[str, Any] = Field(description="错误详情")


class HealthResponse(BaseModel):
    """健康检查响应模型。"""

    status: str = "ok"
    version: str = "1.0.0"
    uptime_seconds: float = 0.0


# ============================================================
# 内存存储（已拆分到 memory_store.py）
# ============================================================

from channels.api.memory_store import MemoryStore  # noqa: E402

__all__ = [
    "RefreshRequest",
    "LoginRequest",
    "RegisterRequest",
    "TokenResponse",
    "UserResponse",
    "ThreadCreate",
    "ThreadUpdate",
    "ThreadResponse",
    "ThreadListResponse",
    "MessageListResponse",
    "MessageResponse",
    "AgentResponse",
    "AgentListResponse",
    "TaskCreate",
    "TaskUpdate",
    "TaskResponse",
    "TaskListResponse",
    "TaskSubmitResponse",
    "TaskEvaluateRequest",
    "TaskEvaluateResponse",
    "ToolResponse",
    "ToolListResponse",
    "MemorySearchRequest",
    "MemoryResponse",
    "MemoryListResponse",
    "MetricResponse",
    "MetricDetailResponse",
    "MetricListResponse",
    "PaginatedQuery",
    "ErrorResponse",
    "HealthResponse",
    "MemoryStore",
]
