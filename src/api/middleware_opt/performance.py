"""
性能优化中间件

提供API响应缓存、压缩、性能监控等功能
"""

import gzip
import json
import time
from collections.abc import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.cache import get_global_cache
from src.config.settings import get_settings
from src.monitoring.performance_monitor import get_performance_monitor


class PerformanceMiddleware(BaseHTTPMiddleware):
    """性能优化中间件"""

    def __init__(self, app, enable_cache: bool = True, enable_compression: bool = True):
        super().__init__(app)
        self.enable_cache = enable_cache
        self.enable_compression = enable_compression
        self.settings = get_settings()
        self.cache = get_global_cache()
        self.monitor = get_performance_monitor()

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """处理请求"""
        start_time = time.time()

        try:
            # 尝试从缓存获取响应（仅对GET请求）
            if self.enable_cache and request.method == "GET":
                cached_response = await self._get_cached_response(request)
                if cached_response:
                    return cached_response

            # 执行请求
            response = await call_next(request)

            # 缓存响应（仅对成功的GET请求）
            if (
                self.enable_cache
                and request.method == "GET"
                and response.status_code == 200
            ):
                await self._cache_response(request, response)

            # 压缩响应
            if self.enable_compression:
                response = await self._compress_response(request, response)

            return response

        finally:
            # 记录性能指标
            response_time = (time.time() - start_time) * 1000
            self.monitor.metrics.add_metric(response_time=response_time)

    async def _get_cached_response(self, request: Request) -> Response:
        """从缓存获取响应"""
        cache_key = self._generate_cache_key(request)

        try:
            cached_data = await self.cache.get(cache_key)
            if cached_data:
                return JSONResponse(
                    content=cached_data["content"],
                    status_code=cached_data["status_code"],
                    headers={**cached_data.get("headers", {}), "X-Cache": "HIT"},
                )
        except Exception:
            # 缓存失败不影响正常流程
            pass

        return None

    async def _cache_response(self, request: Request, response: Response):
        """缓存响应"""
        cache_key = self._generate_cache_key(request)

        try:
            # 只缓存JSON响应
            if response.headers.get("content-type", "").startswith("application/json"):
                # 读取响应内容
                response_body = b""
                async for chunk in response.body_iterator:
                    response_body += chunk

                # 解析JSON内容
                content = json.loads(response_body.decode())

                # 缓存数据
                cache_data = {
                    "content": content,
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                }

                await self.cache.set(
                    cache_key, cache_data, ttl=self.settings.api_response_cache_ttl
                )

                # 重新创建响应
                response = JSONResponse(
                    content=content,
                    status_code=response.status_code,
                    headers={**response.headers, "X-Cache": "MISS"},
                )
        except Exception:
            # 缓存失败不影响正常流程
            pass

    async def _compress_response(
        self, request: Request, response: Response
    ) -> Response:
        """压缩响应"""
        # 检查客户端是否支持gzip
        accept_encoding = request.headers.get("accept-encoding", "")
        if "gzip" not in accept_encoding:
            return response

        # 检查响应类型
        content_type = response.headers.get("content-type", "")
        if not (
            content_type.startswith("application/json")
            or content_type.startswith("text/")
        ):
            return response

        try:
            # 读取响应内容
            response_body = b""
            async for chunk in response.body_iterator:
                response_body += chunk

            # 只压缩大于1KB的响应
            if len(response_body) < 1024:
                return response

            # 压缩内容
            compressed_body = gzip.compress(response_body)

            # 创建压缩响应
            return Response(
                content=compressed_body,
                status_code=response.status_code,
                headers={
                    **response.headers,
                    "content-encoding": "gzip",
                    "content-length": str(len(compressed_body)),
                },
            )
        except Exception:
            # 压缩失败返回原响应
            return response

    def _generate_cache_key(self, request: Request) -> str:
        """生成缓存键"""
        # 基于URL和查询参数生成缓存键
        url = str(request.url)
        user_id = getattr(request.state, "user_id", "anonymous")
        return f"api_cache:{user_id}:{hash(url)}"


class ResponseTimeMiddleware(BaseHTTPMiddleware):
    """响应时间中间件（轻量级）"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """处理请求"""
        start_time = time.time()

        response = await call_next(request)

        # 添加响应时间头
        response_time = (time.time() - start_time) * 1000
        response.headers["X-Response-Time"] = f"{response_time:.2f}ms"

        return response
