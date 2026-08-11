"""
认证模块数据模型

定义用户、Token 等相关的数据结构
"""

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class UserCreate(BaseModel):
    """用户创建请求"""

    username: str = Field(..., min_length=3, max_length=255, description="用户名")
    password: str = Field(..., min_length=8, max_length=255, description="密码")
    email: str | None = Field(None, description="邮箱")
    role: str = Field(default="user", description="角色")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        """验证用户名格式"""
        # 先去除空格
        v = v.strip() if v else v
        if not v:
            raise ValueError("用户名不能为空")
        # 只允许字母、数字、下划线
        if not re.match(r"^[a-zA-Z0-9_]+$", v):
            raise ValueError("用户名只能包含字母、数字和下划线")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """验证密码强度"""
        if len(v) < 8:
            raise ValueError("密码长度至少为8位")
        return v


class UserInDB(BaseModel):
    """数据库中的用户模型"""

    model_config = {"from_attributes": True}

    id: UUID = Field(..., description="用户 ID")
    username: str = Field(..., description="用户名")
    email: str | None = Field(None, description="邮箱")
    password_hash: str = Field(..., description="密码哈希")
    role: str = Field(default="user", description="角色")
    is_active: bool = Field(default=True, description="是否激活")
    preferences: dict[str, Any] = Field(default_factory=dict, description="用户偏好")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime | None = Field(None, description="更新时间")


class TokenPayload(BaseModel):
    """JWT Token 载荷"""

    sub: str = Field(..., description="主题（用户 ID）")
    exp: datetime = Field(..., description="过期时间")
    iat: datetime = Field(..., description="签发时间")
    type: str = Field(..., description="Token 类型: access/refresh")
    role: str = Field(default="user", description="用户角色")
    jti: str | None = Field(None, description="Token 唯一标识")


class TokenPair(BaseModel):
    """Token 对"""

    access_token: str = Field(..., description="访问令牌")
    refresh_token: str = Field(..., description="刷新令牌")
    token_type: str = Field(default="bearer", description="令牌类型")
    expires_in: int = Field(..., description="访问令牌有效期（秒）")
