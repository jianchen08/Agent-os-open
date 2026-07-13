"""Provider 适配器工厂。

按 model 字符串（litellm 前缀，如 "deepseek/deepseek-v4-pro"）选择
对应的 ProviderAdapter 实例。

加新 provider：
    1. 新建 src/llm/provider_adapters/<provider>.py 实现 ProviderAdapter 子类
    2. 在本文件的 _REGISTRY 注册一行（provider 前缀 → adapter 实例）
    3. 不需要改任何其他文件
"""

from __future__ import annotations

from typing import Any

from .base import ProviderAdapter
from .deepseek import DeepSeekAdapter
from .minimax import MiniMaxAdapter

__all__ = ["ProviderAdapter", "DeepSeekAdapter", "MiniMaxAdapter", "get_provider_adapter"]


# 复用单例（adapter 无状态，无需每次创建）
_DEFAULT_ADAPTER = ProviderAdapter()
_DEEPSEEK_ADAPTER = DeepSeekAdapter()
_MINIMAX_ADAPTER = MiniMaxAdapter()

# provider 前缀 → adapter 映射（按 model 字符串子串匹配）
_REGISTRY: dict[str, ProviderAdapter] = {
    "deepseek": _DEEPSEEK_ADAPTER,
    "minimax": _MINIMAX_ADAPTER,
}


def get_provider_adapter(model: str) -> ProviderAdapter:
    """根据 model 字符串返回对应的 ProviderAdapter。

    匹配规则：model 字符串中包含 provider 前缀即命中。
    未命中返回默认 adapter（剥离 reasoning_content）。

    Args:
        model: litellm 模型标识（如 "deepseek/deepseek-v4-pro"、
               "minimax/MiniMax-M3"、"zai/glm-5.2"）

    Returns:
        对应的 ProviderAdapter 实例
    """
    if not model:
        return _DEFAULT_ADAPTER

    model_lower = model.lower()
    for provider_prefix, adapter in _REGISTRY.items():
        if provider_prefix in model_lower:
            return adapter
    return _DEFAULT_ADAPTER
