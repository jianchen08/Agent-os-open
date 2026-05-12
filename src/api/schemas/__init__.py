"""
API 数据模型模块
"""

from src.api.schemas.agents import (
    AgentCreateRequest,
    AgentListResponse,
    AgentResponse,
    AgentUpdateRequest,
)
from src.api.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshTokenRequest,
    TokenResponse,
    UserResponse,
)
from src.api.schemas.common import (
    MessageResponse,
    PaginatedResponse,
)
from src.api.schemas.projects import (
    ProjectAutoExecuteRequest,
    ProjectAutoExecuteResponse,
    ProjectControlResponse,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectResponse,
)
from src.api.schemas.tasks import (
    AcceptanceCriterionStatus,
    ACEvaluateRequest,
    ACEvaluationResult,
    ExecutePhaseCompleteRequest,
    PhaseCompleteResponse,
    PhaseOutputResponse,
    PreparePhaseCompleteRequest,
    TaskACListResponse,
    TaskACResultResponse,
    TaskPhaseStatusResponse,
)
from src.api.schemas.tools import (
    ToolListResponse,
    ToolResponse,
)

__all__ = [
    # 认证
    "LoginRequest",
    "RefreshTokenRequest",
    "LogoutRequest",
    "TokenResponse",
    "UserResponse",
    # Agent
    "AgentCreateRequest",
    "AgentUpdateRequest",
    "AgentResponse",
    "AgentListResponse",
    # 工具
    "ToolResponse",
    "ToolListResponse",
    # 通用
    "MessageResponse",
    "PaginatedResponse",
    # 长期任务
    "ProjectCreateRequest",
    "ProjectResponse",
    "ProjectListResponse",
    "ProjectAutoExecuteRequest",
    "ProjectAutoExecuteResponse",
    "ProjectControlResponse",
    # 任务阶段
    "TaskPhaseStatusResponse",
    "PreparePhaseCompleteRequest",
    "ExecutePhaseCompleteRequest",
    "PhaseCompleteResponse",
    "PhaseOutputResponse",
    # 任务评估
    "TaskACListResponse",
    "ACEvaluateRequest",
    "ACEvaluationResult",
    "TaskACResultResponse",
    "AcceptanceCriterionStatus",
]
