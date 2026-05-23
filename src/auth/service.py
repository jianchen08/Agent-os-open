"""
认证服务

提供用户注册、登录、登出等核心认证功能
"""

from typing import Any, Protocol
from uuid import UUID

from src.core.exceptions.auth import (
    InvalidCredentialsError,
    UserExistsError,
    UserInactiveError,
    UserNotFoundError,
)
from src.auth.models import UserCreate, UserInDB
from src.auth.password import hash_password, verify_password
from src.auth.token import TokenManager


class UserRepository(Protocol):
    """用户仓库协议"""

    async def get_by_id(self, user_id: UUID) -> UserInDB | None:
        """通过 ID 获取用户"""
        ...

    async def get_by_username(self, username: str) -> UserInDB | None:
        """通过用户名获取用户"""
        ...

    async def create(self, user: UserCreate, password_hash: str) -> UserInDB:
        """创建用户"""
        ...

    async def update_last_login(self, user_id: UUID) -> None:
        """更新最后登录时间"""
        ...


class AuthService:
    """认证服务"""

    def __init__(
        self,
        token_manager: TokenManager,
        user_repository: UserRepository,
    ):
        """
        初始化认证服务

        Args:
            token_manager: Token 管理器
            user_repository: 用户仓库
        """
        self.token_manager = token_manager
        self.user_repository = user_repository

    async def register(self, user_create: UserCreate) -> UserInDB:
        """
        注册新用户

        Args:
            user_create: 用户创建请求

        Returns:
            创建的用户对象

        Raises:
            UserExistsError: 用户名已存在
        """
        # 检查用户名是否已存在
        existing_user = await self.user_repository.get_by_username(user_create.username)
        if existing_user is not None:
            raise UserExistsError(f"用户名 '{user_create.username}' 已存在")

        # 哈希密码
        password_hash = self.hash_password(user_create.password)

        # 创建用户
        user = await self.user_repository.create(user_create, password_hash)

        return user

    async def authenticate(
        self,
        username: str,
        password: str,
    ) -> dict[str, Any]:
        """
        用户认证（登录）

        Args:
            username: 用户名
            password: 密码

        Returns:
            包含 Token 信息的字典

        Raises:
            InvalidCredentialsError: 凭证无效
            UserInactiveError: 用户已禁用
        """
        # 获取用户
        user = await self.user_repository.get_by_username(username)
        if user is None:
            raise InvalidCredentialsError()

        # 验证密码
        if not self._verify_password(password, user.password_hash):
            raise InvalidCredentialsError()

        # 检查用户状态
        if not user.is_active:
            raise UserInactiveError()

        # 更新最后登录时间
        try:
            await self.user_repository.update_last_login(user.id)
        except Exception:
            pass  # 忽略更新失败

        # 创建 Token 对
        token_pair = self.token_manager.create_token_pair(
            user_id=str(user.id),
            role=user.role,
        )

        return {
            "access_token": token_pair.access_token,
            "refresh_token": token_pair.refresh_token,
            "token_type": token_pair.token_type,
            "expires_in": token_pair.expires_in,
        }

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        """
        刷新 Token

        Args:
            refresh_token: 刷新令牌

        Returns:
            新的 Token 信息

        Raises:
            UserNotFoundError: 用户不存在
            UserInactiveError: 用户已禁用
        """
        # 验证刷新令牌
        payload = self.token_manager.verify_token(refresh_token, token_type="refresh")

        # 获取用户
        user_id = UUID(payload.sub)
        user = await self.user_repository.get_by_id(user_id)

        if user is None:
            raise UserNotFoundError()

        if not user.is_active:
            raise UserInactiveError()

        # 创建新的 Token 对
        token_pair = self.token_manager.refresh_token_pair(
            refresh_token=refresh_token,
            role=user.role,
        )

        return {
            "access_token": token_pair.access_token,
            "refresh_token": token_pair.refresh_token,
            "token_type": token_pair.token_type,
            "expires_in": token_pair.expires_in,
        }

    async def logout(
        self,
        user_id: UUID,
        refresh_token: str | None = None,
        logout_all_devices: bool = False,
    ) -> None:
        """
        用户登出

        Args:
            user_id: 用户 ID
            refresh_token: 刷新令牌（可选）
            logout_all_devices: 是否登出所有设备
        """
        if logout_all_devices:
            # 撤销用户所有 Token
            self.token_manager.revoke_all_user_tokens(str(user_id))
        elif refresh_token:
            # 只撤销指定的刷新令牌
            self.token_manager.revoke_token(refresh_token)

    async def get_user_by_id(self, user_id: UUID) -> UserInDB | None:
        """
        通过 ID 获取用户

        Args:
            user_id: 用户 ID

        Returns:
            用户对象或 None
        """
        return await self.user_repository.get_by_id(user_id)

    def hash_password(self, password: str) -> str:
        """
        哈希密码

        Args:
            password: 明文密码

        Returns:
            密码哈希
        """
        return hash_password(password)

    def _verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """
        验证密码

        Args:
            plain_password: 明文密码
            hashed_password: 密码哈希

        Returns:
            密码是否匹配
        """
        return verify_password(plain_password, hashed_password)
