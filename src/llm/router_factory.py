"""litellm.Router 工厂 — 从 llm.yaml 构建共享 Router 实例。

多 key 场景下，为同一 model_name 注册多个 deployment（每个 key 一个），
litellm Router 自动负载均衡、冷却和 failover。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import litellm

from llm.key_pool import KeyPool, KeySlot

logger = logging.getLogger(__name__)

# LLM 出口要直连的域名白名单（用于 NO_PROXY 兜底，确保即使开了 trust_env 也不走代理）。
# 与 llm.yaml 中各 provider 的 api_base 对应。
_LLM_DIRECT_HOSTS = (
    "open.bigmodel.cn",
    "api.deepseek.com",
    "api.minimaxi.com",
    "cn.apigocn.top",
    "ai.1cc.ai",
)

# 进程内代理环境变量名（httpx / aiohttp / requests 三家都读这些）。
_PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


def disable_llm_proxy() -> None:
    """强制 LLM 调用直连，与本机系统代理解耦。

    背景：宿主机代理客户端（Clash/V2Ray 等）会向 Windows 注册表写入
    系统代理（如 127.0.0.1:7993）。litellm 底层的 httpx 默认 trust_env=True，
    会读 HTTP(S)_PROXY 环境变量；aiohttp 在 aiohttp_trust_env=True 时同样读取。
    结果是 LLM 请求被转发到本地代理端口——代理开着时 TLS 链路被破坏
    (报 api.xxx:443 ssl)，代理关掉时端口无人监听 (报 connection refused)，
    两种现象随代理开关反复横跳。

    本函数在 Router 首次构建时执行一次，从两个层面钉死直连：
    1. 清空进程内 *_PROXY 环境变量 → httpx/aiohttp/requests 均不再走代理；
    2. 设置 litellm.aiohttp_trust_env=False → 即便环境变量被外部恢复，
       aiohttp 也不读系统代理；
    3. 用 NO_PROXY 列出 LLM 域名作为兜底 → 即使上层强制开启代理，
       这些域名仍直连。

    该函数幂等，可安全重复调用。
    """
    # 1. 清空进程内代理环境变量（httpx/aiohttp/requests 三家共用）
    for var in _PROXY_ENV_VARS:
        os.environ.pop(var, None)

    # 2. 关闭 litellm 的 aiohttp trust_env（默认已是 False，显式设以防被外部改动）
    litellm.aiohttp_trust_env = False
    # 双保险：litellm 提供的禁用开关
    litellm.disable_aiohttp_trust_env = True

    # 3. NO_PROXY 兜底：列出所有 LLM 直连域名（NO_PROXY 支持逗号分隔列表）
    existing_no_proxy = os.environ.get("NO_PROXY", "")
    hosts_to_add = [h for h in _LLM_DIRECT_HOSTS if h not in existing_no_proxy]
    if hosts_to_add:
        merged = ",".join(filter(None, [existing_no_proxy, *hosts_to_add]))
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged

    logger.info(
        "[Router] LLM 出口已强制直连 (proxy env cleared, aiohttp_trust_env=False, "
        "NO_PROXY=%s)", os.environ.get("NO_PROXY"),
    )

# 模块级单例缓存
_router_instance: litellm.Router | None = None
_adapter_instance: Any = None
_key_pools: dict[str, KeyPool] = {}
# model_id → provider 映射（由 build_router 填充）
_model_to_provider: dict[str, str] = {}
# model_id → model_name 映射（由 build_router 填充）
# KeyPoolAdapter 直连时用 model_id 路由到正确 provider，再反查 model_name 拼成
# litellm model 字符串发给上游（model_id 不是真实模型名，不能直接发给 API）
_model_to_name: dict[str, str] = {}
# provider 名称 → litellm 前缀映射（由 build_router 从 llm.yaml providers.type 填充）
_provider_type_map: dict[str, str] = {}


def get_litellm_prefix(provider_name: str) -> str:
    """获取 provider 对应的 litellm 前缀（从配置动态读取）。

    优先从 _provider_type_map（llm.yaml providers.type 字段）查找。
    若映射为空（reset_router() 后未重建，或独立调用未走 build_router），
    则懒加载从 yaml 重建，避免回退到 provider_name 本身——provider 名
    （如 yichengc/apigo）不是 litellm 合法前缀，直接用会导致注册成
    "yichengc/glm-5.2" 这种 litellm 无法路由的非法字符串。

    Args:
        provider_name: 配置中的 provider 名称（如 "apigo"、"zhipu_coding"）

    Returns:
        litellm 模型字符串前缀（如 "openai"、"zai"）
    """
    if provider_name in _provider_type_map:
        return _provider_type_map[provider_name]
    # 映射为空或缺失：懒加载重建，绝不回退到 provider_name 本身（非法前缀）
    _ensure_provider_type_map_loaded()
    return _provider_type_map.get(provider_name, provider_name)


def _ensure_provider_type_map_loaded() -> None:
    """懒加载 _provider_type_map（若为空则从 yaml 重建）。

    应对 reset_router() 清空映射后的窗口期，以及 adapter/llm_core 等独立
    调用 get_litellm_prefix 的场景，保证总能拿到 provider.type 配置。
    """
    if _provider_type_map:
        return
    try:
        from config.models import get_model_config_loader  # noqa: PLC0415

        loader = get_model_config_loader()
        llm_data = loader._load_llm_data()
        for pn, pc in llm_data.get("providers", {}).items():
            if isinstance(pc, dict) and "type" in pc:
                _provider_type_map[pn] = pc["type"]
    except Exception:  # noqa: BLE001
        # 加载失败不抛：调用方拿到 provider_name 兜底，至少不比原来更差
        logger.warning("[Router] 懒加载 provider_type_map 失败", exc_info=True)


def _get_litellm_model_string(provider: str, model_name: str) -> str:
    """计算 litellm 格式的模型标识字符串。"""
    prefix = get_litellm_prefix(provider)
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
    """从 llm.yaml 的 defaults.tiers.fallback_chain 构建 Router fallbacks。

    BUG-FIX: 原来读 defaults.fallback_chain，但 yaml 里 fallback_chain
    放在 defaults.tiers.fallback_chain 下，导致始终读空，fallback 永不生效。
    """
    llm_data = model_loader._load_llm_data()
    defaults = llm_data.get("defaults", {})
    tiers = defaults.get("tiers", {})
    fallback_chain = tiers.get("fallback_chain", {})

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
    global _key_pools, _model_to_provider, _provider_type_map  # noqa: PLW0602

    # 钉死 LLM 出口直连：清掉进程内代理 env + 关 aiohttp trust_env + NO_PROXY 兜底。
    # 放在 Router 首次构建处执行一次，从源头与宿主机系统代理解耦。
    disable_llm_proxy()

    llm_data = model_loader._load_llm_data()
    defaults = llm_data.get("defaults", {})
    call_timeout = defaults.get("call_timeout", 600)

    # 从 providers.type 字段构建 provider → litellm 前缀映射
    _provider_type_map.clear()
    for provider_name, provider_conf in llm_data.get("providers", {}).items():
        if isinstance(provider_conf, dict) and "type" in provider_conf:
            _provider_type_map[provider_name] = provider_conf["type"]
            logger.info(
                "[Router] provider %s → litellm prefix: %s",
                provider_name, provider_conf["type"],
            )

    provider_keys = _parse_provider_keys(llm_data)
    model_list = build_model_list(model_loader, provider_keys)
    fallbacks = build_fallbacks(model_loader)

    # 构建 model_id → provider 映射
    _model_to_provider.clear()
    # 构建 model_id → model_name 映射（KeyPoolAdapter 直连时反查真实模型名用）
    _model_to_name.clear()
    for model_id, model_conf in llm_data.get("models", {}).items():
        provider = model_conf.get("provider", "")
        model_name = model_conf.get("model_name", model_id)
        if provider:
            _model_to_provider[model_id] = provider
        _model_to_name[model_id] = model_name

    # 构建 KeyPools（仅用于统计展示）
    for prov_name, slots in provider_keys.items():
        _key_pools[prov_name] = KeyPool(slots, pool_id=prov_name)

    router_kwargs: dict[str, Any] = {
        "model_list": model_list,
        "num_retries": 1,
        "allowed_fails": 5,
        "cooldown_time": 15,
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


def get_model_name_for_id(model_id: str) -> str:
    """根据 model_id 反查 model_name（如 deepseek-v4-pro-apigo → deepseek-v4-pro）。

    model_id 是 yaml key，用作路由标识；model_name 是发给上游 API 的真实模型名。
    KeyPoolAdapter 直连时用 model_id 路由到正确 provider，再反查 model_name 拼
    litellm 模型字符串。
    """
    return _model_to_name.get(model_id, model_id)


def build_adapter(model_loader: Any) -> Any:
    """构建 KeyPoolAdapter — 按 key 粒度并发控制 + RPM 限流 + 配额追踪。"""
    from llm.adapter import KeyPoolAdapter  # noqa: PLC0415

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
    global _router_instance  # noqa: PLW0603
    if _router_instance is None:
        _router_instance = build_router(model_loader)
    return _router_instance


def get_or_create_adapter(model_loader: Any) -> Any:
    """获取或创建共享的 Adapter 单例。"""
    global _adapter_instance  # noqa: PLW0603
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
    global _router_instance, _adapter_instance  # noqa: PLW0603
    _router_instance = None
    _adapter_instance = None
    _key_pools.clear()
    _model_to_provider.clear()
    _model_to_name.clear()
    _provider_type_map.clear()
    logger.info("[Router] 模块级单例已重置")

