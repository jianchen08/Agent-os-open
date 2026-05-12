"""
认证相关数据模型
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class RegisterRequest(BaseModel):
    """注册请求"""

    username: str = Field(..., min_length=3, max_length=50, description="用户名")
    email: str = Field(..., description="邮箱")
    password: str = Field(..., min_length=6, max_length=100, description="密码")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        """验证用户名"""
        if not v or not v.strip():
            raise ValueError("用户名不能为空")
        v = v.strip()
        if len(v) < 3:
            raise ValueError("用户名至少3个字符")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """验证邮箱"""
        if not v or "@" not in v:
            raise ValueError("邮箱格式无效")
        return v.strip().lower()

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """验证密码"""
        if not v:
            raise ValueError("密码不能为空")
        if len(v) < 6:
            raise ValueError("密码至少6个字符")
        return v


class LoginRequest(BaseModel):
    """登录请求"""

    username: str = Field(..., min_length=1, max_length=255, description="用户名")
    password: str = Field(..., min_length=1, max_length=255, description="密码")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        """验证用户名"""
        if not v or not v.strip():
            raise ValueError("用户名不能为空")
        return v.strip()

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """验证密码"""
        if not v:
            raise ValueError("密码不能为空")
        return v


class RefreshTokenRequest(BaseModel):
    """刷新 Token 请求"""

    refresh_token: str = Field(..., description="Refresh Token")


class LogoutRequest(BaseModel):
    """登出请求"""

    refresh_token: str | None = Field(None, description="Refresh Token")
    logout_all_devices: bool = Field(False, description="是否登出所有设备")


class TokenResponse(BaseModel):
    """Token 响应"""

    access_token: str = Field(..., description="Access Token")
    refresh_token: str = Field(..., description="Refresh Token")
    token_type: str = Field(default="bearer", description="Token 类型")
    expires_in: int = Field(..., description="Access Token 有效期（秒）")


class UserResponse(BaseModel):
    """用户信息响应"""

    id: UUID = Field(..., description="用户 ID")
    username: str = Field(..., description="用户名")
    email: str | None = Field(None, description="邮箱")
    role: str = Field(..., description="角色")
    created_at: datetime = Field(..., description="创建时间")
    last_login_at: datetime | None = Field(None, description="最后登录时间")
