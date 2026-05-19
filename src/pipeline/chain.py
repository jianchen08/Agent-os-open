"""插件执行链。

按优先级排序顺序执行插件列表，
支持错误策略处理（ABORT/SKIP/RETRY/FALLBACK）和跳过后续逻辑。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from pipeline.plugin import (
    IPlugin,
    PluginContext,
    PluginResult,
)
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


class PluginChain:
    """插件执行链。

    按优先级排序（数值小的先执行）顺序执行插件列表，
    每次执行后更新上下文状态。支持 skip_remaining 提前终止
    以及四种错误策略处理。

    Attributes:
        plugins: 待执行的插件列表
    """

    def __init__(self, plugins: list[IPlugin]) -> None:
        self.plugins = sorted(plugins, key=lambda p: p.priority)

    async def execute(self, ctx: PluginContext) -> list[PluginResult]:
        """顺序执行所有插件。

        按优先级排序后依次执行，每次执行后将 state_updates 合并到
        上下文状态中。若某插件返回 skip_remaining=True，
        则跳过后续所有插件。

        Args:
            ctx: 插件执行上下文

        Returns:
            所有已执行插件的执行结果列表
        """
        results: list[PluginResult] = []

        for plugin in self.plugins:
            result = await self._execute_plugin(plugin, ctx)
            results.append(result)

            # 合并状态更新
            if result.state_updates:
                ctx.state.update(result.state_updates)

            # 跳过剩余插件
            if result.skip_remaining:
                logger.info(
                    "[%s] skip_remaining=True, skipping remaining plugins",
                    plugin.name,
                )
                break

        return results

    async def _execute_plugin(
        self, plugin: IPlugin, ctx: PluginContext
    ) -> PluginResult:
        """执行单个插件，内建错误策略处理。

        - ABORT: 记录错误，返回 skip_remaining=True
        - SKIP: 记录警告，返回空 PluginResult
        - RETRY: 指数退避重试（max_retries=3, base_delay=1.0）
        - FALLBACK: 使用 plugin.fallback_state 作为 state_updates

        Args:
            plugin: 待执行的插件实例
            ctx: 插件执行上下文

        Returns:
            插件执行结果
        """
        start = time.monotonic()
        logger.info("[%s] started", plugin.name)

        try:
            raw_result = await plugin.execute(ctx)
            elapsed = time.monotonic() - start
            logger.info("[%s] success (%.3fs)", plugin.name, elapsed)

            # ICorePlugin 返回 dict，需要包装为 PluginResult
            if isinstance(raw_result, dict):
                return PluginResult(state_updates=raw_result)
            return raw_result

        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.error("[%s] error (%.3fs): %s", plugin.name, elapsed, exc)
            return await self._handle_error(plugin, ctx, exc)

    async def _handle_error(
        self, plugin: IPlugin, ctx: PluginContext, exc: Exception
    ) -> PluginResult:
        """根据插件错误策略处理异常。

        Args:
            plugin: 发生错误的插件
            ctx: 插件执行上下文
            exc: 捕获的异常

        Returns:
            错误处理后的插件结果
        """
        policy = plugin.error_policy

        if policy == ErrorPolicy.ABORT:
            logger.error("[%s] ABORT: %s", plugin.name, exc)
            return PluginResult(error=exc, skip_remaining=True)

        if policy == ErrorPolicy.SKIP:
            logger.warning("[%s] SKIP: %s", plugin.name, exc)
            return PluginResult()

        if policy == ErrorPolicy.RETRY:
            # BUG-FIX: 从插件实例读取重试参数，避免硬编码导致无法按场景调优
            max_retries = getattr(plugin, "max_retries", 3)
            base_delay = getattr(plugin, "retry_delay", 1.0)
            for attempt in range(1, max_retries + 1):
                delay = base_delay * (2 ** (attempt - 1)) * (0.5 + random.random() * 0.5)
                logger.warning(
                    "[%s] RETRY attempt %d/%d (delay=%.1fs): %s",
                    plugin.name, attempt, max_retries, delay, exc,
                )
                await asyncio.sleep(delay)
                try:
                    raw_result = await plugin.execute(ctx)
                    logger.info("[%s] RETRY succeeded on attempt %d", plugin.name, attempt)
                    if isinstance(raw_result, dict):
                        return PluginResult(state_updates=raw_result)
                    return raw_result
                except Exception as retry_exc:
                    exc = retry_exc
                    logger.warning("[%s] RETRY attempt %d failed: %s", plugin.name, attempt, retry_exc)
            logger.error("[%s] RETRY exhausted after %d attempts: %s", plugin.name, max_retries, exc)
            return PluginResult(error=exc)

        if policy == ErrorPolicy.FALLBACK:
            fallback = getattr(plugin, "fallback_state", {})
            logger.warning("[%s] FALLBACK: using fallback_state: %s", plugin.name, exc)
            return PluginResult(state_updates=dict(fallback))

        # 未知策略，默认 ABORT
        logger.error("[%s] Unknown error policy %s, defaulting to ABORT: %s", plugin.name, policy, exc)
        return PluginResult(error=exc, skip_remaining=True)
