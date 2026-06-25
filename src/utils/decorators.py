"""
通用装饰器

提供常用的装饰器功能，减少重复代码
"""

import asyncio
import functools
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def handle_exceptions(
    default_return: Any = None,
    log_error: bool = True,
    error_message: str | None = None,
    reraise: bool = False,
):
    """
    异常处理装饰器

    Args:
        default_return: 异常时的默认返回值
        log_error: 是否记录错误日志
        error_message: 自定义错误消息模板，可使用 {func_name} 和 {error} 占位符
        reraise: 是否重新抛出异常

    Returns:
        装饰器函数
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T | Any]:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                if log_error:
                    if error_message:
                        msg = error_message.format(
                            func_name=func.__name__, error=str(e)
                        )
                    else:
                        msg = f"{func.__name__} 执行失败: {e}"
                    logger.error(msg)

                if reraise:
                    raise

                return default_return

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if log_error:
                    if error_message:
                        msg = error_message.format(
                            func_name=func.__name__, error=str(e)
                        )
                    else:
                        msg = f"{func.__name__} 执行失败: {e}"
                    logger.error(msg)

                if reraise:
                    raise

                return default_return

        # 根据函数是否为协程选择合适的包装器
        if hasattr(func, "__code__") and func.__code__.co_flags & 0x80:  # CO_COROUTINE
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


def retry_on_failure(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff_factor: float = 2.0,
    exceptions: tuple = (Exception,),
):
    """
    重试装饰器

    Args:
        max_retries: 最大重试次数
        delay: 初始延迟时间（秒）
        backoff_factor: 延迟时间递增因子
        exceptions: 需要重试的异常类型

    Returns:
        装饰器函数
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(
                            f"{func.__name__} 第 {attempt + 1} 次尝试失败: {e}, "
                            f"{current_delay:.1f}秒后重试"
                        )
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff_factor
                    else:
                        logger.error(f"{func.__name__} 重试 {max_retries} 次后仍然失败")

            raise last_exception

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(
                            f"{func.__name__} 第 {attempt + 1} 次尝试失败: {e}, "
                            f"{current_delay:.1f}秒后重试"
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff_factor
                    else:
                        logger.error(f"{func.__name__} 重试 {max_retries} 次后仍然失败")

            raise last_exception

        # 根据函数是否为协程选择合适的包装器
        if hasattr(func, "__code__") and func.__code__.co_flags & 0x80:  # CO_COROUTINE
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


def log_execution_time(log_level: int = logging.INFO):
    """
    执行时间记录装饰器

    Args:
        log_level: 日志级别

    Returns:
        装饰器函数
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = await func(*args, **kwargs)
                execution_time = time.time() - start_time
                logger.log(
                    log_level, f"{func.__name__} 执行完成，耗时: {execution_time:.3f}秒"
                )
                return result
            except Exception as e:
                execution_time = time.time() - start_time
                logger.log(
                    log_level,
                    f"{func.__name__} 执行失败，耗时: {execution_time:.3f}秒，错误: {e}",
                )
                raise

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                execution_time = time.time() - start_time
                logger.log(
                    log_level, f"{func.__name__} 执行完成，耗时: {execution_time:.3f}秒"
                )
                return result
            except Exception as e:
                execution_time = time.time() - start_time
                logger.log(
                    log_level,
                    f"{func.__name__} 执行失败，耗时: {execution_time:.3f}秒，错误: {e}",
                )
                raise

        # 根据函数是否为协程选择合适的包装器
        if hasattr(func, "__code__") and func.__code__.co_flags & 0x80:  # CO_COROUTINE
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


def validate_params(**validators):
    """
    参数验证装饰器

    Args:
        **validators: 参数名到验证函数的映射

    Returns:
        装饰器函数

    Example:
        @validate_params(
            user_id=lambda x: isinstance(x, str) and len(x) > 0,
            age=lambda x: isinstance(x, int) and x >= 0
        )
        def create_user(user_id: str, age: int):
            pass
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # 获取函数参数名
            sig = inspect.signature(func)
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()

            # 验证参数
            for param_name, validator in validators.items():
                if param_name in bound_args.arguments:
                    value = bound_args.arguments[param_name]
                    if not validator(value):
                        raise ValueError(f"参数 {param_name} 验证失败: {value}")

            return func(*args, **kwargs)

        return wrapper

    return decorator
