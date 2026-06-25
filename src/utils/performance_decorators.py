"""
性能监控装饰器

提供用于监控函数执行时间的装饰器
"""

import asyncio
import functools
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


def monitor_performance(
    func: Callable | None = None,
    *,
    name: str | None = None,
    threshold: float = 1.0,
    log_args: bool = False,
) -> Callable:
    """
    性能监控装饰器

    Args:
        func: 被装饰的函数
        name: 监控名称（默认使用函数名）
        threshold: 慢执行阈值（秒）
        log_args: 是否记录函数参数

    Returns:
        装饰后的函数
    """

    def decorator(f: Callable) -> Callable:
        @functools.wraps(f)
        async def async_wrapper(*args, **kwargs):
            monitor_name = name or f"{f.__module__}.{f.__name__}"
            start_time = time.time()

            try:
                result = await f(*args, **kwargs)
                return result
            finally:
                elapsed = time.time() - start_time

                if elapsed > threshold:
                    args_str = ""
                    if log_args:
                        args_str = (
                            f" | args={args[:2]} | kwargs={list(kwargs.keys())[:3]}"
                        )

                    logger.warning(
                        f"[Performance] 慢函数检测 | "
                        f"func={monitor_name} | "
                        f"elapsed={elapsed:.3f}s | "
                        f"threshold={threshold}s{args_str}"
                    )
                else:
                    logger.debug(
                        f"[Performance] 函数执行 | "
                        f"func={monitor_name} | "
                        f"elapsed={elapsed:.3f}s"
                    )

        @functools.wraps(f)
        def sync_wrapper(*args, **kwargs):
            monitor_name = name or f"{f.__module__}.{f.__name__}"
            start_time = time.time()

            try:
                result = f(*args, **kwargs)
                return result
            finally:
                elapsed = time.time() - start_time

                if elapsed > threshold:
                    args_str = ""
                    if log_args:
                        args_str = (
                            f" | args={args[:2]} | kwargs={list(kwargs.keys())[:3]}"
                        )

                    logger.warning(
                        f"[Performance] 慢函数检测 | "
                        f"func={monitor_name} | "
                        f"elapsed={elapsed:.3f}s | "
                        f"threshold={threshold}s{args_str}"
                    )
                else:
                    logger.debug(
                        f"[Performance] 函数执行 | "
                        f"func={monitor_name} | "
                        f"elapsed={elapsed:.3f}s"
                    )

        # 根据函数类型返回对应的包装器
        if asyncio.iscoroutinefunction(f):
            return async_wrapper
        else:
            return sync_wrapper

    if func is not None:
        return decorator(func)
    return decorator


def count_queries(func: Callable) -> Callable:
    """
    数据库查询计数装饰器

    统计函数执行期间的数据库查询次数

    Args:
        func: 被装饰的函数

    Returns:
        装饰后的函数
    """

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        from src.db.performance_monitor import get_performance_monitor

        monitor = get_performance_monitor()
        initial_count = len(monitor._session_queries)

        try:
            result = await func(*args, **kwargs)
            return result
        finally:
            query_count = len(monitor._session_queries) - initial_count

            if query_count > 10:
                logger.warning(
                    f"[Performance] 数据库查询过多 | "
                    f"func={func.__name__} | "
                    f"query_count={query_count}"
                )
            elif query_count > 0:
                logger.debug(
                    f"[Performance] 数据库查询统计 | "
                    f"func={func.__name__} | "
                    f"query_count={query_count}"
                )

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        # 同步函数暂不支持查询计数
        return func(*args, **kwargs)

    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    else:
        return sync_wrapper


def batch_operation(batch_size: int = 100):
    """
    批量操作装饰器

    自动分批处理大量数据，避免单次操作内存溢出

    Args:
        batch_size: 批次大小

    Returns:
        装饰器
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            # 检查是否有 items 参数
            items = kwargs.get("items")

            if items is None and len(args) > 0:
                items = args[0]

            if not isinstance(items, list):
                return await func(*args, **kwargs)

            if len(items) <= batch_size:
                return await func(*args, **kwargs)

            # 分批处理
            results = []
            total_batches = (len(items) + batch_size - 1) // batch_size

            for i in range(0, len(items), batch_size):
                batch = items[i : i + batch_size]
                batch_num = i // batch_size + 1

                logger.debug(
                    f"[Performance] 批量处理 | "
                    f"func={func.__name__} | "
                    f"batch={batch_num}/{total_batches} | "
                    f"size={len(batch)}"
                )

                # 更新参数
                if "items" in kwargs:
                    kwargs["items"] = batch
                else:
                    args = (batch,) + args[1:]

                batch_result = await func(*args, **kwargs)
                results.extend(
                    batch_result if isinstance(batch_result, list) else [batch_result]
                )

            return results

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            # 同步版本暂不支持批量处理
            return func(*args, **kwargs)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator
