"""
LLM 调度器

提供 LLM 请求的并发控制和调度功能
支持按提供商配置不同的并发限制
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from src.config.settings import get_settings

logger = logging.getLogger(__name__)


class LLMScheduler:
    """
    LLM 调度器

    控制对 LLM API 的并发请求，避免超过速率限制
    支持按提供商配置不同的并发限制
    """

    def __init__(
        self,
        provider: str = "default",
        max_concurrent: int | None = None,
        rate_limit_per_minute: int = 60,
    ):
        """
        初始化调度器

        Args:
            provider: 提供商名称
            max_concurrent: 最大并发请求数（None 则从配置读取）
            rate_limit_per_minute: 每分钟最大请求数
        """
        self.provider = provider
        self.rate_limit_per_minute = rate_limit_per_minute

        # 从配置读取并发限制
        if max_concurrent is None:
            settings = get_settings()
            max_concurrent = self._get_max_concurrent_from_settings(settings, provider)

        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._request_count = 0
        self._last_reset = asyncio.get_event_loop().time()

        logger.info(
            f"[LLM 调度器] {provider} 调度器已初始化 | max_concurrent={max_concurrent}"
        )

    def _get_max_concurrent_from_settings(self, settings, provider: str) -> int:
        """从配置中获取提供商的并发限制"""
        provider_concurrency_map = {
            "zhipu": getattr(settings, "llm_zhipu_max_concurrent", 2),
            "openai": getattr(settings, "llm_openai_max_concurrent", 10),
            "anthropic": getattr(settings, "llm_anthropic_max_concurrent", 5),
            "default": getattr(settings, "llm_default_max_concurrent", 2),
        }
        return provider_concurrency_map.get(provider, 2)

    async def execute(
        self,
        func: Callable,
        *args,
        **kwargs,
    ) -> Any:
        """
        执行 LLM 请求（带并发控制）

        Args:
            func: 要执行的异步函数
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            函数执行结果
        """
        async with self._semaphore:
            # 简单的速率限制检查
            current_time = asyncio.get_event_loop().time()
            if current_time - self._last_reset >= 60:
                self._request_count = 0
                self._last_reset = current_time

            if self._request_count >= self.rate_limit_per_minute:
                # 等待到下一分钟
                wait_time = 60 - (current_time - self._last_reset)
                if wait_time > 0:
                    logger.warning(
                        f"[LLM 调度器] 达到速率限制 | provider={self.provider} | "
                        f"等待 {wait_time:.1f} 秒"
                    )
                    await asyncio.sleep(wait_time)
                    self._request_count = 0
                    self._last_reset = asyncio.get_event_loop().time()

            self._request_count += 1
            return await func(*args, **kwargs)

    async def call_with_semaphore(
        self,
        provider: str,
        func: Callable,
        *args,
        **kwargs,
    ) -> Any:
        """
        使用信号量执行 LLM 请求（兼容旧接口）

        Args:
            provider: 提供商名称（用于日志）
            func: 要执行的异步函数
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            函数执行结果
        """
        logger.debug(f"[LLM 调度器] 执行请求 | provider={provider}")
        return await self.execute(func, *args, **kwargs)

    async def acquire(self, provider: str = "default") -> None:
        """
        获取信号量（用于流式请求）

        Args:
            provider: 提供商名称（用于日志）
        """
        logger.debug(f"[LLM 调度器] 获取信号量 | provider={provider}")
        await self._semaphore.acquire()

    async def release(self, provider: str = "default") -> None:
        """
        释放信号量（用于流式请求）

        Args:
            provider: 提供商名称（用于日志）
        """
        logger.debug(f"[LLM 调度器] 释放信号量 | provider={provider}")
        self._semaphore.release()


# 全局调度器实例（按提供商分组）
_schedulers: dict[str, LLMScheduler] = {}


def get_llm_scheduler(provider: str = "default") -> LLMScheduler:
    """
    获取 LLM 调度器实例

    按提供商返回对应的调度器实例
    每个提供商有独立的信号量和速率限制

    Args:
        provider: 提供商名称 (zhipu, openai, anthropic, default)

    Returns:
        LLMScheduler 实例
    """
    if provider not in _schedulers:
        _schedulers[provider] = LLMScheduler(provider=provider)
    return _schedulers[provider]


def reset_scheduler(provider: str | None = None) -> None:
    """
    重置调度器

    Args:
        provider: 提供商名称，None 则重置所有
    """
    global _schedulers
    if provider:
        if provider in _schedulers:
            del _schedulers[provider]
    else:
        _schedulers = {}
