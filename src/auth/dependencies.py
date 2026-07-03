"""
FastAPI 认证依赖

提供用于路由的认证和授权依赖函数
"""

from collections.abc import Callable
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.auth.models import TokenPayload, UserInDB
from src.auth.permission_matrix import Action, Resource
from src.auth.rbac import Permission, RBACManager, Role
from src.auth.token import TokenManager
from src.core.exceptions import (
    PermissionDeniedError,
    TokenExpiredError,
    TokenInvalidError,
    TokenRevokedError,
)

# HTTP Bearer 认证方案
security = HTTPBearer(auto_error=False)

# 全局实例（需要在应用启动时初始化）
_token_manager: TokenManager | None = None
_user_repository = None
_rbac_manager: RBACManager | None = None


def init_auth_dependencies(
    token_manager: TokenManager,
    user_repository,
    rbac_manager: RBACManager | None = None,
) -> None:
    """
    初始化认证依赖

    Args:
        token_manager: Token 管理器
        user_repository: 用户仓库
        rbac_manager: RBAC 管理器（可选）
    """
    global _token_manager, _user_repository, _rbac_manager  # noqa: PLW0603
    _token_manager = token_manager
    _user_repository = user_repository
    _rbac_manager = rbac_manager or RBACManager()


def get_token_manager() -> TokenManager:
    """获取 Token 管理器"""
    if _token_manager is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="认证服务未初始化")
    return _token_manager


def get_rbac_manager() -> RBACManager:
    """获取 RBAC 管理器"""
    if _rbac_manager is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="权限服务未初始化")
    return _rbac_manager


async def get_token_payload(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    token_manager: TokenManager = Depends(get_token_manager),
) -> TokenPayload:
    """
    从请求中提取并验证 Token

    Args:
        credentials: HTTP 认证凭证
        token_manager: Token 管理器

    Returns:
        Token 载荷

    Raises:
        HTTPException: 认证失败
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供认证凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = token_manager.verify_token(credentials.credentials, token_type="access")
        return payload
    except TokenExpiredError:
        raise HTTPException(  # noqa: B904
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except TokenRevokedError:
        raise HTTPException(  # noqa: B904
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 已被撤销",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except TokenInvalidError:
        raise HTTPException(  # noqa: B904
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 无效",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user(
    payload: TokenPayload = Depends(get_token_payload),
) -> UserInDB:
    """
    获取当前认证用户

    Args:
        payload: Token 载荷

    Returns:
        当前用户

    Raises:
        HTTPException: 用户不存在
    """
    if _user_repository is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="用户服务未初始化")

    user_id = UUID(payload.sub)
    user = await _user_repository.get_by_id(user_id)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_current_active_user(
    user: UserInDB = Depends(get_current_user),
) -> UserInDB:
    """
    获取当前活跃用户

    Args:
        user: 当前用户

    Returns:
        活跃用户

    Raises:
        HTTPException: 用户已禁用
    """
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="用户已被禁用")
    return user


def require_role(allowed_roles: list[str]) -> Callable:
    """
    创建角色检查依赖

    Args:
        allowed_roles: 允许的角色列表

    Returns:
        依赖函数
    """

    async def role_checker(
        user: UserInDB = Depends(get_current_active_user),
        rbac_manager: RBACManager = Depends(get_rbac_manager),
    ) -> UserInDB:
        """检查用户角色"""
        user_role = Role(user.role)

        for allowed_role in allowed_roles:
            target_role = Role(allowed_role)
            if rbac_manager.is_role_higher_or_equal(user_role, target_role):
                return user

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"需要以下角色之一: {', '.join(allowed_roles)}",
        )

    return role_checker


def require_permission(permission: Permission) -> Callable:
    """
    创建权限检查依赖

    Args:
        permission: 需要的权限

    Returns:
        依赖函数
    """

    async def permission_checker(
        user: UserInDB = Depends(get_current_active_user),
        rbac_manager: RBACManager = Depends(get_rbac_manager),
    ) -> UserInDB:
        """检查用户权限"""
        try:
            rbac_manager.check_permission(user.role, permission)
            return user
        except PermissionDeniedError:
            raise HTTPException(  # noqa: B904
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要 '{permission.value}' 权限",
            )

    return permission_checker


def require_resource_permission(resource: str, permission: Permission) -> Callable:
    """
    创建资源权限检查依赖

    Args:
        resource: 资源名称
        permission: 需要的权限

    Returns:
        依赖函数
    """

    async def resource_permission_checker(
        user: UserInDB = Depends(get_current_active_user),
        rbac_manager: RBACManager = Depends(get_rbac_manager),
    ) -> UserInDB:
        """检查资源权限"""
        try:
            rbac_manager.check_resource_permission(user.role, resource, permission)
            return user
        except PermissionDeniedError:
            raise HTTPException(  # noqa: B904
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要资源 '{resource}' 的 '{permission.value}' 权限",
            )

    return resource_permission_checker


def require_resource_action(resource: Resource, action: Action) -> Callable:
    """创建资源级操作权限检查依赖。

    基于 资源×操作 权限矩阵进行细粒度权限控制。

    Args:
        resource: 目标资源（如 Resource.TASKS）
        action: 目标操作（如 Action.DELETE）

    Returns:
        依赖函数
    """

    async def resource_action_checker(
        user: UserInDB = Depends(get_current_active_user),
        rbac_manager: RBACManager = Depends(get_rbac_manager),
    ) -> UserInDB:
        """检查资源操作权限"""
        try:
            rbac_manager.check_resource_action_permission(user.role, resource, action)
            return user
        except PermissionDeniedError:
            raise HTTPException(  # noqa: B904
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要资源 '{resource.value}' 的 '{action.value}' 权限",
            )

    return resource_action_checker


# 常用依赖快捷方式
require_admin = require_role(["admin", "super_admin"])
require_super_admin = require_role(["super_admin"])
