"""JWT 认证工具模块。

提供 JWT Token 的创建、验证和用户信息提取功能。
用于 API 接口的身份认证和 WebSocket 连接的 Token 校验。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

logger = logging.getLogger(__name__)

# 密钥配置（生产环境应从环境变量读取）
SECRET_KEY = "dev-secret-key-change-in-production"
ALGORITHM = "HS256"


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """创建 access token。

    Args:
        data: 要编码到 token 中的负载数据，通常包含用户 ID 和用户名
        expires_delta: 过期时间间隔，默认 30 分钟

    Returns:
        编码后的 JWT 字符串
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=30))
    to_encode.update({"exp": expire, "type": "access"})
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
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(days=7))
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> dict[str, Any] | None:
    """验证 token 并返回负载数据。

    Args:
        token: 待验证的 JWT 字符串

    Returns:
        验证成功返回 payload 字典，失败返回 None
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Token 已过期")
        return None
    except jwt.InvalidTokenError as exc:
        logger.warning("无效 Token: %s", exc)
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

    return {"sub": user_id, "username": username}
