"""
WebSocket 认证模块

提供 WebSocket 连接的 JWT 认证功能
"""

import logging

from fastapi import Query, WebSocket, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import UserInDB
from src.auth.token import TokenManager
from src.config.settings import get_settings
from src.core.exceptions import TokenExpiredError, TokenInvalidError, TokenRevokedError
from src.db.repositories import UserRepository

logger = logging.getLogger(__name__)

# 获取配置
settings = get_settings()

# 初始化 TokenManager
token_manager = TokenManager(
    secret_key=settings.jwt_secret_key,
    algorithm=settings.jwt_algorithm,
    access_token_expire_minutes=settings.access_token_expire_minutes,
    refresh_token_expire_days=settings.refresh_token_expire_days,
)


async def get_websocket_user(
    websocket: WebSocket,
    token: str | None = Query(None, description="JWT 访问令牌"),
    db: AsyncSession = None,
) -> UserInDB | None:
    """
    从 WebSocket 连接中获取认证用户

    支持两种方式传递 Token:
    1. 查询参数: ?token=xxx
    2. 查询参数: ?access_token=xxx

    为了开发便利，如果未提供 Token 且处于开发模式，则返回匿名用户

    Args:
        websocket: WebSocket 连接
        token: JWT 访问令牌（从查询参数）
        db: 数据库会话

    Returns:
        认证用户对象，未认证时返回 None

    Raises:
        WebSocketDisconnect: Token 无效时断开连接
    """
    # 尝试从查询参数获取 token
    if not token:
        # 尝试从 access_token 参数获取
        token = websocket.query_params.get("access_token")

    # 如果没有 token，检查是否允许匿名访问（开发模式）
    if not token:
        if settings.debug:
            logger.warning("WebSocket 连接未提供 Token，使用匿名用户（开发模式）")
            return None
        else:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION, reason="缺少认证令牌"
            )
            return None

    try:
        # 验证 Token
        payload = token_manager.verify_token(token, token_type="access")

        # 获取用户
        if db:
            user_repo = UserRepository(db)
            from uuid import UUID

            user = await user_repo.get_by_id(UUID(payload.sub))

            if user is None:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION, reason="用户不存在"
                )
                return None

            if not user.is_active:
                await websocket.close(
                    code=status.WS_1008_POLICY_VIOLATION, reason="用户已被禁用"
                )
                return None

            logger.info(
                f"WebSocket 认证成功 | user_id={user.id} | username={user.username}"
            )
            return user
        else:
            # 无数据库会话时，仅返回 payload 信息
            logger.warning("WebSocket 认证缺少数据库会话，仅验证 Token")
            return None

    except TokenExpiredError:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Token 已过期"
        )
        return None

    except TokenRevokedError:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Token 已被撤销"
        )
        return None

    except TokenInvalidError as e:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason=f"Token 无效: {str(e)}"
        )
        return None

    except Exception as e:
        logger.error(f"WebSocket 认证失败: {e}", exc_info=True)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="认证失败")
        return None


async def get_user_id_from_websocket(
    websocket: WebSocket,
    token: str | None = Query(None, description="JWT 访问令牌"),
    db: AsyncSession = None,
) -> str:
    """
    从 WebSocket 连接中获取用户 ID

    这是 get_websocket_user 的简化版本，仅返回用户 ID 字符串

    Args:
        websocket: WebSocket 连接
        token: JWT 访问令牌（从查询参数）
        db: 数据库会话

    Returns:
        用户 ID 字符串，未认证时返回 "anonymous"
    """
    user = await get_websocket_user(websocket, token, db)

    if user:
        return str(user.id)
    else:
        return "anonymous"
