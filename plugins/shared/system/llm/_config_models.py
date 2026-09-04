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
import re
from pathlib import Path
from typing import Any

# 模块级配置存储（由 set_config 注入，进程内单例）
_config: dict[str, Any] = {}

# 整串 ${VAR} 占位符（ADR §4.3 secrets）
_ENV_REF_RE = re.compile(r"^\$\{(\w+)\}$")

# .env 文件缓存：(mtime, vars)；set_config 每次注入时按 mtime 决定是否重读
_env_cache: tuple[float, dict[str, str]] | None = None


def _resolve_project_root() -> Path | None:
    """向上探测项目根（包含 config/models 的目录）。

    sidecar 从 plugins/shared/system/llm/ 运行，向上 4-5 层即项目根；
    找不到返回 None（保持纯环境变量展开行为，不比原来差）。
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "config" / "models").is_dir():
            return candidate
    return None


def _env_file_vars() -> dict[str, str]:
    """读取项目根 .env（mtime 缓存）。空行/注释跳过。"""
    global _env_cache  # noqa: PLW0603
    root = _resolve_project_root()
    if root is None:
        return {}
    env_path = root / ".env"
    try:
        mtime = env_path.stat().st_mtime
    except OSError:
        return {}
    if _env_cache and _env_cache[0] == mtime:
        return _env_cache[1]
    result: dict[str, str] = {}
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            result[key.strip()] = value.strip().strip('"')
    except OSError:
        return {}
    _env_cache = (mtime, result)
    return result


def _expand_env_vars(value: Any) -> Any:
    """递归解析配置值里的 ``${VAR}`` 占位符为真实 key。

    解析顺序：进程环境变量（sidecar 继承内核父进程环境，tokio Command
    默认行为）→ 项目根 .env 文件兜底。内核只在**启动时**加载一次 .env，
    用户在设置页填写的 key 会写入 .env 但不进入运行中进程的环境——
    .env 文件兜底使 sidecar 重启（配置变更触发的热重启）后即可拿到
    新 key，无需重启内核（ADR §4.3 secrets 的运行时补全）。

    未定义的变量保持 ``${VAR}`` 原样（``expandvars`` 行为），路由构建侧
    以 ``UNRESOLVED:`` 指纹记日志定位。.env.example 的示例值
    （``your-`` 开头）同样不视为已配置。

    递归处理 dict / list / str 三种类型，其他类型原样返回。
    """
    if isinstance(value, str):
        m = _ENV_REF_RE.match(value.strip())
        if m:
            var = m.group(1)
            resolved = os.environ.get(var)
            if resolved is None:
                resolved = _env_file_vars().get(var)
            if resolved is None:
                return value
            if resolved.startswith("your-"):
                # .env.example 示例值——视为未配置，保留占位符
                return value
            return resolved
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
        # 模型条目 default_params 原样透传：未配置即空 dict——不发明兜底值
        # （参数缺省由 llm.complete_stream 按 llm.yaml 回填，缺即不发，
        # 上游按模型自身默认运行；2026-09-03 用户裁定退役 0.7/4096 内联兜底）
        default_params = model_conf.get("default_params", {})
        # 模型级思考强度手填映射（models.<id>.thinking_strength_params）：
        # 不同模型的 think 参数不一致（DeepSeek reasoning_effort / MiniMax adaptive
        # thinking / 无 reasoning 的普通模型），每个模型可配置自己的档位参数。
        # 无配置时省略（不破坏旧配置）。
        thinking_strength_params = model_conf.get("thinking_strength_params")
        # 厂商级思考强度映射（providers.<name>.thinking_strength_params）：
        # 该厂商 API 真实接受的参数形态，llm_core 路由优先级为 厂商 > 手填 >
        # 内置默认表。无配置时省略。
        provider_thinking_strength_params = provider_conf.get("thinking_strength_params")

        defaults = self._load_llm_data().get("defaults", {})
        call_timeout = model_conf.get("call_timeout", defaults.get("call_timeout", 300))
        first_token_timeout = model_conf.get(
            "first_token_timeout", defaults.get("first_token_timeout", 60)
        )
        stream_idle_timeout = model_conf.get(
            "stream_idle_timeout", defaults.get("stream_idle_timeout", 600)
        )

        result: dict[str, Any] = {
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
        if thinking_strength_params:
            result["thinking_strength_params"] = thinking_strength_params
        if provider_thinking_strength_params:
            result["provider_thinking_strength_params"] = provider_thinking_strength_params
        return result
