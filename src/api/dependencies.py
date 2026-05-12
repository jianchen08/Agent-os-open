"""
API 公共依赖模块

提供 FastAPI 依赖注入的公共组件，包括：
- 链路追踪 ID 生成
- 分页参数
- 排序参数
- 过滤参数
- 通用查询参数
- 数据库会话获取
- 用户认证
"""

import uuid
from typing import Literal
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.models import UserInDB
from src.auth.token import TokenManager
from src.config.settings import get_settings
from src.db.connection import get_async_session
from src.db.repositories import UserRepository

# 初始化 TokenManager
settings = get_settings()
token_manager = TokenManager(
    secret_key=settings.jwt_secret_key,
    algorithm=settings.jwt_algorithm,
    access_token_expire_minutes=settings.access_token_expire_minutes,
    refresh_token_expire_days=settings.refresh_token_expire_days,
)


def generate_trace_id() -> str:
    """
    生成链路追踪 ID

    Returns:
        格式为 req-{uuid} 的追踪 ID
    """
    return f"req-{uuid.uuid4().hex[:12]}"


class PaginationParams(BaseModel):
    """分页参数"""

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(default=20, ge=1, le=100, description="每页数量，最大 100")

    @property
    def offset(self) -> int:
        """计算偏移量"""
        return (self.page - 1) * self.page_size

    @field_validator("page")
    @classmethod
    def validate_page(cls, v: int) -> int:
        """验证页码"""
        if v < 1:
            raise ValueError("页码必须大于等于 1")
        return v

    @field_validator("page_size")
    @classmethod
    def validate_page_size(cls, v: int) -> int:
        """验证每页数量"""
        if v < 1:
            raise ValueError("每页数量必须大于等于 1")
        if v > 100:
            raise ValueError("每页数量不能超过 100")
        return v


class SortingParams(BaseModel):
    """排序参数"""

    sort_by: str = Field(default="created_at", description="排序字段")
    sort_order: Literal["asc", "desc"] = Field(default="desc", description="排序顺序")

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, v: str) -> str:
        """验证排序顺序"""
        if v not in ("asc", "desc"):
            raise ValueError("排序顺序只能是 asc 或 desc")
        return v


class FilterParams(BaseModel):
    """过滤参数"""

    status: str | None = Field(default=None, description="状态过滤")
    tags: list[str] | None = Field(default=None, description="标签过滤")
    search: str | None = Field(default=None, description="搜索关键词")


class CommonQueryParams(BaseModel):
    """通用查询参数，组合分页、排序和过滤"""

    # 分页
    page: int = Field(default=1, ge=1, description="页码")
    page_size: int = Field(default=20, ge=1, le=100, description="每页数量")

    # 排序
    sort_by: str = Field(default="created_at", description="排序字段")
    sort_order: Literal["asc", "desc"] = Field(default="desc", description="排序顺序")

    # 过滤
    status: str | None = Field(default=None, description="状态过滤")
    tags: list[str] | None = Field(default=None, description="标签过滤")
    search: str | None = Field(default=None, description="搜索关键词")

    @property
    def offset(self) -> int:
        """计算偏移量"""
        return (self.page - 1) * self.page_size

    @field_validator("page")
    @classmethod
    def validate_page(cls, v: int) -> int:
        """验证页码"""
        if v < 1:
            raise ValueError("页码必须大于等于 1")
        return v

    @field_validator("page_size")
    @classmethod
    def validate_page_size(cls, v: int) -> int:
        """验证每页数量"""
        if v < 1:
            raise ValueError("每页数量必须大于等于 1")
        if v > 100:
            raise ValueError("每页数量不能超过 100")
        return v

    @field_validator("sort_order")
    @classmethod
    def validate_sort_order(cls, v: str) -> str:
        """验证排序顺序"""
        if v not in ("asc", "desc"):
            raise ValueError("排序顺序只能是 asc 或 desc")
        return v


async def get_db() -> AsyncSession:
    """
    获取数据库会话（FastAPI 依赖注入）

    Returns:
        AsyncSession: 数据库会话
    """
    async for session in get_async_session():
        yield session


async def get_current_user(
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_async_session),
) -> UserInDB:
    """
    获取当前用户（JWT 验证）

    Args:
        authorization: Authorization header (Bearer token)
        session: 数据库会话

    Returns:
        当前用户对象

    Raises:
        HTTPException: 认证失败
    """
    # 提取 Bearer Token
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少认证令牌"
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的认证格式"
        )

    token = authorization[7:]  # 去掉 "Bearer " 前缀

    try:
        # 验证 Token
        payload = token_manager.verify_token(token, token_type="access")

        # 获取用户
        user_repo = UserRepository(session)
        user = await user_repo.get_by_id(UUID(payload.sub))

        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在"
            )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="用户已被禁用"
            )

        return user

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"无效的令牌格式: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=f"认证失败: {str(e)}"
        )


async def get_current_user_websocket(
    token: str | None = Query(None, description="WebSocket 认证令牌"),
    session: AsyncSession = Depends(get_async_session),
) -> UserInDB:
    """
    获取当前用户（WebSocket 连接认证）

    通过查询参数传递 token，用于 WebSocket 连接的用户认证。

    Args:
        token: 认证令牌（通过查询参数传递）
        session: 数据库会话

    Returns:
        当前用户对象

    Raises:
        HTTPException: 认证失败
    """
    # 检查 Token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少认证令牌"
        )

    try:
        # 验证 Token
        payload = token_manager.verify_token(token, token_type="access")

        # 获取用户
        user_repo = UserRepository(session)
        user = await user_repo.get_by_id(UUID(payload.sub))

        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在"
            )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="用户已被禁用"
            )

        return user

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"无效的令牌格式: {str(e)}",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=f"认证失败: {str(e)}"
        )
