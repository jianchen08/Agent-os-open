"""
认证服务单元测试。

覆盖 AC：
- AC-AUTH-01: 用户注册成功，密码 bcrypt 存储

对应需求：F-AUTH-01, F-AUTH-03, F-AUTH-05
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.auth.models import UserCreate, UserInDB
from src.auth.password import hash_password, verify_password
from src.auth.service import AuthService
from src.auth.token import TokenManager
from src.core.exceptions.auth import (
    InvalidCredentialsError,
    UserExistsError,
    UserInactiveError,
    UserNotFoundError,
)


# ============================================================
# 辅助
# ============================================================

class MockUserRepository:
    """内存用户仓库模拟。"""

    def __init__(self) -> None:
        self._users: dict[str, UserInDB] = {}

    async def get_by_id(self, user_id: uuid.UUID) -> UserInDB | None:
        return next(
            (u for u in self._users.values() if u.id == user_id),
            None,
        )

    async def get_by_username(self, username: str) -> UserInDB | None:
        return self._users.get(username)

    async def create(self, user_create: UserCreate, password_hash: str) -> UserInDB:
        user = UserInDB(
            id=uuid.uuid4(),
            username=user_create.username,
            email=user_create.email,
            password_hash=password_hash,
            role=user_create.role,
            is_active=True,
            created_at=datetime.now(),
        )
        self._users[user_create.username] = user
        return user

    async def update_last_login(self, user_id: uuid.UUID) -> None:
        pass

    def add_inactive_user(self, username: str, password: str) -> UserInDB:
        """添加一个非激活用户。"""
        user = UserInDB(
            id=uuid.uuid4(),
            username=username,
            password_hash=hash_password(password),
            role="user",
            is_active=False,
            created_at=datetime.now(),
        )
        self._users[username] = user
        return user


def _make_service() -> tuple[AuthService, MockUserRepository]:
    """创建使用内存降级的 AuthService + Mock 仓库。"""
    token_mgr = TokenManager(
        secret_key="test-secret-key-at-least-32-bytes-long-for-hs256",
        redis_url="redis://invalid:99999/0",
    )
    repo = MockUserRepository()
    return AuthService(token_manager=token_mgr, user_repository=repo), repo


# ============================================================
# AC-AUTH-01: 用户注册成功，密码 bcrypt 存储
# ============================================================

class TestRegister:
    """用户注册测试。"""

    @pytest.mark.asyncio
    async def test_register_success(self) -> None:
        """注册成功返回用户对象。"""
        svc, repo = _make_service()
        user_create = UserCreate(username="newuser", password="password123")

        user = await svc.register(user_create)

        assert user.username == "newuser"
        assert user.is_active is True

    @pytest.mark.asyncio
    async def test_register_password_is_bcrypt_hash(self) -> None:
        """注册后存储的密码是 bcrypt 哈希，不是明文。"""
        svc, repo = _make_service()
        user_create = UserCreate(username="bcryptuser", password="mypassword123")

        user = await svc.register(user_create)

        # 密码哈希不等于明文
        assert user.password_hash != "mypassword123"
        # bcrypt 哈希以 $2b$ 开头
        assert user.password_hash.startswith("$2")

    @pytest.mark.asyncio
    async def test_register_duplicate_raises(self) -> None:
        """重复注册同名用户抛出 UserExistsError。"""
        svc, repo = _make_service()
        user_create = UserCreate(username="dupuser", password="password123")
        await svc.register(user_create)

        with pytest.raises(UserExistsError):
            await svc.register(user_create)


# ============================================================
# F-AUTH-03: 密码验证（bcrypt 对比）
# ============================================================

class TestPasswordHashing:
    """密码哈希和验证测试。"""

    def test_hash_and_verify_password(self) -> None:
        """哈希后的密码可以通过 verify_password 验证。"""
        plain = "test_password_123"
        hashed = hash_password(plain)

        assert verify_password(plain, hashed) is True

    def test_verify_wrong_password(self) -> None:
        """错误密码验证失败。"""
        hashed = hash_password("correct_password")

        assert verify_password("wrong_password", hashed) is False

    def test_hash_is_different_each_time(self) -> None:
        """每次哈希结果不同（salt 随机）。"""
        h1 = hash_password("same")
        h2 = hash_password("same")

        assert h1 != h2
        assert verify_password("same", h1) is True
        assert verify_password("same", h2) is True


# ============================================================
# F-AUTH-02 / F-AUTH-03 / F-AUTH-05: 登录认证
# ============================================================

class TestAuthenticate:
    """用户登录认证测试。"""

    @pytest.mark.asyncio
    async def test_authenticate_success_returns_tokens(self) -> None:
        """正确凭证登录返回 access_token 和 refresh_token。"""
        svc, repo = _make_service()
        await svc.register(UserCreate(username="loginuser", password="password123"))

        result = await svc.authenticate("loginuser", "password123")

        assert "access_token" in result
        assert "refresh_token" in result
        assert result["token_type"] == "bearer"
        assert result["expires_in"] > 0

    @pytest.mark.asyncio
    async def test_authenticate_wrong_password_raises(self) -> None:
        """错误密码抛出 InvalidCredentialsError。"""
        svc, repo = _make_service()
        await svc.register(UserCreate(username="loginuser", password="password123"))

        with pytest.raises(InvalidCredentialsError):
            await svc.authenticate("loginuser", "wrongpassword")

    @pytest.mark.asyncio
    async def test_authenticate_nonexistent_user_raises(self) -> None:
        """不存在用户抛出 InvalidCredentialsError。"""
        svc, repo = _make_service()

        with pytest.raises(InvalidCredentialsError):
            await svc.authenticate("ghost", "password123")

    @pytest.mark.asyncio
    async def test_authenticate_inactive_user_raises(self) -> None:
        """已禁用用户抛出 UserInactiveError。"""
        svc, repo = _make_service()
        repo.add_inactive_user("inactive_user", "password123")

        with pytest.raises(UserInactiveError):
            await svc.authenticate("inactive_user", "password123")


# ============================================================
# F-AUTH-08: Token 刷新（服务层）
# ============================================================

class TestAuthServiceRefresh:
    """AuthService.refresh_token 测试。"""

    @pytest.mark.asyncio
    async def test_refresh_token_success(self) -> None:
        """refresh_token 刷新成功返回新 Token。"""
        svc, repo = _make_service()
        await svc.register(UserCreate(username="refreshuser", password="password123"))

        auth_result = await svc.authenticate("refreshuser", "password123")

        new_result = await svc.refresh_token(auth_result["refresh_token"])

        assert "access_token" in new_result
        assert new_result["access_token"] != auth_result["access_token"]

    @pytest.mark.asyncio
    async def test_refresh_token_revoked_raises(self) -> None:
        """已撤销的 refresh_token 刷新失败。"""
        from src.core.exceptions import TokenRevokedError

        svc, repo = _make_service()
        await svc.register(UserCreate(username="refreshuser2", password="password123"))
        auth_result = await svc.authenticate("refreshuser2", "password123")

        user = await repo.get_by_username("refreshuser2")
        await svc.logout(user_id=user.id, refresh_token=auth_result["refresh_token"])

        with pytest.raises(TokenRevokedError):
            await svc.refresh_token(auth_result["refresh_token"])
