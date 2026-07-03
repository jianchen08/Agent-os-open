"""模型配置加载器。"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 环境变量占位符模式：${VAR_NAME}
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

# 默认配置目录（相对于项目根目录）
# src/config/models.py → 3 层 parent 到项目根
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "models"

# .env 文件路径（项目根目录）
_ENV_FILE_PATH = Path(__file__).resolve().parent.parent.parent / ".env"

# 跟踪已加载的 .env 文件，避免重复加载
_dotenv_loaded = False


def _load_dotenv_once() -> None:
    """加载项目根目录的 .env 文件到 os.environ（仅执行一次）。"""
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True

    if not _ENV_FILE_PATH.exists():
        return

    try:
        with open(_ENV_FILE_PATH, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                # .env 强制覆盖系统环境变量（见 docstring 设计说明）
                if key:
                    os.environ[key] = value
        logger.debug("已加载 .env 文件（强制覆盖）: %s", _ENV_FILE_PATH)
    except Exception as exc:
        logger.warning("加载 .env 文件失败: %s", exc)


def _substitute_env_vars(value: Any) -> Any:
    """递归替换字典/列表/字符串中的环境变量占位符。"""
    # 确保 .env 已加载
    _load_dotenv_once()

    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            env_value = os.environ.get(var_name)
            if env_value is None:
                logger.warning(
                    "环境变量 %s 未设置，对应配置项将为空。请在 .env 文件或系统环境变量中设置该值。",
                    var_name,
                )
                return ""
            return env_value

        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env_vars(item) for item in value]
    return value


class ModelConfigLoader:
    """模型配置加载器。"""

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self._config_dir = Path(config_dir) if config_dir else _DEFAULT_CONFIG_DIR
        self._llm_data: dict[str, Any] | None = None
        self._embedding_data: dict[str, Any] | None = None

    # ── 内部加载方法 ──────────────────────────────────────────

    def _load_llm_data(self) -> dict[str, Any]:
        """加载并缓存 LLM 配置数据。"""
        if self._llm_data is None:
            path = self._config_dir / "llm.yaml"
            self._llm_data = self._load_yaml(path)
        return self._llm_data

    def _load_embedding_data(self) -> dict[str, Any]:
        """加载并缓存嵌入模型配置数据。"""
        if self._embedding_data is None:
            path = self._config_dir / "embedding.yaml"
            self._embedding_data = self._load_yaml(path)
        return self._embedding_data

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        """加载 YAML 文件并做环境变量替换。"""
        if not path.exists():
            raise FileNotFoundError(f"模型配置文件不存在: {path}")

        with open(path, encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}

        return _substitute_env_vars(raw)

    # ── 公共查询接口 ──────────────────────────────────────────

    @staticmethod
    def _case_insensitive_lookup(
        mapping: dict[str, Any],
        key: str,
    ) -> dict[str, Any] | None:
        """在字典中执行大小写不敏感的键查找。"""
        if key in mapping:
            return dict(mapping[key])
        lower = key.lower()
        for k, v in mapping.items():
            if k.lower() == lower:
                return dict(v)
        return None

    def get_model_config(self, model_id: str) -> dict[str, Any] | None:
        """根据模型 ID 获取模型配置。"""
        # 先在 LLM models 中查找（大小写不敏感）
        llm_data = self._load_llm_data()
        models = llm_data.get("models", {})
        result = self._case_insensitive_lookup(models, model_id)
        if result is not None:
            return result

        # 再在 embedding embeddings 中查找
        emb_data = self._load_embedding_data()
        embeddings = emb_data.get("embeddings", {})
        result = self._case_insensitive_lookup(embeddings, model_id)
        if result is not None:
            return result

        return None

    def get_default_model(self, model_type: str = "chat") -> dict[str, Any] | None:
        """获取默认模型配置。"""
        if model_type == "embedding":
            emb_data = self._load_embedding_data()
            default_id = emb_data.get("default_embedding", "")
            embeddings = emb_data.get("embeddings", {})
            if default_id:
                result = self._case_insensitive_lookup(embeddings, default_id)
                if result is not None:
                    return result
            return None

        # chat / reasoning 类型从 llm.yaml defaults 查找
        llm_data = self._load_llm_data()
        defaults = llm_data.get("defaults", {})
        default_id = defaults.get(model_type, "")
        models = llm_data.get("models", {})
        if default_id:
            result = self._case_insensitive_lookup(models, default_id)
            if result is not None:
                return result
            result["_id"] = default_id
            return result

        return None

    def get_provider_config(self, provider_name: str) -> dict[str, Any] | None:
        """获取提供商配置。"""
        # 先在 LLM providers 中查找
        llm_data = self._load_llm_data()
        providers = llm_data.get("providers", {})
        if provider_name in providers:
            return dict(providers[provider_name])

        # 再在 embedding providers 中查找
        emb_data = self._load_embedding_data()
        emb_providers = emb_data.get("providers", {})
        if provider_name in emb_providers:
            return dict(emb_providers[provider_name])

        return None

    def get_llm_core_config(self, model_id: str) -> dict[str, Any] | None:
        """获取 LLMCore 所需格式的模型配置。"""
        model_conf = self.get_model_config(model_id)
        if model_conf is None:
            return None

        provider_name = model_conf.get("provider", "")
        provider_conf = self.get_provider_config(provider_name) or {}

        # api_key: 模型配置优先，提供商配置回退
        api_key = model_conf.get("api_key", "") or provider_conf.get("api_key", "")
        if not api_key:
            keys_list = provider_conf.get("keys", [])
            if keys_list:
                api_key = keys_list[0].get("api_key", "")

        # api_base: 模型配置优先，提供商配置回退
        api_base = model_conf.get("api_base", "") or provider_conf.get("api_base", "")
        # default_params: 使用模型配置中的值，或默认值
        default_params = model_conf.get("default_params", {"temperature": 0.7, "max_tokens": 4096})

        # call_timeout: 优先模型配置，回退到 defaults 节
        defaults = self._load_llm_data().get("defaults", {})
        call_timeout = model_conf.get("call_timeout", defaults.get("call_timeout", 300))
        # 首 token 超时：流式首 chunk 不来时强制超时的秒数（优先模型配置，回退 defaults）
        first_token_timeout = model_conf.get("first_token_timeout", defaults.get("first_token_timeout", 60))
        # 流式静默超时：连续 N 秒收不到任何 chunk 即中断死等（优先模型配置，回退 defaults）
        stream_idle_timeout = model_conf.get("stream_idle_timeout", defaults.get("stream_idle_timeout", 600))

        return {
            "model_id": model_id,
            "provider": provider_name,
            "model_name": model_conf.get("model_name", model_id),
            "api_base": api_base,
            "api_key": api_key,
            "context_window": model_conf.get("context_window"),
            "default_params": default_params,
            "call_timeout": call_timeout,
            "first_token_timeout": first_token_timeout,
            "stream_idle_timeout": stream_idle_timeout,
        }

    def resolve_env_or_model(self, value: str, provider_name: str = "") -> str:
        """解析环境变量占位符，若为空则回退到模型提供商配置中的 api_key。"""
        resolved = _substitute_env_vars(value)
        if isinstance(resolved, str) and not resolved.strip() and provider_name:
            provider_conf = self.get_provider_config(provider_name)
            if provider_conf:
                fallback = provider_conf.get("api_key", "")
                if fallback:
                    return fallback
        return resolved if isinstance(resolved, str) else str(resolved)


# 模块级缓存 — 避免重复实例化导致 YAML 重复解析

_loader_cache: dict[str, ModelConfigLoader] = {}


def get_model_config_loader(config_dir: str | Path | None = None) -> ModelConfigLoader:
    """获取缓存的 ModelConfigLoader 单例。"""
    cache_key = str(config_dir or _DEFAULT_CONFIG_DIR)
    if cache_key not in _loader_cache:
        _loader_cache[cache_key] = ModelConfigLoader(config_dir)
    return _loader_cache[cache_key]


def invalidate_model_config_cache(config_dir: str | Path | None = None) -> None:
    """清除缓存，下次 get_model_config_loader 会重新加载。"""
    cache_key = str(config_dir or _DEFAULT_CONFIG_DIR)
    _loader_cache.pop(cache_key, None)


def invalidate_all_llm_caches(config_dir: str | Path | None = None) -> None:
    """清除所有 LLM 相关缓存，使配置变更实时生效。"""
    # 1. 清除 ModelConfigLoader 缓存
    invalidate_model_config_cache(config_dir)

    # 2. 清除 LLMConfigManager 单例（延迟导入避免循环依赖）
    from config.llm_config import reset_llm_config

    reset_llm_config()

    # 3. 清除 Router 和 Adapter 单例（延迟导入）
    from llm.router_factory import reset_router

    reset_router()

    # 4. 清除 tier 缓存，使配置变更实时生效
    try:
        import pipeline.plugin_resolver as pr_mod

        pr_mod._tier_cache.clear()
    except Exception:
        pass

    logger.info("所有 LLM 缓存已清除")
