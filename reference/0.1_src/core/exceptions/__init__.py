"""
核心异常定义

提供统一的异常层次结构，所有业务逻辑异常都应继承自这些基类。

异常层次结构:
    BaseAppException
    ├── DomainException (业务逻辑错误)
    │   ├── ValidationException (验证错误)
    │   ├── NotFoundException (资源未找到)
    │   ├── ConflictException (冲突错误)
    │   ├── PermissionException (权限错误)
    │   └── BusinessRuleException (业务规则错误)
    └── SystemException (系统级错误)
        ├── DatabaseException (数据库错误)
        ├── CacheException (缓存错误)
        ├── ExternalServiceException (外部服务错误)
        └── ConfigurationException (配置错误)
"""

# 模块特定异常
from core.exceptions.agent import (
    AgentAlreadyExistsError,
    AgentException,
    AgentExecutionError,
    AgentNotFoundError,
    SubAgentNestingError,
)
from core.exceptions.auth import (
    AuthenticationFailedError,
    AuthException,
    InvalidCredentialsError,
    PermissionDeniedError,
    RateLimitExceededError,
    TokenError,
    TokenExpiredError,
    TokenInvalidError,
    TokenRevokedError,
    UserExistsError,
    UserInactiveError,
    UserNotFoundError,
)
from core.exceptions.base import (
    BaseAppException,
    BusinessRuleException,
    CacheException,
    ConfigurationException,
    ConflictException,
    DatabaseException,
    DomainException,
    ExternalServiceException,
    NotFoundException,
    PermissionException,
    SystemException,
    TimeoutException,
    ValidationException,
)
from core.exceptions.config import (
    ConfigException,
    ConfigNotFoundError,
    ConfigValidationError,
    EndpointNotFoundError,
    EnvVarNotFoundError,
    ModelNotFoundError,
    ProviderNotFoundError,
)
from core.exceptions.cost_control import (
    BudgetExceededException,
    CostControlException,
    QuotaExhaustedException,
)
from core.exceptions.di import (
    CircularDependencyError,
    DIException,
    InvalidServiceFactoryError,
    ServiceAlreadyRegisteredError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from core.exceptions.llm import (
    AuthenticationError,
    BudgetExhaustedError,
    ContentFilterError,
    InvalidRequestError,
    LLMException,
    LLMTimeoutError,
    ModelNotAvailableError,
    RateLimitError,
)
from core.exceptions.reasoning import ReasoningRequiredError
from core.exceptions.tool import (
    ApprovalRequiredError,
    MCPConfigError,
    MCPConnectionError,
    MCPException,
    ToolAlreadyExistsError,
    ToolException,
    ToolExecutionError,
    ToolNotFoundError,
    ToolValidationError,
)

__all__ = [
    # 核心基类
    "BaseAppException",
    "DomainException",
    "SystemException",
    # 通用异常
    "ValidationException",
    "NotFoundException",
    "ConflictException",
    "PermissionException",
    "BusinessRuleException",
    "TimeoutException",
    # 系统异常
    "DatabaseException",
    "CacheException",
    "ExternalServiceException",
    "ConfigurationException",
    # Agent 异常
    "AgentException",
    "AgentNotFoundError",
    "AgentAlreadyExistsError",
    "AgentExecutionError",
    "SubAgentNestingError",
    # LLM 异常
    "LLMException",
    "RateLimitError",
    "AuthenticationError",
    "InvalidRequestError",
    "ModelNotAvailableError",
    "LLMTimeoutError",
    "ContentFilterError",
    "BudgetExhaustedError",
    # Auth 异常
    "AuthException",
    "TokenError",
    "TokenExpiredError",
    "TokenInvalidError",
    "TokenRevokedError",
    "AuthenticationFailedError",
    "InvalidCredentialsError",
    "UserNotFoundError",
    "UserInactiveError",
    "UserExistsError",
    "PermissionDeniedError",
    "RateLimitExceededError",
    # Tool 异常
    "ToolException",
    "ToolNotFoundError",
    "ToolAlreadyExistsError",
    "ToolValidationError",
    "ToolExecutionError",
    "ApprovalRequiredError",
    "MCPException",
    "MCPConnectionError",
    "MCPConfigError",
    "ReasoningRequiredError",
    # Config 异常
    "ConfigException",
    "ConfigNotFoundError",
    "ConfigValidationError",
    "ModelNotFoundError",
    "ProviderNotFoundError",
    "EndpointNotFoundError",
    "EnvVarNotFoundError",
    # Cost Control 异常
    "CostControlException",
    "BudgetExceededException",
    "QuotaExhaustedException",
    # DI 异常
    "DIException",
    "ServiceNotFoundError",
    "ServiceAlreadyRegisteredError",
    "CircularDependencyError",
    "InvalidServiceFactoryError",
    "ServiceValidationError",
]
