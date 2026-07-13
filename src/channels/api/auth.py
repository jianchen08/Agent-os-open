"""JWT 认证工具模块。

提供 JWT Token 的创建、验证和用户信息提取功能。
用于 API 接口的身份认证和 WebSocket 连接的 Token 校验。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from src.auth.token import TokenManager
from src.config.settings import get_settings

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
SECRET_KEY = get_settings().jwt_secret_key


# 模块级 TokenManager 单例，复用 Redis 连接池；同时作为统一撤销入口。
def _get_token_manager() -> TokenManager:
    """返回模块级 TokenManager 单例（惰性初始化）。"""
    global _token_manager
    if _token_manager is None:
        _token_manager = TokenManager(secret_key=SECRET_KEY)
    return _token_manager


_token_manager: TokenManager | None = None


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """创建 access token。

    Args:
        data: 要编码到 token 中的负载数据，通常包含用户 ID 和用户名
        expires_delta: 过期时间间隔，默认 30 分钟

    Returns:
        编码后的 JWT 字符串
    """
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=30))
    # 必须写入 iat：TokenManager.verify_token 会读取 iat 做用户撤销校验，
    # 缺失会导致 KeyError，令牌验证直接失败（401）。
    to_encode.update({"exp": expire, "iat": now, "type": "access"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """创建 refresh token。

    Args:
        data: 要编码到 token 中的负载数据，通常包含用户 ID 和用户名
        expires_delta: 过期时间间隔，默认 7 天

    Returns:
        编码后的 JWT 字符串
    """
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(days=7))
    # 同 create_access_token，写入 iat 以匹配 TokenManager.verify_token 的契约。
    to_encode.update({"exp": expire, "iat": now, "type": "refresh"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str, token_type: str = "access") -> dict[str, Any] | None:
    """验证 token 并返回负载数据。

    DEBT: 旧版 JWT 验证，委托到 TokenManager。ceiling: 两套并存。
    upgrade: 全部路由认证迁移到 TokenManager 后删除此模块函数。

    委托 TokenManager 进行验证（含撤销检查），但返回完整的原始 payload dict
    以保持对 username 等自定义字段的向后兼容。

    Args:
        token: 待验证的 JWT 字符串
        token_type: 期望的令牌类型（access/refresh）

    Returns:
        验证成功返回 payload 字典，失败返回 None
    """
    try:
        import jwt as _jwt

        manager = _get_token_manager()
        # 先用 TokenManager 验证（含撤销检查），再 decode 获取完整 payload
        manager.verify_token(token, token_type=token_type)
        # 验证通过后，decode 获取完整 payload（含 username 等自定义字段）
        payload = _jwt.decode(token, manager.secret_key, algorithms=[manager.algorithm])
        return payload
    except Exception as exc:
        logger.warning("Token 验证失败: %s", exc)
        return None


def get_current_user(token: str) -> dict[str, Any] | None:
    """从 token 中获取当前用户信息。

    验证 token 类型必须为 access，并返回用户相关字段。

    Args:
        token: 待解析的 JWT 字符串

    Returns:
        包含用户信息的字典（sub, username），验证失败返回 None
    """
    payload = verify_token(token)
    if payload is None:
        return None

    # 检查 token 类型
    if payload.get("type") != "access":
        logger.warning("非 access token，无法获取用户信息")
        return None

    user_id = payload.get("sub")
    username = payload.get("username")
    if user_id is None or username is None:
        return None

    return {"sub": user_id, "username": username, "role": payload.get("role", "user")}
