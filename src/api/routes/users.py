"""
用户管理路由

提供用户管理相关的 API 端点（管理员专用）
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import generate_trace_id, get_current_user
from src.api.errors import create_error_response
from src.api.schemas.auth import UserResponse
from src.api.schemas.common import MessageResponse
from src.auth.models import UserCreate, UserInDB
from src.auth.password import hash_password
from src.db.connection import get_async_session
from src.db.repositories import UserRepository

router = APIRouter()


# ============================================================================
# 数据模型
# ============================================================================


class UserSettingsResponse(BaseModel):
    """用户设置响应"""

    default_agent_id: str | None = Field(None, description="默认 Agent ID")
    preferences: dict[str, Any] = Field(
        default_factory=dict, description="用户偏好设置"
    )


class UserSettingsUpdateRequest(BaseModel):
    """用户设置更新请求"""

    default_agent_id: str | None = Field(None, description="默认 Agent ID")
    preferences: dict[str, Any] | None = Field(None, description="用户偏好设置")


# ============================================================================
# 依赖注入
# ============================================================================


async def get_user_repository(
    session: AsyncSession = Depends(get_async_session),
) -> UserRepository:
    """
    获取用户仓库实例（依赖注入）

    Args:
        session: 数据库会话

    Returns:
        UserRepository 实例
    """
    return UserRepository(session)


async def require_admin(current_user: UserInDB = Depends(get_current_user)) -> UserInDB:
    """
    要求管理员权限

    Args:
        current_user: 当前用户

    Returns:
        当前用户

    Raises:
        HTTPException: 非管理员用户
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限"
        )
    return current_user


@router.get(
    "/settings",
    response_model=UserSettingsResponse,
    summary="获取用户设置",
    description="获取当前用户的设置",
    responses={
        200: {"description": "获取成功"},
        401: {"description": "未认证"},
    },
)
async def get_user_settings(
    current_user: UserInDB = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> UserSettingsResponse:
    """
    获取用户设置

    Args:
        current_user: 当前用户

    Returns:
        用户设置
    """
    user_repo = await get_user_repository(session)

    # 从数据库获取用户偏好设置
    user = await user_repo.get(current_user.id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    preferences = user.preferences or {}
    default_agent_id = preferences.get("default_agent_id")

    return UserSettingsResponse(
        default_agent_id=default_agent_id, preferences=preferences
    )


@router.put(
    "/settings",
    response_model=UserSettingsResponse,
    summary="更新用户设置",
    description="更新当前用户的设置",
    responses={
        200: {"description": "更新成功"},
        401: {"description": "未认证"},
    },
)
async def update_user_settings(
    request: UserSettingsUpdateRequest,
    current_user: UserInDB = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> UserSettingsResponse:
    """
    更新用户设置

    Args:
        request: 更新请求
        current_user: 当前用户
        session: 数据库会话

    Returns:
        更新后的设置
    """
    user_repo = await get_user_repository(session)

    # 获取当前用户
    user = await user_repo.get(current_user.id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")

    # 更新用户偏好设置
    current_preferences = user.preferences or {}

    # 合并新的偏好设置
    if request.preferences:
        current_preferences.update(request.preferences)

    # 设置默认Agent ID
    if request.default_agent_id is not None:
        current_preferences["default_agent_id"] = request.default_agent_id

    # 保存到数据库
    await user_repo.update(current_user.id, {"preferences": current_preferences})
    await session.commit()

    return UserSettingsResponse(
        default_agent_id=current_preferences.get("default_agent_id"),
        preferences=current_preferences,
    )


@router.get(
    "",
    response_model=list[UserResponse],
    summary="获取用户列表",
    description="获取所有用户列表（管理员专用）",
    responses={
        200: {"description": "获取成功"},
        401: {"description": "未认证"},
        403: {"description": "权限不足"},
    },
)
async def get_users(
    skip: int = 0,
    limit: int = 100,
    current_user: UserInDB = Depends(require_admin),
    user_repo: UserRepository = Depends(get_user_repository),
) -> list[UserResponse]:
    """
    获取用户列表

    Args:
        skip: 跳过的记录数
        limit: 返回的记录数限制
        current_user: 当前用户（管理员）
        user_repo: 用户仓库

    Returns:
        用户列表
    """
    try:
        users = await user_repo.get_all(skip=skip, limit=limit)
        return [
            UserResponse(
                id=user.id,
                username=user.username,
                email=user.email,
                role=user.role,
                created_at=user.created_at,
                last_login_at=None,
            )
            for user in users
        ]
    except Exception as e:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="USER_001", trace_id=trace_id, path="/api/v1/users"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{error.model_dump(mode='json')} - {str(e)}",
        )


@router.get(
    "/stats",
    summary="获取用户统计",
    description="获取用户统计数据（管理员专用）",
    responses={
        200: {"description": "获取成功"},
        401: {"description": "未认证"},
        403: {"description": "权限不足"},
    },
)
async def get_user_stats(
    current_user: UserInDB = Depends(require_admin),
    user_repo: UserRepository = Depends(get_user_repository),
):
    """
    获取用户统计

    Args:
        current_user: 当前用户（管理员）
        user_repo: 用户仓库

    Returns:
        统计数据
    """
    try:
        total_users = await user_repo.count_total()
        active_users = await user_repo.count_active()
        admin_count = await user_repo.count_admins()

        return {
            "total_users": total_users,
            "active_users": active_users,
            "admin_count": admin_count,
        }
    except Exception as e:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="USER_002", trace_id=trace_id, path="/api/v1/users/stats"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{error.model_dump(mode='json')} - {str(e)}",
        )


@router.post(
    "",
    response_model=UserResponse,
    summary="创建用户",
    description="创建新用户（管理员专用）",
    responses={
        200: {"description": "创建成功"},
        400: {"description": "请求数据无效"},
        401: {"description": "未认证"},
        403: {"description": "权限不足"},
        409: {"description": "用户名已存在"},
    },
)
async def create_user(
    username: str,
    password: str,
    role: str = "user",
    current_user: UserInDB = Depends(require_admin),
    user_repo: UserRepository = Depends(get_user_repository),
) -> UserResponse:
    """
    创建用户

    Args:
        username: 用户名
        password: 密码
        role: 角色
        current_user: 当前用户（管理员）
        user_repo: 用户仓库

    Returns:
        创建的用户信息

    Raises:
        HTTPException: 用户名已存在
    """
    try:
        # 检查用户名是否已存在
        existing_user = await user_repo.get_by_username(username)
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="用户名已存在"
            )

        # 创建用户
        password_hash = hash_password(password)
        user_create = UserCreate(
            username=username,
            password=password,
            email="",
            role=role,
        )
        user = await user_repo.create(user_create, password_hash)

        return UserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role,
            created_at=user.created_at,
            last_login_at=None,
        )
    except HTTPException:
        raise
    except Exception as e:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="USER_003", trace_id=trace_id, path="/api/v1/users"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{error.model_dump(mode='json')} - {str(e)}",
        )


@router.put(
    "/{user_id}/role",
    response_model=UserResponse,
    summary="更新用户角色",
    description="更新用户角色（管理员专用）",
    responses={
        200: {"description": "更新成功"},
        401: {"description": "未认证"},
        403: {"description": "权限不足"},
        404: {"description": "用户不存在"},
    },
)
async def update_user_role(
    user_id: str,
    role: str,
    current_user: UserInDB = Depends(require_admin),
    user_repo: UserRepository = Depends(get_user_repository),
) -> UserResponse:
    """
    更新用户角色

    Args:
        user_id: 用户 ID
        role: 新角色
        current_user: 当前用户（管理员）
        user_repo: 用户仓库

    Returns:
        更新后的用户信息
    """
    try:
        # 验证角色
        if role not in ["admin", "user"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的角色，必须是 admin 或 user",
            )

        # 更新角色
        user = await user_repo.update_role(UUID(user_id), role)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在"
            )

        return UserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role,
            created_at=user.created_at,
            last_login_at=None,
        )
    except HTTPException:
        raise
    except Exception as e:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="USER_004",
            trace_id=trace_id,
            path=f"/api/v1/users/{user_id}/role",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{error.model_dump(mode='json')} - {str(e)}",
        )


@router.put(
    "/{user_id}/active",
    response_model=UserResponse,
    summary="更新用户激活状态",
    description="启用或禁用用户（管理员专用）",
    responses={
        200: {"description": "更新成功"},
        401: {"description": "未认证"},
        403: {"description": "权限不足"},
        404: {"description": "用户不存在"},
    },
)
async def update_user_active_status(
    user_id: str,
    is_active: bool,
    current_user: UserInDB = Depends(require_admin),
    user_repo: UserRepository = Depends(get_user_repository),
) -> UserResponse:
    """
    更新用户激活状态

    Args:
        user_id: 用户 ID
        is_active: 是否激活
        current_user: 当前用户（管理员）
        user_repo: 用户仓库

    Returns:
        更新后的用户信息
    """
    try:
        # 更新状态
        user = await user_repo.update_active_status(UUID(user_id), is_active)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在"
            )

        return UserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role,
            created_at=user.created_at,
            last_login_at=None,
        )
    except HTTPException:
        raise
    except Exception as e:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="USER_005",
            trace_id=trace_id,
            path=f"/api/v1/users/{user_id}/active",
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{error.model_dump(mode='json')} - {str(e)}",
        )


@router.delete(
    "/{user_id}",
    response_model=MessageResponse,
    summary="删除用户",
    description="删除用户（管理员专用）",
    responses={
        200: {"description": "删除成功"},
        401: {"description": "未认证"},
        403: {"description": "权限不足"},
        404: {"description": "用户不存在"},
    },
)
async def delete_user(
    user_id: str,
    current_user: UserInDB = Depends(require_admin),
    user_repo: UserRepository = Depends(get_user_repository),
) -> MessageResponse:
    """
    删除用户

    Args:
        user_id: 用户 ID
        current_user: 当前用户（管理员）
        user_repo: 用户仓库

    Returns:
        删除结果
    """
    try:
        # 不允许删除自己
        if str(current_user.id) == user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="不能删除自己"
            )

        # 删除用户
        success = await user_repo.delete(UUID(user_id))
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在"
            )

        return MessageResponse(message="用户删除成功")
    except HTTPException:
        raise
    except Exception as e:
        trace_id = generate_trace_id()
        error = create_error_response(
            error_code="USER_006", trace_id=trace_id, path=f"/api/v1/users/{user_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{error.model_dump(mode='json')} - {str(e)}",
        )
