"""媒体 Provider Fallback 链。

暴露接口：
- FallbackStrategy：Fallback 策略枚举（sequential/random/weighted）
- ProviderChain：按优先级顺序尝试 Provider 的 Fallback 链
"""

from __future__ import annotations

import logging
import random
from enum import Enum
from typing import Any

from tools.media.base import MediaProvider, MediaResult

logger = logging.getLogger(__name__)


class FallbackStrategy(str, Enum):
    """Fallback 策略枚举。"""

    SEQUENTIAL = "sequential"  # 按优先级顺序
    RANDOM = "random"  # 随机选择
    WEIGHTED = "weighted"  # 按权重选择


class ProviderChain:
    """按优先级顺序尝试 Provider 的 Fallback 链。

    主 Provider 失败时自动切换到备用 Provider，
    并记录每次 fallback 触发日志。

    Attributes:
        providers: Provider 列表（已按优先级排序）
        strategy: Fallback 策略
    """

    def __init__(
        self,
        providers: list[MediaProvider],
        strategy: FallbackStrategy = FallbackStrategy.SEQUENTIAL,
    ) -> None:
        """初始化 Fallback 链。

        Args:
            providers: Provider 列表（会按 priority 排序）
            strategy: Fallback 策略
        """
        self._providers = sorted(providers, key=lambda p: p.config.priority)
        self._strategy = strategy

    @property
    def providers(self) -> list[MediaProvider]:
        """获取排序后的 Provider 列表。"""
        return self._providers

    @property
    def strategy(self) -> FallbackStrategy:
        """获取 Fallback 策略。"""
        return self._strategy

    def _order_providers(self) -> list[MediaProvider]:
        """根据策略对 Provider 排序。

        Returns:
            排序后的 Provider 列表
        """
        if self._strategy == FallbackStrategy.RANDOM:
            return random.sample(self._providers, len(self._providers))
        if self._strategy == FallbackStrategy.WEIGHTED:
            # 权重 = 1/priority（priority 越小权重越大）
            return list(self._providers)  # 简化：已按 priority 排序
        return self._providers

    async def execute_synthesize(self, text: str, **kwargs: Any) -> MediaResult:
        """按 Fallback 策略执行 synthesize。

        依次尝试每个 Provider，跳过不可用的 Provider，
        直到成功或全部失败。

        Args:
            text: 要合成的文本
            **kwargs: Provider 特有参数

        Returns:
            MediaResult 统一返回格式

        Raises:
            RuntimeError: 所有 Provider 均失败或无可用 Provider
        """
        if not self._providers:
            raise RuntimeError("没有可用的 Provider")

        errors: list[str] = []
        ordered = self._order_providers()

        for provider in ordered:
            # 检查可用性
            try:
                available = await provider.is_available()
            except Exception as e:
                logger.warning(
                    "[Fallback] Provider '%s' 可用性检查异常: %s",
                    provider.provider_name,
                    e,
                )
                errors.append(f"{provider.provider_name}: unavailable ({e})")
                continue

            if not available:
                logger.debug(
                    "[Fallback] Provider '%s' 不可用，跳过",
                    provider.provider_name,
                )
                errors.append(f"{provider.provider_name}: unavailable")
                continue

            # 尝试执行
            try:
                result = await provider.synthesize(text, **kwargs)
                logger.info(
                    "[Fallback] Provider '%s' synthesize 成功",
                    provider.provider_name,
                )
                return result
            except Exception as e:
                logger.warning(
                    "[Fallback] Provider '%s' synthesize 失败: %s，尝试下一个",
                    provider.provider_name,
                    e,
                )
                errors.append(f"{provider.provider_name}: {e}")

        raise RuntimeError(f"所有 Provider 均失败: {'; '.join(errors)}")

    async def execute_generate(self, prompt: str, **kwargs: Any) -> MediaResult:
        """按 Fallback 策略执行 generate。

        依次尝试每个 Provider，跳过不可用的 Provider，
        直到成功或全部失败。

        Args:
            prompt: 生成提示词
            **kwargs: Provider 特有参数

        Returns:
            MediaResult 统一返回格式

        Raises:
            RuntimeError: 所有 Provider 均失败或无可用 Provider
        """
        if not self._providers:
            raise RuntimeError("没有可用的 Provider")

        errors: list[str] = []
        ordered = self._order_providers()

        for provider in ordered:
            # 检查可用性
            try:
                available = await provider.is_available()
            except Exception as e:
                logger.warning(
                    "[Fallback] Provider '%s' 可用性检查异常: %s",
                    provider.provider_name,
                    e,
                )
                errors.append(f"{provider.provider_name}: unavailable ({e})")
                continue

            if not available:
                logger.debug(
                    "[Fallback] Provider '%s' 不可用，跳过",
                    provider.provider_name,
                )
                errors.append(f"{provider.provider_name}: unavailable")
                continue

            # 尝试执行
            try:
                result = await provider.generate(prompt, **kwargs)
                logger.info(
                    "[Fallback] Provider '%s' generate 成功",
                    provider.provider_name,
                )
                return result
            except Exception as e:
                logger.warning(
                    "[Fallback] Provider '%s' generate 失败: %s，尝试下一个",
                    provider.provider_name,
                    e,
                )
                errors.append(f"{provider.provider_name}: {e}")

        raise RuntimeError(f"所有 Provider 均失败: {'; '.join(errors)}")
