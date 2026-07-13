"""
认证模块数据模型

定义 Token 等相关的数据结构（体系 A：被 channels/api/auth.py 与 token.py 使用）。
"""

from datetime import datetime

from pydantic import BaseModel, Field


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
