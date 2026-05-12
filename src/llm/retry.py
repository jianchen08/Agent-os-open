"""
重试处理器

提供 LLM 调用的重试和降级策略
"""

import asyncio
import logging
from collections.abc import Callable
from typing import TypeVar

from src.core.exceptions import (
    AuthenticationError,
    InvalidRequestError,
    RateLimitError,
)
from src.core.exceptions import (
    LLMException as LLMError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryHandler:
    """重试处理器"""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
    ):
        """
        初始化重试处理器

        Args:
            max_retries: 最大重试次数
            base_delay: 基础延迟（秒）
            max_delay: 最大延迟（秒）
            exponential_base: 指数退避基数
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base

    def _calculate_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """计算延迟时间"""
        if retry_after:
            return min(retry_after, self.max_delay)

        delay = self.base_delay * (self.exponential_base**attempt)
        return min(delay, self.max_delay)

    def _should_retry(self, error: Exception) -> bool:
        """判断是否应该重试"""
        # 认证错误不重试
        if isinstance(error, AuthenticationError):
            return False

        # 无效请求不重试
        if isinstance(error, InvalidRequestError):
            return False

        # 速率限制和其他 LLM 错误可以重试
        if isinstance(error, (RateLimitError, LLMError)):
            return True

        # 网络相关错误可以重试
        if isinstance(error, (ConnectionError, TimeoutError)):
            return True

        return False

    async def execute_with_retry(
        self,
        func: Callable[..., T],
        *args,
        fallback_func: Callable[..., T] | None = None,
        **kwargs,
    ) -> T:
        """
        带重试的执行

        Args:
            func: 要执行的异步函数
            *args: 位置参数
            fallback_func: 降级函数
            **kwargs: 关键字参数

        Returns:
            函数执行结果

        Raises:
            LLMError: 重试耗尽后抛出最后一个错误
        """
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                last_error = e

                if not self._should_retry(e):
                    logger.warning(f"不可重试的错误: {e}")
                    raise

                if attempt < self.max_retries:
                    retry_after = getattr(e, "retry_after", None)
                    delay = self._calculate_delay(attempt, retry_after)
                    logger.warning(
                        f"第 {attempt + 1} 次尝试失败: {e}，{delay:.1f} 秒后重试"
                    )
                    await asyncio.sleep(delay)

        # 尝试降级
        if fallback_func:
            logger.warning("主函数重试耗尽，尝试降级")
            try:
                return await fallback_func(*args, **kwargs)
            except Exception as e:
                logger.error(f"降级函数也失败: {e}")
                raise

        # 抛出最后一个错误
        if last_error:
            raise last_error

        raise LLMError("未知错误")


# 模块级单例
_retry_handler: RetryHandler | None = None


def get_retry_handler() -> RetryHandler:
    """获取重试处理器单例"""
    global _retry_handler
    if _retry_handler is None:
        _retry_handler = RetryHandler()
    return _retry_handler
