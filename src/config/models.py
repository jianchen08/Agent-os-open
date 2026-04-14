"""模型配置加载器。

从 config/models/ 目录加载 LLM 和嵌入模型配置，支持环境变量替换
和提供商配置聚合。

支持的功能：
- 从 ``config/models/llm.yaml`` 加载模型配置
- 从 ``config/models/embedding.yaml`` 加载嵌入模型配置
- 环境变量替换：``${ENV_VAR}`` 格式自动替换为 ``os.environ.get("ENV_VAR", "")``
- ``get_model_config(model_id)`` 返回模型配置字典
- ``get_default_model(model_type)`` 返回默认模型配置
- ``get_provider_config(provider_name)`` 返回提供商配置（含 api_key、api_base）
- ``get_llm_core_config(model_id)`` 返回 LLMCore 所需格式配置

典型用法::

    from config import ModelConfigLoader

    loader = ModelConfigLoader()
    config = loader.get_llm_core_config("minimax-m2.7")
    # {"provider": "minimax", "model_name": "MiniMax-M2.7",
    #  "api_base": "...", "api_key": "...", "default_params": {...}}
"""

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


def _substitute_env_vars(value: Any) -> Any:
    """递归替换字典/列表/字符串中的环境变量占位符。

    将 ``${ENV_VAR}`` 格式的占位符替换为 ``os.environ.get("ENV_VAR", "")``。
    若整个字符串就是一个占位符，替换后保留原始类型推导（空字符串视为空）；
    若占位符是字符串的一部分，则做字符串替换。

    Args:
        value: 待替换的值，可以是字典、列表、字符串或其他类型。

    Returns:
        替换后的值，类型与输入一致。
    """
    if isinstance(value, str):
        def _replace(match: re.Match[str]) -> str:
            var_name = match.group(1)
            return os.environ.get(var_name, "")
        return _ENV_VAR_PATTERN.sub(_replace, value)
    elif isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_substitute_env_vars(item) for item in value]
    return value


class ModelConfigLoader:
    """模型配置加载器。

    从 YAML 文件加载模型配置和提供商配置，支持环境变量替换。

    加载两个配置文件：
    - ``llm.yaml``: LLM 模型定义、默认模型、提供商配置
    - ``embedding.yaml``: 嵌入模型定义、提供商配置

    Args:
        config_dir: 模型配置目录路径，默认为项目 ``config/models/`` 目录。
    """

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self._config_dir = Path(config_dir) if config_dir else _DEFAULT_CONFIG_DIR
        self._llm_data: dict[str, Any] | None = None
        self._embedding_data: dict[str, Any] | None = None

    # ── 内部加载方法 ──────────────────────────────────────────

    def _load_llm_data(self) -> dict[str, Any]:
        """加载并缓存 LLM 配置数据。

        Returns:
            LLM 配置字典，含 models、defaults、providers 顶级键。
        """
        if self._llm_data is None:
            path = self._config_dir / "llm.yaml"
            self._llm_data = self._load_yaml(path)
        return self._llm_data

    def _load_embedding_data(self) -> dict[str, Any]:
        """加载并缓存嵌入模型配置数据。

        Returns:
            嵌入配置字典，含 embeddings、default_embedding、providers 顶级键。
        """
        if self._embedding_data is None:
            path = self._config_dir / "embedding.yaml"
            self._embedding_data = self._load_yaml(path)
        return self._embedding_data

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        """加载 YAML 文件并做环境变量替换。

        Args:
            path: YAML 文件路径。

        Returns:
            解析后的字典数据。

        Raises:
            FileNotFoundError: 文件不存在。
        """
        if not path.exists():
            raise FileNotFoundError(f"模型配置文件不存在: {path}")

        with open(path, encoding="utf-8") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}

        return _substitute_env_vars(raw)

    # ── 公共查询接口 ──────────────────────────────────────────

    def get_model_config(self, model_id: str) -> dict[str, Any] | None:
        """根据模型 ID 获取模型配置。

        依次查找 LLM 配置和嵌入配置中的模型定义。

        Args:
            model_id: 模型标识（如 ``minimax-m2.7``、``embedding-3``）。

        Returns:
            模型配置字典，若未找到返回 ``None``。
        """
        # 先在 LLM models 中查找
        llm_data = self._load_llm_data()
        models = llm_data.get("models", {})
        if model_id in models:
            return dict(models[model_id])

        # 再在 embedding embeddings 中查找
        emb_data = self._load_embedding_data()
        embeddings = emb_data.get("embeddings", {})
        if model_id in embeddings:
            return dict(embeddings[model_id])

        return None

    def get_default_model(self, model_type: str = "chat") -> dict[str, Any] | None:
        """获取默认模型配置。

        根据 llm.yaml 中的 ``defaults`` 节查找默认模型 ID，再返回其配置。

        Args:
            model_type: 模型类型，可选 ``chat``、``reasoning``、``embedding``。
                默认为 ``chat``。

        Returns:
            默认模型配置字典，若未找到返回 ``None``。
        """
        if model_type == "embedding":
            emb_data = self._load_embedding_data()
            default_id = emb_data.get("default_embedding", "")
            embeddings = emb_data.get("embeddings", {})
            if default_id and default_id in embeddings:
                return dict(embeddings[default_id])
            return None

        # chat / reasoning 类型从 llm.yaml defaults 查找
        llm_data = self._load_llm_data()
        defaults = llm_data.get("defaults", {})
        default_id = defaults.get(model_type, "")
        models = llm_data.get("models", {})
        if default_id and default_id in models:
            return dict(models[default_id])

        return None

    def get_provider_config(self, provider_name: str) -> dict[str, Any] | None:
        """获取提供商配置。

        依次查找 LLM 配置和嵌入配置中的 providers 节。

        Args:
            provider_name: 提供商名称（如 ``minimax``、``deepseek``、``zhipu``）。

        Returns:
            提供商配置字典（含 api_key、api_base 等），若未找到返回 ``None``。
        """
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
        """获取 LLMCore 所需格式的模型配置。

        将模型配置和提供商配置合并，提取 LLMCore.__init__ 所需字段：
        ``provider``、``model_name``、``api_base``、``api_key``、``default_params``。

        合并逻辑：
        1. 以模型配置为基础
        2. api_key 优先取模型配置中的值，若为空则回退到提供商配置
        3. api_base 同理
        4. 添加 default_params（若模型配置中未指定则使用默认值）

        Args:
            model_id: 模型标识（如 ``minimax-m2.7``）。

        Returns:
            LLMCore 格式的配置字典，若模型未找到返回 ``None``。
        """
        model_conf = self.get_model_config(model_id)
        if model_conf is None:
            return None

        provider_name = model_conf.get("provider", "")
        provider_conf = self.get_provider_config(provider_name) or {}

        # api_key: 模型配置优先，提供商配置回退
        api_key = model_conf.get("api_key", "") or provider_conf.get("api_key", "")

        # api_base: 模型配置优先，提供商配置回退
        api_base = model_conf.get("api_base", "") or provider_conf.get("api_base", "")

        # default_params: 使用模型配置中的值，或默认值
        default_params = model_conf.get("default_params", {"temperature": 0.7, "max_tokens": 4096})

        return {
            "provider": provider_name,
            "model_name": model_conf.get("model_name", model_id),
            "api_base": api_base,
            "api_key": api_key,
            "default_params": default_params,
        }

    def resolve_env_or_model(self, value: str, provider_name: str = "") -> str:
        """解析环境变量占位符，若为空则回退到模型提供商配置中的 api_key。

        此方法用于管道配置中的 ``${MINIMAX_API_KEY}`` 等占位符解析：
        1. 先替换环境变量
        2. 若替换后为空字符串且 provider_name 已知，则回退到提供商配置

        Args:
            value: 可能包含 ``${ENV_VAR}`` 的字符串。
            provider_name: 提供商名称，用于回退查找。

        Returns:
            解析后的字符串值。
        """
        resolved = _substitute_env_vars(value)
        if isinstance(resolved, str) and not resolved.strip() and provider_name:
            provider_conf = self.get_provider_config(provider_name)
            if provider_conf:
                fallback = provider_conf.get("api_key", "")
                if fallback:
                    return fallback
        return resolved if isinstance(resolved, str) else str(resolved)
