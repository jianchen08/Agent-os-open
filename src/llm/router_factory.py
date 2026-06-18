"""litellm.Router 工厂 — 从 llm.yaml 构建共享 Router 实例。

多 key 场景下，为同一 model_name 注册多个 deployment（每个 key 一个），
litellm Router 自动负载均衡、冷却和 failover。
"""

from __future__ import annotations

import logging
from typing import Any

import litellm

from llm.key_pool import KeyPool, KeySlot

logger = logging.getLogger(__name__)

# provider 名称 → litellm 前缀
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
_key_pools: dict[str, KeyPool] = {}
# model_id → provider 映射（由 build_router 填充）
_model_to_provider: dict[str, str] = {}


def _get_litellm_model_string(provider: str, model_name: str) -> str:
    """计算 litellm 格式的模型标识字符串。"""
    prefix = _PROVIDER_MAP.get(provider, provider)
    return f"{prefix}/{model_name}"


def _parse_provider_keys(
    llm_data: dict[str, Any],
) -> dict[str, list[KeySlot]]:
    """从 llm.yaml 的 providers 段解析所有 key。

    支持两种配置格式：

    多 key（新）::

        providers:
          zhipu_coding:
            api_base: https://...
            keys:
              - id: key1
                api_key: xxxx
                rpm: 60
              - id: key2
                api_key: yyyy
                rpm: 60

    单 key（旧）::

        providers:
          zhipu_coding:
            api_key: xxxx
            api_base: https://...

    Returns:
        provider_name → [KeySlot, ...] 的映射
    """
    providers_section = llm_data.get("providers", {})

    result: dict[str, list[KeySlot]] = {}

    for provider_name, provider_conf in providers_section.items():
        if not isinstance(provider_conf, dict):
            continue

        api_base = provider_conf.get("api_base", "")
        keys_conf = provider_conf.get("keys", [])
        slots: list[KeySlot] = []

        if keys_conf and isinstance(keys_conf, list):
            for i, key_conf in enumerate(keys_conf):
                if not isinstance(key_conf, dict):
                    continue
                slots.append(KeySlot(
                    key_id=key_conf.get("id", f"{provider_name}_{i}"),
                    api_key=key_conf.get("api_key", ""),
                    api_base=key_conf.get("api_base", "") or api_base,
                    max_concurrent=key_conf.get("max_concurrent", 2),
                    rpm_limit=key_conf.get("rpm", 0),
                    token_quota=key_conf.get("token_quota", 0),
                ))
        else:
            api_key = provider_conf.get("api_key", "")
            if api_key:
                slots.append(KeySlot(
                    key_id=f"{provider_name}_default",
                    api_key=api_key,
                    api_base=api_base,
                ))

        if slots:
            result[provider_name] = slots
            logger.info(
                "[Router] provider %s: %d key(s)",
                provider_name, len(slots),
            )

    return result


def build_model_list(
    model_loader: Any,
    provider_keys: dict[str, list[KeySlot]],
) -> list[dict[str, Any]]:
    """从 llm.yaml 构建 Router model_list。

    多 key provider: 为每个 key 注册一个 deployment（同一 model_name）。
    litellm Router 自动负载均衡和 failover。
    """
    llm_data = model_loader._load_llm_data()
    models_section = llm_data.get("models", {})

    model_list: list[dict[str, Any]] = []

    for model_id, model_conf in models_section.items():
        # 跳过 embedding 模型：litellm Router 不支持自定义前缀的 embedding
        if model_conf.get("dimension") or "embedding" in model_id:
            continue

        provider = model_conf.get("provider", "")
        model_name = model_conf.get("model_name", model_id)

        litellm_model = _get_litellm_model_string(provider, model_name)
        slots = provider_keys.get(provider)

        # 模型级凭证覆盖（如 deepseek-chat 有自己的 api_key）
        model_api_key = model_conf.get("api_key", "")
        model_api_base = model_conf.get("api_base", "")

        if slots and not model_api_key:
            # 多 key：每个 slot 注册一个 deployment
            for slot in slots:
                lp: dict[str, Any] = {"model": litellm_model}
                lp["api_key"] = slot.api_key
                lp["api_base"] = model_api_base or slot.api_base or ""
                if not lp["api_base"]:
                    del lp["api_base"]

                model_list.append({
                    "model_name": model_id,
                    "litellm_params": lp,
                })
                logger.info(
                    "[Router] deployment: %s → %s (key=%s)",
                    model_id, litellm_model, slot.key_id,
                )
        else:
            # 单 key：直接用模型级或 provider 级的凭证
            lp: dict[str, Any] = {"model": litellm_model}
            if model_api_key:
                lp["api_key"] = model_api_key
            if model_api_base:
                lp["api_base"] = model_api_base
            elif slots and slots[0].api_base:
                lp["api_base"] = slots[0].api_base

            model_list.append({
                "model_name": model_id,
                "litellm_params": lp,
            })
            logger.info(
                "[Router] deployment: %s → %s",
                model_id, litellm_model,
            )

    return model_list


def build_fallbacks(model_loader: Any) -> list[dict[str, Any]]:
    """从 llm.yaml 的 defaults.fallback_chain 构建 Router fallbacks。"""
    llm_data = model_loader._load_llm_data()
    defaults = llm_data.get("defaults", {})
    fallback_chain = defaults.get("fallback_chain", {})

    merged: dict[str, list[str]] = {}
    for model_type, fallback_ids in fallback_chain.items():
        primary_id = defaults.get(model_type, "")
        if primary_id and fallback_ids:
            existing = merged.setdefault(primary_id, [])
            for fid in fallback_ids:
                if fid not in existing:
                    existing.append(fid)

    fallbacks: list[dict[str, Any]] = []
    for primary_id, fb_ids in merged.items():
        fallbacks.append({primary_id: fb_ids})
        logger.info("[Router] fallback: %s → %s", primary_id, fb_ids)

    return fallbacks


def build_router(model_loader: Any) -> litellm.Router:
    """构建 litellm.Router 实例。"""
    global _key_pools, _model_to_provider

    llm_data = model_loader._load_llm_data()
    defaults = llm_data.get("defaults", {})
    call_timeout = defaults.get("call_timeout", 600)

    provider_keys = _parse_provider_keys(llm_data)
    model_list = build_model_list(model_loader, provider_keys)
    fallbacks = build_fallbacks(model_loader)

    # 构建 model_id → provider 映射
    _model_to_provider.clear()
    for model_id, model_conf in llm_data.get("models", {}).items():
        provider = model_conf.get("provider", "")
        if provider:
            _model_to_provider[model_id] = provider

    # 构建 KeyPools（仅用于统计展示）
    for prov_name, slots in provider_keys.items():
        _key_pools[prov_name] = KeyPool(slots, pool_id=prov_name)

    router_kwargs: dict[str, Any] = {
        "model_list": model_list,
        "num_retries": 1,
        "allowed_fails": 2,
        "cooldown_time": 120,
        "retry_after": 5,
        "stream_timeout": call_timeout,
        "timeout": call_timeout,
    }
    if fallbacks:
        router_kwargs["fallbacks"] = fallbacks

    router = litellm.Router(**router_kwargs)

    logger.info(
        "[Router] 创建完成: %d deployments, fallbacks=%d",
        len(model_list), len(fallbacks),
    )
    return router


def get_key_pool(provider_name: str) -> KeyPool | None:
    """获取指定 provider 的 KeyPool（仅统计用）。"""
    return _key_pools.get(provider_name)


def get_provider_for_model(model_id: str) -> str:
    """根据 model_id 查找 provider 名称（如 glm-5.1 → zhipu_coding）。"""
    return _model_to_provider.get(model_id, "")


def build_adapter(model_loader: Any) -> Any:
    """构建 KeyPoolAdapter — 按 key 粒度并发控制 + RPM 限流 + 配额追踪。"""
    from llm.adapter import KeyPoolAdapter

    llm_data = model_loader._load_llm_data()
    concurrency_section = llm_data.get("concurrency", {})
    default_max_concurrent = concurrency_section.get("default_max_concurrent", 2)

    router = get_or_create_router(model_loader)

    adapter = KeyPoolAdapter(
        router,
        default_max_concurrent=default_max_concurrent,
    )

    logger.info(
        "[Router] KeyPoolAdapter: default_max_concurrent=%d, key_pools=%s",
        default_max_concurrent, list(_key_pools.keys()),
    )
    return adapter


def get_or_create_router(model_loader: Any) -> litellm.Router:
    """获取或创建共享的 Router 单例。"""
    global _router_instance
    if _router_instance is None:
        _router_instance = build_router(model_loader)
    return _router_instance


def get_or_create_adapter(model_loader: Any) -> Any:
    """获取或创建共享的 Adapter 单例。"""
    global _adapter_instance
    if _adapter_instance is None:
        _adapter_instance = build_adapter(model_loader)
    return _adapter_instance



def reset_router() -> None:
    """重置 Router/Adapter 模块级单例，使配置变更后重新构建。

    BUG-FIX-20260617: router_factory 在 REFACTOR-20260614 中删除了 reset_router，
    但 config.models.invalidate_all_llm_caches() 仍通过延迟导入调用它，导致
    LLM 配置热重载时抛出 ImportError（配置实际已保存成功）。
    修复方案：恢复 reset_router，仅清除本模块的模块级单例，让下次调用
    get_or_create_router / get_or_create_adapter 时按新配置重新构建。
    """
    global _router_instance, _adapter_instance
    _router_instance = None
    _adapter_instance = None
    _key_pools.clear()
    _model_to_provider.clear()
    logger.info("[Router] 模块级单例已重置")

