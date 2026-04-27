"""LLM Adapter 中间层模块。

提供统一的 LLM 调用抽象，支持多模型 fallback 和 litellm.Router 并发控制。
"""

from llm.adapter import (
    AdaptiveRouterAdapter,
    FallbackAdapter,
    LiteLLMAdapter,
    LLMAdapter,
    LLMResponse,
    RouterAdapter,
)
from llm.router_factory import build_router, get_or_create_router, reset_router

__all__ = [
    "AdaptiveRouterAdapter",
    "FallbackAdapter",
    "LiteLLMAdapter",
    "LLMAdapter",
    "LLMResponse",
    "RouterAdapter",
    "build_router",
    "get_or_create_router",
    "reset_router",
]
