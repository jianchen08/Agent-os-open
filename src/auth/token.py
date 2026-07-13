"""
JWT Token 管理

提供 Token 的创建、验证、刷新和撤销功能。
撤销机制优先使用 Redis 存储，Redis 不可用时降级到内存。
"""

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import jwt

from src.auth.models import TokenPair, TokenPayload
from src.core.exceptions import (
    TokenExpiredError,
    TokenInvalidError,
    TokenRevokedError,
)

logger = logging.getLogger(__name__)

# Redis 键前缀
_REVOKED_TOKEN_PREFIX = "auth:revoked_token:"
_REVOKED_USER_PREFIX = "auth:revoked_user:"


class TokenManager:
    """JWT Token 管理器

    撤销机制：
    - 优先使用 Redis 存储已撤销的 token 和用户
    - Redis 不可用时降级到内存存储，并记录警告日志
    - 接口保持不变，调用方无感知
    """

    def __init__(
        self,
        secret_key: str,
        algorithm: str = "HS256",
        access_token_expire_minutes: int = 30,
        refresh_token_expire_days: int = 7,
        redis_url: str | None = None,
    ):
        """
        初始化 Token 管理器

        Args:
            secret_key: JWT 签名密钥
            algorithm: 签名算法
            access_token_expire_minutes: 访问令牌有效期（分钟）
            refresh_token_expire_days: 刷新令牌有效期（天）
            redis_url: Redis 连接 URL，为 None 时尝试从环境变量读取
        """
        self.secret_key = secret_key
        self.algorithm = algorithm
        self.access_token_expire_minutes = access_token_expire_minutes
        self.refresh_token_expire_days = refresh_token_expire_days

        # 最大 TTL：刷新令牌有效期（秒），用于 Redis 键过期
        self._max_ttl_seconds = refresh_token_expire_days * 24 * 3600

        # 内存 fallback
        self._revoked_tokens: set[str] = set()
        self._revoked_users: dict[str, datetime] = {}

        # Redis 客户端（同步）
        self._redis: Any = None
        self._redis_available: bool = False
        self._init_redis(redis_url)

    def _init_redis(self, redis_url: str | None = None) -> None:
        """尝试连接 Redis，失败则降级到内存存储。

        Args:
            redis_url: Redis 连接 URL
        """
        url = redis_url
        if url is None:
            import os  # noqa: PLC0415

            url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

        try:
            import redis as redis_lib  # noqa: PLC0415

            self._redis = redis_lib.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            self._redis.ping()
            self._redis_available = True
            logger.info("TokenManager: Redis 连接成功，撤销存储使用 Redis")
        except Exception as exc:
            self._redis = None
            self._redis_available = False
            logger.warning(
                "TokenManager: Redis 不可用 (%s)，撤销存储降级到内存。多实例部署时 token 撤销无法跨进程生效。",
                exc,
            )

    @staticmethod
    def _token_hash(token: str) -> str:
        """计算 token 的 SHA256 哈希，用于 Redis 键。

        Args:
            token: 原始 token 字符串

        Returns:
            SHA256 哈希的十六进制字符串
        """
        return hashlib.sha256(token.encode()).hexdigest()

    def create_access_token(
        self,
        user_id: str,
        role: str = "user",
        expires_delta: timedelta | None = None,
    ) -> str:
        """
        创建访问令牌

        Args:
            user_id: 用户 ID
            role: 用户角色
            expires_delta: 自定义过期时间

        Returns:
            JWT 访问令牌
        """
        if expires_delta is None:
            expires_delta = timedelta(minutes=self.access_token_expire_minutes)

        # 绑定日志上下文，使后续认证日志自动携带 request_id
        from src.core.logging import LogContext  # noqa: PLC0415

        LogContext.bind(request_id=user_id)

        return self._create_token(
            user_id=user_id,
            role=role,
            token_type="access",
            expires_delta=expires_delta,
        )

    def create_refresh_token(
        self,
        user_id: str,
        expires_delta: timedelta | None = None,
    ) -> str:
        """
        创建刷新令牌

        Args:
            user_id: 用户 ID
            expires_delta: 自定义过期时间

        Returns:
            JWT 刷新令牌
        """
        if expires_delta is None:
            expires_delta = timedelta(days=self.refresh_token_expire_days)

        return self._create_token(
            user_id=user_id,
            role="",
            token_type="refresh",
            expires_delta=expires_delta,
        )

    def create_token_pair(
        self,
        user_id: str,
        role: str = "user",
    ) -> TokenPair:
        """
        创建 Token 对（访问令牌 + 刷新令牌）

        Args:
            user_id: 用户 ID
            role: 用户角色

        Returns:
            TokenPair 对象
        """
        access_token = self.create_access_token(user_id=user_id, role=role)
        refresh_token = self.create_refresh_token(user_id=user_id)

        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=self.access_token_expire_minutes * 60,
        )

    def _is_token_revoked(self, token: str) -> bool:
        """检查 token 是否已撤销（优先查 Redis，fallback 内存）。"""
        token_h = self._token_hash(token)

        if self._redis_available:
            try:
                return bool(self._redis.exists(f"{_REVOKED_TOKEN_PREFIX}{token_h}"))
            except Exception as exc:
                logger.debug("TokenManager: Redis 查询失败，降级到内存: %s", exc)

        return token in self._revoked_tokens

    def _is_user_revoked(self, user_id: str, iat: datetime) -> bool:
        """检查用户是否在 token 签发后被全局撤销。"""
        if self._redis_available:
            try:
                revoke_ts = self._redis.get(f"{_REVOKED_USER_PREFIX}{user_id}")
                if revoke_ts is not None:
                    revoke_time = datetime.fromtimestamp(float(revoke_ts), tz=UTC)
                    return iat <= revoke_time
                return False
            except Exception as exc:
                logger.debug("TokenManager: Redis 查询失败，降级到内存: %s", exc)

        # 严格小于：iat < revoke_time 才判定为撤销。
        if user_id in self._revoked_users:
            return iat <= self._revoked_users[user_id]
        return False

    def verify_token(
        self,
        token: str,
        token_type: str = "access",
    ) -> TokenPayload:
        """
        验证令牌

        Args:
            token: JWT 令牌
            token_type: 期望的令牌类型

        Returns:
            TokenPayload 对象

        Raises:
            TokenExpiredError: 令牌已过期
            TokenInvalidError: 令牌无效
            TokenRevokedError: 令牌已被撤销
        """
        # 检查是否已撤销
        if self._is_token_revoked(token):
            raise TokenRevokedError()

        try:
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=[self.algorithm],
            )
        except jwt.ExpiredSignatureError:
            raise TokenExpiredError()  # noqa: B904
        except jwt.InvalidTokenError:
            raise TokenInvalidError()  # noqa: B904

        # 验证 token 类型
        if payload.get("type") != token_type:
            raise TokenInvalidError(f"期望 {token_type} 类型的令牌")

        # 检查用户是否被撤销
        user_id = payload.get("sub")
        iat = datetime.fromtimestamp(payload.get("iat", 0), tz=UTC)
        if user_id and self._is_user_revoked(user_id, iat):
            raise TokenRevokedError()

        return TokenPayload(
            sub=payload["sub"],
            exp=datetime.fromtimestamp(payload["exp"], tz=UTC),
            iat=datetime.fromtimestamp(payload["iat"], tz=UTC),
            type=payload["type"],
            role=payload.get("role", "user"),
            jti=payload.get("jti"),
        )

    def refresh_token_pair(
        self,
        refresh_token: str,
        role: str = "user",
    ) -> TokenPair:
        """
        使用刷新令牌获取新的 Token 对

        Args:
            refresh_token: 刷新令牌
            role: 用户角色

        Returns:
            新的 TokenPair 对象

        Raises:
            TokenInvalidError: 刷新令牌无效
        """
        # 验证刷新令牌
        payload = self.verify_token(refresh_token, token_type="refresh")

        # 撤销旧的刷新令牌
        self.revoke_token(refresh_token)

        # 创建新的 Token 对
        return self.create_token_pair(user_id=payload.sub, role=role)

    def revoke_token(self, token: str) -> None:
        """
        撤销令牌

        Args:
            token: 要撤销的令牌
        """
        token_h = self._token_hash(token)

        if self._redis_available:
            try:
                self._redis.setex(
                    f"{_REVOKED_TOKEN_PREFIX}{token_h}",
                    self._max_ttl_seconds,
                    "1",
                )
                return
            except Exception as exc:
                logger.warning("TokenManager: Redis 写入失败，降级到内存: %s", exc)

        self._revoked_tokens.add(token)

    def revoke_all_user_tokens(self, user_id: str) -> None:
        """
        撤销用户的所有令牌

        Args:
            user_id: 用户 ID
        """
        now = datetime.now(UTC)

        if self._redis_available:
            try:
                self._redis.setex(
                    f"{_REVOKED_USER_PREFIX}{user_id}",
                    self._max_ttl_seconds,
                    str(now.timestamp()),
                )
                return
            except Exception as exc:
                logger.warning("TokenManager: Redis 写入失败，降级到内存: %s", exc)

        self._revoked_users[user_id] = now

    def decode_token(
        self,
        token: str,
        verify: bool = True,
    ) -> dict[str, Any]:
        """
        解码令牌

        Args:
            token: JWT 令牌
            verify: 是否验证签名

        Returns:
            令牌载荷字典
        """
        options = {"verify_signature": verify}
        if not verify:
            options["verify_exp"] = False

        return jwt.decode(
            token,
            self.secret_key,
            algorithms=[self.algorithm],
            options=options,
        )

    def _create_token(
        self,
        user_id: str,
        role: str,
        token_type: str,
        expires_delta: timedelta,
    ) -> str:
        """
        创建 JWT 令牌

        Args:
            user_id: 用户 ID
            role: 用户角色
            token_type: 令牌类型
            expires_delta: 过期时间增量

        Returns:
            JWT 令牌字符串
        """
        now = datetime.now(UTC)
        expire = now + expires_delta

        payload = {
            "sub": user_id,
            "exp": expire,
            "iat": now,
            "type": token_type,
            "jti": str(uuid4()),
        }

        if role:
            payload["role"] = role

        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)
