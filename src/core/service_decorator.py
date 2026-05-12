"""
服务层装饰器

提供服务层常用的装饰器，包括异常处理、日志记录等。
"""

import asyncio
import functools
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from src.core.exception_wrapper import wrap_exception

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ============================================================================
# 异常处理装饰器
# ============================================================================


def handle_service_exceptions(
    default_error_code: str | None = None,
    reraise: bool = True,
):
    """服务层异常处理装饰器

    自动捕获并包装模块特定异常为核心异常。

    Args:
        default_error_code: 默认错误码
        reraise: 是否重新抛出包装后的异常（True）还是返回错误字典（False）

    Examples:
        >>> @handle_service_exceptions()
        ... async def get_agent(agent_id: str):
        ...     # 如果抛出 AgentNotFoundError，会自动包装为 NotFoundException
        ...     result = await db.get_agent(agent_id)
        ...     return result

        >>> @handle_service_exceptions(reraise=False)
        ... async def get_agent(agent_id: str):
        ...     # 如果抛出异常，返回 {"error": ..., "success": False}
        ...     result = await db.get_agent(agent_id)
        ...     return result
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                # 检查是否已经是核心异常
                from src.core.exceptions import BaseAppException

                if isinstance(e, BaseAppException):
                    if reraise:
                        raise
                    else:
                        return {
                            "success": False,
                            "error": e.to_dict(),
                        }

                # 包装异常
                wrapped_exc = wrap_exception(e, default_error_code)

                # 记录日志
                logger.warning(
                    f"服务层异常已被包装: {func.__name__} | "
                    f"原始: {type(e).__name__} | "
                    f"包装后: {type(wrapped_exc).__name__}",
                    extra={
                        "function": func.__name__,
                        "original_exception": type(e).__name__,
                        "wrapped_exception": type(wrapped_exc).__name__,
                    },
                )

                if reraise:
                    raise wrapped_exc
                else:
                    return {
                        "success": False,
                        "error": wrapped_exc.to_dict(),
                    }

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # 检查是否已经是核心异常
                from src.core.exceptions import BaseAppException

                if isinstance(e, BaseAppException):
                    if reraise:
                        raise
                    else:
                        return {
                            "success": False,
                            "error": e.to_dict(),
                        }

                # 包装异常
                wrapped_exc = wrap_exception(e, default_error_code)

                # 记录日志
                logger.warning(
                    f"服务层异常已被包装: {func.__name__} | "
                    f"原始: {type(e).__name__} | "
                    f"包装后: {type(wrapped_exc).__name__}",
                    extra={
                        "function": func.__name__,
                        "original_exception": type(e).__name__,
                        "wrapped_exception": type(wrapped_exc).__name__,
                    },
                )

                if reraise:
                    raise wrapped_exc
                else:
                    return {
                        "success": False,
                        "error": wrapped_exc.to_dict(),
                    }

        # 根据函数类型返回相应的包装器
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


# ============================================================================
# 日志记录装饰器
# ============================================================================


def log_service_calls(
    log_level: int = logging.INFO,
    log_args: bool = True,
    log_result: bool = False,
):
    """服务调用日志装饰器

    记录服务方法的调用信息。

    Args:
        log_level: 日志级别
        log_args: 是否记录参数
        log_result: 是否记录返回值

    Examples:
        >>> @log_service_calls()
        ... async def get_agent(agent_id: str):
        ...     return agent_data
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            # 构建日志消息
            log_msg = f"调用服务方法: {func.__name__}"
            if log_args:
                # 过滤敏感参数
                safe_kwargs = {
                    k: v
                    for k, v in kwargs.items()
                    if k not in ["password", "token", "secret"]
                }
                log_msg += f" | 参数: args={args}, kwargs={safe_kwargs}"

            logger.log(log_level, log_msg)

            try:
                result = await func(*args, **kwargs)

                if log_result:
                    logger.log(
                        log_level,
                        f"服务方法完成: {func.__name__} | "
                        f"结果类型: {type(result).__name__}",
                    )

                return result
            except Exception as e:
                logger.error(
                    f"服务方法失败: {func.__name__} | 错误: {str(e)}",
                    exc_info=True,
                )
                raise

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            # 构建日志消息
            log_msg = f"调用服务方法: {func.__name__}"
            if log_args:
                # 过滤敏感参数
                safe_kwargs = {
                    k: v
                    for k, v in kwargs.items()
                    if k not in ["password", "token", "secret"]
                }
                log_msg += f" | 参数: args={args}, kwargs={safe_kwargs}"

            logger.log(log_level, log_msg)

            try:
                result = func(*args, **kwargs)

                if log_result:
                    logger.log(
                        log_level,
                        f"服务方法完成: {func.__name__} | "
                        f"结果类型: {type(result).__name__}",
                    )

                return result
            except Exception as e:
                logger.error(
                    f"服务方法失败: {func.__name__} | 错误: {str(e)}",
                    exc_info=True,
                )
                raise

        # 根据函数类型返回相应的包装器
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


# ============================================================================
# 组合装饰器
# ============================================================================


def service_method(
    handle_exceptions: bool = True,
    log_calls: bool = True,
    default_error_code: str | None = None,
):
    """服务方法组合装饰器

    结合异常处理和日志记录功能。

    Args:
        handle_exceptions: 是否处理异常
        log_calls: 是否记录调用
        default_error_code: 默认错误码

    Examples:
        >>> @service_method()
        ... async def get_agent(agent_id: str):
        ...     return agent_data
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        # 应用装饰器（注意顺序：日志在外，异常处理在内）
        if log_calls:
            func = log_service_calls()(func)
        if handle_exceptions:
            func = handle_service_exceptions(default_error_code)(func)

        return func

    return decorator
