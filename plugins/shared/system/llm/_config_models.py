"""LLM 插件配置注入桥（task_11 P0-3）。

本模块是 sidecar 内部的配置注入桥，供三处复用：
- ``server.py::_on_load`` 调 ``set_config(config)`` 注入内核下发的配置；
- ``router_factory._ensure_provider_type_map_loaded`` 和
  ``adapter.KeyPoolAdapter._route_call`` 调 ``get_model_config_loader()``
  拿到一个与 0.1 ``ModelConfigLoader`` 接口兼容的 loader（暴露 ``_load_llm_data``）。

配置结构（P1：manifest ``config_files`` 映射，按 id 命名空间合并后注入）::

    {"llm": <llm.yaml 内容>, "embedding": <embedding.yaml 内容>}

``_load_llm_data`` 返回 ``config["llm"]``，即 ``llm.yaml`` 的完整内容
（含顶层 ``models`` / ``providers`` / ``defaults`` / ``concurrency`` 键）。

[来源: docs/tasks/task_11_plugin_capability_unification.md P1-3；ADR §4.3 B3]
"""
from __future__ import annotations

import os
from typing import Any

# 模块级配置存储（由 set_config 注入，进程内单例）
_config: dict[str, Any] = {}


def _expand_env_vars(value: Any) -> Any:
    """递归解析配置值里的 ``${VAR}`` 占位符为进程环境变量值。

    sidecar 子进程继承内核父进程环境（tokio Command 默认行为），
    所以能拿到 ``.env`` 里 ``API_KEY`` 等变量。解析 ADR §4.3 的
    secret 占位符（如 ``api_key: ${ZHIPU_API_KEY}``）。未定义的变量
    展开为空字符串（``expandvars`` 默认行为）。

    递归处理 dict / list / str 三种类型，其他类型原样返回。
    """
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    return value


def set_config(config: dict[str, Any]) -> None:
    """注入内核下发的插件配置。

    注入时递归解析 ``${VAR}`` 占位符（ADR §4.3 secrets：占位符解析在
    sidecar 收到配置后进行，使 router_factory 拿到真实 key）。

    Args:
        config: 内核经 ``config_files`` 映射合并后注入的配置字典（P1 起）。
    """
    global _config  # noqa: PLW0603
    _config = _expand_env_vars(config) if isinstance(config, dict) else {}


def get_config() -> dict[str, Any]:
    """获取当前注入的配置（调试用）。"""
    return _config


def get_model_config_loader() -> ModelConfigLoaderShim:
    """返回一个与 0.1 ``ModelConfigLoader`` 接口兼容的 loader 实例。

    loader 暴露 ``_load_llm_data()``，返回当前注入配置中的 ``llm`` 命名空间节。
    """
    return ModelConfigLoaderShim(_config)


class ModelConfigLoaderShim:
    """模拟 0.1 ``ModelConfigLoader`` 接口，数据来自注入配置。

    ``router_factory.build_router`` / ``build_adapter`` 及
    ``adapter.KeyPoolAdapter._route_call`` 调用 ``_load_llm_data()``
    获取 ``llm.yaml`` 解析后的字典。
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

    def _load_llm_data(self) -> dict[str, Any]:
        """返回 ``llm.yaml`` 内容（即注入配置的 ``llm`` 命名空间节）。

        P1 config_files 映射后内核注入结构为
        ``{"llm": <llm.yaml 全文>, "embedding": <embedding.yaml 全文>}``，
        本方法取 ``config["llm"]``。缺失或非 dict 返回 ``{}``，
        不抛异常（让调用方按空配置降级，而非崩溃）。
        """
        llm_config = self._config.get("llm")
        if not isinstance(llm_config, dict):
            return {}
        return llm_config

    def _load_embedding_data(self) -> dict[str, Any]:
        """返回 ``embedding.yaml`` 内容（注入配置的 ``embedding`` 命名空间节）。"""
        emb = self._config.get("embedding")
        return emb if isinstance(emb, dict) else {}

    @staticmethod
    def _case_insensitive_lookup(
        mapping: dict[str, Any], key: str
    ) -> tuple[str, Any] | None:
        """大小写不敏感查 key，返回 (真实key, value)；未命中返回 None。"""
        if not key:
            return None
        key_lower = key.lower()
        for k, v in mapping.items():
            if k.lower() == key_lower:
                return k, v
        return None

    def get_model_config(self, model_id: str) -> dict[str, Any] | None:
        """根据 model_id 从 ``llm.yaml`` 的 models 段取模型配置。

        与 0.1 ``ModelConfigLoader.get_model_config`` 对齐（仅 LLM models，
        sidecar 场景不做 embedding 回退——LLMCore 不会用 embedding）。
        """
        models = self._load_llm_data().get("models", {})
        hit = self._case_insensitive_lookup(models, model_id)
        return hit[1] if hit else None

    def get_provider_config(self, provider_name: str) -> dict[str, Any] | None:
        """根据 provider 名从 ``llm.yaml`` 的 providers 段取提供商配置。"""
        providers = self._load_llm_data().get("providers", {})
        conf = providers.get(provider_name)
        return dict(conf) if isinstance(conf, dict) else None

    def resolve_tier(self, tier: str) -> str:
        """从 ``llm.yaml`` defaults.tiers 解析 tier 为 model_id。

        与 0.1 ``plugin_resolver.resolve_tier`` 对齐：tier(large/medium/small)
        → defaults.tiers[tier] → model_id。
        """
        if not tier:
            return ""
        tiers = self._load_llm_data().get("defaults", {}).get("tiers", {})
        return tiers.get(tier, "")

    def get_default_chat_model(self) -> str:
        """``llm.yaml`` defaults.chat（默认对话模型 id）。"""
        return self._load_llm_data().get("defaults", {}).get("chat", "")

    def get_llm_core_config(self, model_id: str) -> dict[str, Any] | None:
        """获取 LLMCore 所需格式的模型配置（与 0.1 ``ModelConfigLoader`` 对齐）。

        合并 model_conf + provider_conf，产出扁平的
        provider/model_name/api_base/api_key/default_params/context_window/
        call_timeout/first_token_timeout/stream_idle_timeout 字典。
        """
        model_conf = self.get_model_config(model_id)
        if model_conf is None:
            return None

        provider_name = model_conf.get("provider", "")
        provider_conf = self.get_provider_config(provider_name) or {}

        # api_key: 模型配置优先，provider.keys[0] 回退
        api_key = model_conf.get("api_key", "") or provider_conf.get("api_key", "")
        if not api_key:
            keys_list = provider_conf.get("keys", [])
            if keys_list:
                api_key = keys_list[0].get("api_key", "")

        api_base = model_conf.get("api_base", "") or provider_conf.get("api_base", "")
        default_params = model_conf.get(
            "default_params", {"temperature": 0.7, "max_tokens": 4096}
        )

        defaults = self._load_llm_data().get("defaults", {})
        call_timeout = model_conf.get("call_timeout", defaults.get("call_timeout", 300))
        first_token_timeout = model_conf.get(
            "first_token_timeout", defaults.get("first_token_timeout", 60)
        )
        stream_idle_timeout = model_conf.get(
            "stream_idle_timeout", defaults.get("stream_idle_timeout", 600)
        )

        return {
            "provider": provider_name,
            "model_name": model_conf.get("model_name", model_id),
            "model_id": model_id,
            "api_base": api_base,
            "api_key": api_key,
            "context_window": model_conf.get("context_window"),
            "default_params": default_params,
            "call_timeout": call_timeout,
            "first_token_timeout": first_token_timeout,
            "stream_idle_timeout": stream_idle_timeout,
        }

