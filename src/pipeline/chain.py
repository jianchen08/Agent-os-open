"""插件执行链。

按优先级排序顺序执行插件列表，
支持错误策略处理（ABORT/SKIP）。
"""

from __future__ import annotations

import logging
import time

from pipeline.plugin import (
    IPlugin,
    PluginContext,
    PluginResult,
)
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


def _deep_update(target: dict, updates: dict) -> None:
    """将 updates 合并到 target，展开点号键为嵌套字典结构。

    插件的 state_updates 中使用点号键（如 "security.decision"），
    但条件解析器按嵌套字典访问（state["security"]["decision"]）。
    此函数将 "security.decision" 展开为 target["security"]["decision"]，
    使两种访问方式都能正确工作。

    展开为嵌套结构的同时保留顶层点号键（如 target["security.decision"]），
    确保 state.get("security.decision") 也能正确访问（否则只展开为嵌套结构时
    state.get("prompt.dynamic_vars") 等调用会返回 None）。

    Args:
        target: 目标 state 字典（原地修改）
        updates: 插件返回的 state_updates 字典
    """
    for key, value in updates.items():
        if "." in key:
            # 展开点号键：security.decision → target["security"]["decision"]
            parts = key.split(".")
            current = target
            for part in parts[:-1]:
                if part not in current or not isinstance(current[part], dict):
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = value
            # 同时保留顶层点号键，确保 state.get("xxx.yyy") 也能访问
            target[key] = value
        else:
            target[key] = value


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

            # 合并状态更新（展开点号键为嵌套字典结构）
            if result.state_updates:
                _deep_update(ctx.state, result.state_updates)

            # 跳过剩余插件
            if result.skip_remaining:
                logger.debug(
                    "[%s] skip_remaining=True, skipping remaining plugins",
                    plugin.name,
                )
                break

        return results

    async def _execute_plugin(self, plugin: IPlugin, ctx: PluginContext) -> PluginResult:
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
        logger.debug("[%s] started", plugin.name)

        try:
            raw_result = await plugin.execute(ctx)
            elapsed = time.monotonic() - start
            logger.debug("[%s] success (%.3fs)", plugin.name, elapsed)

            # ICorePlugin 返回 dict，需要包装为 PluginResult
            if isinstance(raw_result, dict):
                return PluginResult(state_updates=raw_result)
            return raw_result

        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.error("[%s] error (%.3fs): %s", plugin.name, elapsed, exc)
            return await self._handle_error(plugin, ctx, exc)

    async def _handle_error(  # noqa: PLR0911
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

        if policy == ErrorPolicy.FALLBACK:
            fallback = getattr(plugin, "fallback_state", None)
            logger.info("[%s] FALLBACK: using fallback_state=%s", plugin.name, fallback)
            if isinstance(fallback, dict):
                return PluginResult(state_updates=fallback)
            return PluginResult()

        if policy == ErrorPolicy.RETRY:
            max_retries = getattr(plugin, "max_retries", 3)
            for attempt in range(1, max_retries + 1):
                logger.info("[%s] RETRY attempt %d/%d", plugin.name, attempt, max_retries)
                try:
                    raw_result = await plugin.execute(ctx)
                    logger.info("[%s] retry success on attempt %d", plugin.name, attempt)
                    if isinstance(raw_result, dict):
                        return PluginResult(state_updates=raw_result)
                    return raw_result
                except Exception as retry_exc:
                    logger.warning("[%s] retry %d failed: %s", plugin.name, attempt, retry_exc)
            logger.error("[%s] RETRY exhausted after %d attempts", plugin.name, max_retries)
            return PluginResult(error=exc, skip_remaining=True)

        # 未知策略，默认 ABORT
        logger.error("[%s] Unknown error policy %s, defaulting to ABORT: %s", plugin.name, policy, exc)
        return PluginResult(error=exc, skip_remaining=True)
