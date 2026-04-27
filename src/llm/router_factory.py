"""litellm.Router 工厂 — 从 llm.yaml 构建共享 Router 实例。

从 ModelConfigLoader 读取所有模型定义，构建 litellm.Router，
配合 AdaptiveRouterAdapter 提供自适应并发控制和 fallback 能力。
"""

from __future__ import annotations

import logging
from typing import Any

import litellm

logger = logging.getLogger(__name__)

# provider 名称 → litellm 前缀（与 llm_core.py 保持一致）
_PROVIDER_MAP: dict[str, str] = {
    "openai": "openai",
    "minimax": "minimax",
    "anthropic": "anthropic",
    "azure": "azure",
    "zhipu_coding": "zai",
    "zhipu": "zai",
}

# 模块级单例缓存
_router_instance: litellm.Router | None = None
_adapter_instance: Any = None


def _get_litellm_model_string(provider: str, model_name: str) -> str:
    """计算 litellm 格式的模型标识字符串。

    Args:
        provider: 提供商名称（如 zhipu_coding）
        model_name: 模型名称（如 glm-5.1）

    Returns:
        litellm 模型字符串（如 "zai/glm-5.1"）
    """
    prefix = _PROVIDER_MAP.get(provider, provider)
    return f"{prefix}/{model_name}"


def build_model_list(model_loader: Any) -> list[dict[str, Any]]:
    """从 llm.yaml 构建 Router model_list。

    Args:
        model_loader: ModelConfigLoader 实例

    Returns:
        litellm.Router model_list 格式的列表
    """
    llm_data = model_loader._load_llm_data()
    models_section = llm_data.get("models", {})

    model_list: list[dict[str, Any]] = []

    for model_id, model_conf in models_section.items():
        provider = model_conf.get("provider", "")
        model_name = model_conf.get("model_name", model_id)

        litellm_params: dict[str, Any] = {
            "model": _get_litellm_model_string(provider, model_name),
        }

        # api_key / api_base：先从模型配置取，再从提供商配置回退
        provider_conf = model_loader.get_provider_config(provider) or {}
        api_key = model_conf.get("api_key", "") or provider_conf.get("api_key", "")
        api_base = model_conf.get("api_base", "") or provider_conf.get("api_base", "")
        if api_key:
            litellm_params["api_key"] = api_key
        if api_base:
            litellm_params["api_base"] = api_base

        # 不在 model_list 设 max_parallel_requests — 由 AdaptiveRouterAdapter 管理

        model_list.append({
            "model_name": model_id,
            "litellm_params": litellm_params,
        })

        logger.info(
            "[Router] 注册模型: %s → %s",
            model_id, litellm_params["model"],
        )

    return model_list


def build_fallbacks(model_loader: Any) -> list[dict[str, Any]]:
    """从 llm.yaml 的 defaults.fallback_chain 构建 Router fallbacks。

    llm.yaml 格式::

        defaults:
          fallback_chain:
            chat: [deepseek-chat, glm-4.7]

    Router 格式::

        fallbacks=[{"glm-5.1": ["deepseek-chat", "glm-4.7"]}]

    Args:
        model_loader: ModelConfigLoader 实例

    Returns:
        Router fallbacks 列表
    """
    llm_data = model_loader._load_llm_data()
    defaults = llm_data.get("defaults", {})
    fallback_chain = defaults.get("fallback_chain", {})

    # 从 fallback_chain 的 key 推导出默认模型 ID
    # chat → defaults.chat, reasoning → defaults.reasoning
    # 合并同 primary 的 fallback 链（去重）
    merged: dict[str, list[str]] = {}
    for model_type, fallback_ids in fallback_chain.items():
        primary_id = defaults.get(model_type, "")
        if primary_id and fallback_ids:
            existing = merged.get(primary_id, [])
            for fid in fallback_ids:
                if fid not in existing:
                    existing.append(fid)
            merged[primary_id] = existing

    fallbacks: list[dict[str, Any]] = []
    for primary_id, fb_ids in merged.items():
        fallbacks.append({primary_id: fb_ids})
        logger.info(
            "[Router] fallback 链: %s → %s", primary_id, fb_ids,
        )

    return fallbacks


def build_router(model_loader: Any) -> litellm.Router:
    """构建 litellm.Router 实例。

    Args:
        model_loader: ModelConfigLoader 实例

    Returns:
        配置好的 litellm.Router
    """
    llm_data = model_loader._load_llm_data()
    defaults = llm_data.get("defaults", {})

    model_list = build_model_list(model_loader)
    fallbacks = build_fallbacks(model_loader)

    call_timeout = defaults.get("call_timeout", 600)

    router_kwargs: dict[str, Any] = {
        "model_list": model_list,
        # Router 自身不再管并发 — 由 AdaptiveRouterAdapter 管理
        "default_max_parallel_requests": 10,
        "num_retries": 2,
        "allowed_fails": 2,
        "cooldown_time": 90,
        # stream_timeout: Router 内置流式超时，作为最终兜底
        "stream_timeout": call_timeout,
        "timeout": call_timeout,
    }

    if fallbacks:
        router_kwargs["fallbacks"] = fallbacks

    router = litellm.Router(**router_kwargs)

    logger.info(
        "[Router] 创建完成: %d 个模型, fallbacks=%d, stream_timeout=%ds",
        len(model_list), len(fallbacks), call_timeout,
    )

    return router


def build_adapter(model_loader: Any) -> Any:
    """构建带自适应并发的 AdaptiveRouterAdapter。

    从 llm.yaml concurrency 段读取 min/max/default 配置。

    Args:
        model_loader: ModelConfigLoader 实例

    Returns:
        AdaptiveRouterAdapter 实例
    """
    from llm.adapter import AdaptiveRouterAdapter

    llm_data = model_loader._load_llm_data()
    concurrency_section = llm_data.get("concurrency", {})

    min_cap = concurrency_section.get("min_concurrency", 1)
    max_cap = concurrency_section.get("max_concurrency", 3)
    default_cap = concurrency_section.get("default_concurrency", 2)

    router = get_or_create_router(model_loader)

    adapter = AdaptiveRouterAdapter(
        router,
        min_capacity=min_cap,
        max_capacity=max_cap,
        default_capacity=default_cap,
    )

    logger.info(
        "[Router] AdaptiveRouterAdapter: concurrency %d (min=%d, max=%d)",
        default_cap, min_cap, max_cap,
    )

    return adapter


def get_or_create_router(model_loader: Any) -> litellm.Router:
    """获取或创建共享的 Router 单例。

    Args:
        model_loader: ModelConfigLoader 实例

    Returns:
        共享的 litellm.Router 实例
    """
    global _router_instance
    if _router_instance is None:
        _router_instance = build_router(model_loader)
    return _router_instance


def get_or_create_adapter(model_loader: Any) -> Any:
    """获取或创建共享的 AdaptiveRouterAdapter 单例。

    Args:
        model_loader: ModelConfigLoader 实例

    Returns:
        AdaptiveRouterAdapter 实例
    """
    global _adapter_instance
    if _adapter_instance is None:
        _adapter_instance = build_adapter(model_loader)
    return _adapter_instance


def reset_router() -> None:
    """重置 Router 单例（仅用于测试）。"""
    global _router_instance, _adapter_instance
    _router_instance = None
    _adapter_instance = None
