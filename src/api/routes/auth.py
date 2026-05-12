"""
认证路由

提供用户认证相关的 API 端点
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import generate_trace_id, get_current_user
from src.api.errors import create_error_response
from src.api.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshTokenRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from src.api.schemas.common import MessageResponse
from src.auth.models import UserCreate, UserInDB
from src.auth.service import AuthService
from src.auth.token import TokenManager
from src.config.settings import get_settings
from src.db.connection import get_async_session
from src.db.repositories import UserRepository

router = APIRouter()

# 获取配置
settings = get_settings()

# 初始化 TokenManager
token_manager = TokenManager(
    secret_key=settings.jwt_secret_key,
    algorithm=settings.jwt_algorithm,
    access_token_expire_minutes=settings.access_token_expire_minutes,
    refresh_token_expire_days=settings.refresh_token_expire_days,
)


async def get_auth_service(
    session: AsyncSession = Depends(get_async_session),
) -> AuthService:
    """
    获取认证服务实例（依赖注入）

    Args:
        session: 数据库会话

    Returns:
        AuthService 实例
    """
    user_repo = UserRepository(session)
    return AuthService(token_manager=token_manager, user_repository=user_repo)


@router.post(
    "/register",
    response_model=TokenResponse,
    summary="用户注册",
    description="注册新用户并返回 JWT Token",
    responses={
        200: {"description": "注册成功"},
        400: {"description": "请求数据无效"},
        409: {"description": "用户名或邮箱已存在"},
    },
)
async def register(
    request: RegisterRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """
    用户注册

    Args:
        request: 注册请求，包含用户名、邮箱和密码
        auth_service: 认证服务

    Returns:
        TokenResponse: 包含 access_token 和 refresh_token

    Raises:
        HTTPException: 用户名或邮箱已存在
    """
    try:
        # 创建用户对象
        user_create = UserCreate(
            username=request.username,
            email=request.email,
            password=request.password,
            role="user",  # 默认角色
            is_active=True,  # 默认激活
        )

        # 调用认证服务进行注册
        await auth_service.register(user_create)

        # 注册成功后自动登录，返回 Token
        token_result = await auth_service.authenticate(
            username=request.username, password=request.password
        )

        return TokenResponse(**token_result)

    except ValueError as e:
        # 用户名或邮箱已存在
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        trace_id = generate_trace_id()
        error = create_error_response(
            code="AUTH_002", trace_id=trace_id
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{error.model_dump(mode='json')} - {str(e)}",
        )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="用户登录",
    description="使用用户名和密码进行登录，返回 JWT Token",
    responses={
        200: {"description": "登录成功"},
        401: {"description": "凭证无效"},
        429: {"description": "登录尝试过多"},
    },
)
async def login(
    request: LoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """
    用户登录

    Args:
        request: 登录请求，包含用户名和密码
        auth_service: 认证服务

    Returns:
        TokenResponse: 包含 access_token 和 refresh_token

    Raises:
        HTTPException: 凭证无效或登录尝试过多
    """
    try:
        result = await auth_service.authenticate(
            username=request.username, password=request.password
        )

        return TokenResponse(**result)

    except Exception as e:
        trace_id = generate_trace_id()
        error = create_error_response(
            code="AUTH_001", trace_id=trace_id
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{error.model_dump(mode='json')} - {str(e)}",
        )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="刷新 Token",
    description="使用 Refresh Token 获取新的 Token 对",
    responses={
        200: {"description": "刷新成功"},
        401: {"description": "Token 无效或已过期"},
    },
)
async def refresh_token(
    request: RefreshTokenRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """
    刷新 Token

    Args:
        request: 刷新请求，包含 refresh_token
        auth_service: 认证服务

    Returns:
        TokenResponse: 新的 Token 对

    Raises:
        HTTPException: Token 无效或已过期
    """
    try:
        result = await auth_service.refresh_token(request.refresh_token)
        return TokenResponse(**result)

    except Exception as e:
        trace_id = generate_trace_id()
        error = create_error_response(
            code="AUTH_004", trace_id=trace_id
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{error.model_dump(mode='json')} - {str(e)}",
        )


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="用户登出",
    description="使当前 Token 失效",
    responses={
        200: {"description": "登出成功"},
        401: {"description": "未认证"},
    },
)
async def logout(
    request: LogoutRequest | None = None,
    current_user: UserInDB = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> MessageResponse:
    """
    用户登出

    Args:
        request: 登出请求（可选）
        current_user: 当前用户
        auth_service: 认证服务

    Returns:
        MessageResponse: 登出成功消息
    """
    refresh_token = request.refresh_token if request else None
    logout_all = request.logout_all_devices if request else False

    await auth_service.logout(
        user_id=current_user.id,
        refresh_token=refresh_token,
        logout_all_devices=logout_all,
    )

    return MessageResponse(message="登出成功")


@router.get(
    "/me",
    response_model=UserResponse,
    summary="获取当前用户信息",
    description="获取当前认证用户的详细信息",
    responses={
        200: {"description": "获取成功"},
        401: {"description": "未认证"},
    },
)
async def get_me(current_user: UserInDB = Depends(get_current_user)) -> UserResponse:
    """
    获取当前用户信息

    Args:
        current_user: 当前用户

    Returns:
        UserResponse: 用户信息
    """
    return UserResponse(
        id=str(current_user.id),
        username=current_user.username,
        email=current_user.email,
        role=current_user.role,
        created_at=current_user.created_at,
        last_login_at=None,  # User 模型暂无此字段
    )
