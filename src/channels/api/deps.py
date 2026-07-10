"""API 共享依赖模块。"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from channels.api.auth import get_current_user

logger = logging.getLogger(__name__)


# 认证依赖注入


def _extract_token(authorization: str) -> str:
    """从 Authorization 头提取 Bearer token。"""
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:]
    return ""


async def require_auth(
    authorization: str = Header(default="", description="Bearer token"),
) -> dict[str, Any]:
    """FastAPI 依赖：验证 Bearer token 并返回用户信息。"""
    actual_token = _extract_token(authorization)
    if not actual_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_info = get_current_user(actual_token)
    if user_info is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或过期的认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_info


async def optional_auth(
    authorization: str = Header(default="", description="Bearer token"),
) -> dict[str, Any] | None:
    """FastAPI 依赖：可选认证，不强制要求 token。"""
    actual_token = _extract_token(authorization)
    if not actual_token:
        return None
    return get_current_user(actual_token)


# 标准错误响应工具


class APIError(Exception):
    """API 业务异常，携带错误码和 HTTP 状态码。"""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def error_response(
    status_code: int,
    error_code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """构造标准错误 JSON 响应。"""
    body: dict[str, Any] = {
        "error": {
            "code": error_code,
            "message": message,
        }
    }
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    """FastAPI 异常处理器，将 APIError 转为标准 JSON 响应。"""
    return error_response(
        status_code=exc.status_code,
        error_code=exc.error_code,
        message=exc.message,
        details=exc.details,
    )


async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理器，防止未处理异常泄露堆栈。"""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return error_response(
        status_code=500,
        error_code="SYS_ERR_8003",
        message="服务内部错误，请稍后重试",
    )


# 请求验证工具


def validate_pagination(limit: int, offset: int, max_limit: int = 100) -> None:
    """验证分页参数。"""
    if limit < 1 or limit > max_limit:
        raise APIError(
            status_code=400,
            error_code="VAL_RANGE_7003",
            message=f"limit 必须在 1-{max_limit} 之间",
        )
    if offset < 0:
        raise APIError(
            status_code=400,
            error_code="VAL_RANGE_7003",
            message="offset 不能为负数",
        )


# 限流中间件：旧版全局限流器，新代码请用 TieredRateLimiter。


class RateLimiter:
    """基于 IP 的简易滑动窗口限流器。"""

    def __init__(self, max_requests: int = 100, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = {}

    def is_allowed(self, key: str) -> bool:
        """检查请求是否被允许。"""
        now = time.time()
        cutoff = now - self.window_seconds

        # 清理过期记录
        hits = self._hits.get(key, [])
        hits = [t for t in hits if t > cutoff]

        if len(hits) >= self.max_requests:
            self._hits[key] = hits
            return False

        hits.append(now)
        self._hits[key] = hits
        return True


# 全局限流器实例
rate_limiter = RateLimiter(max_requests=300, window_seconds=60)
