"""
API 中间件模块

提供请求处理的中间件，包括：
- 请求日志记录
- 错误处理
- CORS 配置
- 限流
"""

import logging
import time
from collections.abc import Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.api.dependencies import generate_trace_id
from src.api.errors import create_error_response
from src.api.rate_limit import (
    RateLimitConfig,
    RateLimitKeyStrategy,
    SlidingWindowRateLimiter,
    get_rate_limit_key,
)

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """请求日志中间件"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """处理请求并记录日志"""
        # 生成追踪 ID
        trace_id = generate_trace_id()
        request.state.trace_id = trace_id

        # 记录请求开始时间
        start_time = time.time()

        # 记录请求信息
        logger.info(
            f"请求开始 | {request.method} {request.url.path} | trace_id={trace_id}"
        )

        # 处理请求
        response = await call_next(request)

        # 计算处理时间
        process_time = time.time() - start_time
        process_time_ms = round(process_time * 1000, 2)

        # 添加响应头
        response.headers["X-Request-ID"] = trace_id
        response.headers["X-Process-Time"] = f"{process_time_ms}ms"

        # 记录响应信息
        logger.info(
            f"请求完成 | {request.method} {request.url.path} | "
            f"status={response.status_code} | "
            f"time={process_time_ms}ms | "
            f"trace_id={trace_id}"
        )

        return response


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """错误处理中间件

    注意：此中间件主要用于捕获未被 FastAPI 异常处理器处理的错误。
    大部分错误应该通过 FastAPI 的异常处理器机制处理。
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """处理请求并捕获异常"""
        trace_id = getattr(request.state, "trace_id", generate_trace_id())

        try:
            response = await call_next(request)
            return response
        except Exception as e:
            # 使用新的错误处理器创建响应
            from src.api.error_handler import create_error_response_from_exception

            # 记录错误日志
            logger.error(
                f"中间件捕获异常 | trace_id={trace_id} | "
                f"type={type(e).__name__} | message={str(e)}",
                exc_info=True,
            )

            # 创建错误响应
            return create_error_response_from_exception(
                exc=e,
                trace_id=trace_id,
                request_path=str(request.url.path),
            )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """限流中间件"""

    def __init__(self, app, config: RateLimitConfig):
        super().__init__(app)
        self.config = config
        self.limiter = SlidingWindowRateLimiter(config)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """处理请求并检查限流"""
        # 获取客户端 IP
        client_ip = request.client.host if request.client else "unknown"

        # 获取用户 ID（如果已认证）
        user_id = getattr(request.state, "user_id", None)

        # 生成限流键
        key = get_rate_limit_key(
            strategy=RateLimitKeyStrategy.IP,
            user_id=user_id,
            client_ip=client_ip,
            endpoint=request.url.path,
        )

        # 检查限流
        allowed, headers = self.limiter.is_allowed(key)

        if not allowed:
            trace_id = getattr(request.state, "trace_id", generate_trace_id())
            error_response = create_error_response(
                error_code="SYS_007", trace_id=trace_id, path=str(request.url.path)
            )

            response = JSONResponse(
                status_code=429, content=error_response.model_dump(mode="json")
            )

            # 添加限流响应头
            response.headers["X-RateLimit-Limit"] = str(headers.x_ratelimit_limit)
            response.headers["X-RateLimit-Remaining"] = str(
                headers.x_ratelimit_remaining
            )
            response.headers["X-RateLimit-Reset"] = str(headers.x_ratelimit_reset)
            response.headers["X-RateLimit-Window"] = str(headers.x_ratelimit_window)
            if headers.retry_after:
                response.headers["Retry-After"] = str(headers.retry_after)

            return response

        # 处理请求
        response = await call_next(request)

        # 添加限流响应头
        response.headers["X-RateLimit-Limit"] = str(headers.x_ratelimit_limit)
        response.headers["X-RateLimit-Remaining"] = str(headers.x_ratelimit_remaining)
        response.headers["X-RateLimit-Reset"] = str(headers.x_ratelimit_reset)
        response.headers["X-RateLimit-Window"] = str(headers.x_ratelimit_window)

        return response


def setup_cors(
    app: FastAPI,
    allowed_origins: list[str] | None = None,
    allow_credentials: bool = True,
    allow_methods: list[str] | None = None,
    allow_headers: list[str] | None = None,
) -> None:
    """
    配置 CORS 中间件

    Args:
        app: FastAPI 应用实例
        allowed_origins: 允许的源列表
        allow_credentials: 是否允许凭证
        allow_methods: 允许的 HTTP 方法
        allow_headers: 允许的请求头
    """
    if allowed_origins is None:
        allowed_origins = ["*"]

    if allow_methods is None:
        allow_methods = ["*"]

    if allow_headers is None:
        allow_headers = ["*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=allow_credentials,
        allow_methods=allow_methods,
        allow_headers=allow_headers,
    )


def setup_middlewares(
    app: FastAPI,
    cors_origins: list[str] | None = None,
    rate_limit_config: RateLimitConfig | None = None,
) -> None:
    """
    配置所有中间件

    Args:
        app: FastAPI 应用实例
        cors_origins: CORS 允许的源列表
        rate_limit_config: 限流配置
    """
    # 错误处理中间件（最先添加，最后执行）
    app.add_middleware(ErrorHandlingMiddleware)

    # 请求日志中间件
    app.add_middleware(RequestLoggingMiddleware)

    # 限流中间件
    if rate_limit_config:
        app.add_middleware(RateLimitMiddleware, config=rate_limit_config)

    # CORS 中间件
    setup_cors(app, allowed_origins=cors_origins)
