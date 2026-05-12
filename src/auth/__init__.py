"""
认证授权模块

提供用户认证、JWT 管理和 RBAC 权限控制
"""

from src.auth.dependencies import (
    get_current_active_user,
    get_current_user,
    get_token_payload,
    init_auth_dependencies,
    require_admin,
    require_permission,
    require_resource_permission,
    require_role,
    require_super_admin,
)
from src.auth.models import TokenPair, TokenPayload, UserCreate, UserInDB
from src.auth.rbac import Permission, RBACManager, Role
from src.auth.service import AuthService
from src.auth.token import TokenManager
from src.core.exceptions.auth import (
    AuthenticationFailedError as AuthenticationError,
)
from src.core.exceptions.auth import (
    AuthException as AuthError,
)
from src.core.exceptions.auth import (
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

__all__ = [
    # 模型
    "UserCreate",
    "UserInDB",
    "TokenPayload",
    "TokenPair",
    # Token 管理
    "TokenManager",
    # 认证服务
    "AuthService",
    # RBAC
    "Permission",
    "Role",
    "RBACManager",
    # 依赖
    "init_auth_dependencies",
    "get_current_user",
    "get_current_active_user",
    "get_token_payload",
    "require_role",
    "require_permission",
    "require_resource_permission",
    "require_admin",
    "require_super_admin",
    # 异常
    "AuthError",
    "TokenError",
    "TokenExpiredError",
    "TokenInvalidError",
    "TokenRevokedError",
    "AuthenticationError",
    "InvalidCredentialsError",
    "UserNotFoundError",
    "UserInactiveError",
    "UserExistsError",
    "PermissionDeniedError",
    "RateLimitExceededError",
]
