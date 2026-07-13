"""
缓存装饰器

提供函数级别的缓存装饰器
"""

import asyncio
import hashlib
import json
from collections.abc import Callable
from functools import wraps

from .multi_level_cache import get_global_cache


def cache_key_generator(*args, **kwargs) -> str:
    """
    生成缓存键

    Args:
        *args: 位置参数
        **kwargs: 关键字参数

    Returns:
        缓存键
    """
    # 将参数序列化为字符串
    key_data = {
        "args": args,
        "kwargs": kwargs,
    }
    key_str = json.dumps(key_data, sort_keys=True, default=str)

    # 生成哈希 - 使用SHA256替代MD5以提高安全性
    return hashlib.sha256(key_str.encode()).hexdigest()


def cached_function(
    ttl: int | None = None,
    key_prefix: str = "",
    key_generator: Callable | None = None,
):
    """
    函数缓存装饰器

    Args:
        ttl: 缓存TTL（秒）
        key_prefix: 缓存键前缀
        key_generator: 自定义键生成器

    Returns:
        装饰器函数
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            cache = get_global_cache()

            # 生成缓存键
            cache_key = key_generator(*args, **kwargs) if key_generator else cache_key_generator(*args, **kwargs)

            if key_prefix:
                cache_key = f"{key_prefix}:{cache_key}"

            # 尝试从缓存获取
            cached_result = await cache.get(cache_key)
            if cached_result is not None:
                return cached_result

            # 执行函数
            result = await func(*args, **kwargs)

            # 存入缓存
            await cache.set(cache_key, result, ttl)

            return result

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            # 对于同步函数，使用异步包装
            async def async_func():
                cache = get_global_cache()

                # 生成缓存键
                cache_key = key_generator(*args, **kwargs) if key_generator else cache_key_generator(*args, **kwargs)

                if key_prefix:
                    cache_key = f"{key_prefix}:{cache_key}"

                # 尝试从缓存获取
                cached_result = await cache.get(cache_key)
                if cached_result is not None:
                    return cached_result

                # 执行函数
                result = func(*args, **kwargs)

                # 存入缓存
                await cache.set(cache_key, result, ttl)

                return result

            # 在事件循环中运行
            try:
                loop = asyncio.get_event_loop()
                return loop.run_until_complete(async_func())
            except RuntimeError:
                # 如果没有事件循环，创建一个新的
                return asyncio.run(async_func())

        # 根据函数类型返回对应的包装器
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


def cache_result(
    ttl: int = 300,
    key_prefix: str = "result",
):
    """
    结果缓存装饰器（简化版）

    Args:
        ttl: 缓存TTL（秒）
        key_prefix: 缓存键前缀

    Returns:
        装饰器函数
    """
    return cached_function(ttl=ttl, key_prefix=key_prefix)


def invalidate_cache(pattern: str):
    """
    缓存失效装饰器

    Args:
        pattern: 要失效的缓存键模式

    Returns:
        装饰器函数
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            # 执行函数
            result = await func(*args, **kwargs)

            # 失效缓存
            cache = get_global_cache()
            await cache.clear_pattern(pattern)

            return result

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            # 执行函数
            result = func(*args, **kwargs)

            # 失效缓存
            async def invalidate():
                cache = get_global_cache()
                await cache.clear_pattern(pattern)

            # 在事件循环中运行
            try:
                loop = asyncio.get_event_loop()
                loop.run_until_complete(invalidate())
            except RuntimeError:
                asyncio.run(invalidate())

            return result

        # 根据函数类型返回对应的包装器
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
