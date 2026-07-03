"""认证相关 API 路由。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

from channels.api.auth import (
    _get_token_manager,
    create_access_token,
    create_refresh_token,
    get_current_user,
    verify_token,
)
from channels.api.deps import _extract_token
from channels.api.memory_store import store
from channels.api.models import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from src.auth.password import verify_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])


def _get_user_id_from_bearer(credentials: HTTPAuthorizationCredentials) -> str:
    """从 Bearer credentials 中提取用户 ID。"""
    user_info = get_current_user(credentials.credentials)
    if user_info is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或过期的认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_info["sub"]


@router.post("/login", response_model=TokenResponse, summary="用户登录")
def login(request: LoginRequest) -> TokenResponse:
    """验证用户名密码，返回 access token 和 refresh token。"""
    user = store.get_user_by_username(request.username)
    # 使用 bcrypt 验证密码，禁止明文比对
    stored_password = user.get("password", "") if user else ""
    if not user or not stored_password or not verify_password(request.password, stored_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    token_data = {"sub": user["id"], "username": user["username"]}
    access_token = create_access_token(token_data)
    refresh_token = create_refresh_token(token_data)

    logger.info("用户登录成功: %s", user["username"])

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=1800,  # 30 分钟（秒）
    )


@router.post("/register", response_model=TokenResponse, summary="用户注册")
def register(request: RegisterRequest) -> TokenResponse:
    """创建新用户并返回 token。"""
    try:
        user = store.create_user(
            username=request.username,
            password=request.password,
            email=request.email,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    token_data = {"sub": user["id"], "username": user["username"]}
    access_token = create_access_token(token_data)
    refresh_token = create_refresh_token(token_data)

    logger.info("用户注册成功: %s", user["username"])

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=1800,
    )


@router.get("/me", response_model=UserResponse, summary="获取当前用户信息")
def get_me(
    authorization: str = Header(default=""),
) -> UserResponse:
    """通过 Bearer token 获取当前用户信息。"""
    # 从 Authorization 头提取 Bearer token
    actual_token = _extract_token(authorization)

    if not actual_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_info = get_current_user(actual_token)
    if user_info is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或过期的认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = store.get_user_by_id(user_info["sub"])
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        )

    return UserResponse(
        id=user["id"],
        username=user["username"],
        email=user.get("email"),
        created_at=user["created_at"],
    )


@router.post("/refresh", response_model=TokenResponse, summary="刷新令牌")
def refresh_token(
    authorization: str = Header(default=""),
    body: RefreshRequest | None = None,
) -> TokenResponse:
    """使用 refresh token 获取新的 access token。"""
    # body 优先于 header
    actual_token = ""
    if body and body.refresh_token:
        actual_token = body.refresh_token
    if not actual_token:
        actual_token = _extract_token(authorization)

    if not actual_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 refresh token",
        )

    # 检查是否已被撤销（统一走 TokenManager，P2.2）
    token_manager = _get_token_manager()
    if token_manager._is_token_revoked(actual_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token 已被撤销",
        )

    payload = verify_token(actual_token, token_type="refresh")
    if payload is None or payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的 refresh token",
        )

    user_id = payload.get("sub")
    username = payload.get("username")
    if user_id is None or username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token 中缺少用户信息",
        )

    # 检查用户是否仍然存在
    user = store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
        )

    # 撤销旧的 refresh token（统一走 TokenManager → Redis，P2.2）
    token_manager.revoke_token(actual_token)

    # 生成新 token
    token_data = {"sub": user_id, "username": username}
    new_access = create_access_token(token_data)
    new_refresh = create_refresh_token(token_data)

    return TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_in=1800,
    )


@router.post("/logout", summary="用户登出")
def logout(
    authorization: str = Header(default=""),
    body: RefreshRequest | None = None,
) -> dict[str, str]:
    """登出用户，撤销 refresh token 并使该用户所有已签发 token 立即失效。"""
    actual_token = _extract_token(authorization)
    if not actual_token and body and body.refresh_token:
        actual_token = body.refresh_token

    token_manager = _get_token_manager()

    if actual_token:
        payload = verify_token(actual_token, token_type="refresh")
        if payload and payload.get("type") == "refresh":
            # 撤销 refresh token（P2.2 统一到 TokenManager）
            token_manager.revoke_token(actual_token)
            # 撤销该用户所有已签发 token（含 access token，P2.3）
            # revoke_all_user_tokens 按 iat 时间戳判定，会令所有更早签发的 token 失效
            user_id = payload.get("sub")
            if user_id:
                token_manager.revoke_all_user_tokens(user_id)

    return {"message": "登出成功"}
