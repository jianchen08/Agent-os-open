"""
LLM 配置管理器

管理 LLM 模型配置、提供商配置和默认模型
"""

from pathlib import Path
from typing import Any, Optional

import yaml

from src.config.loader import ConfigLoader
from src.config.schemas import EmbeddingConfig, LLMDefaults, ModelConfig, ProviderConfig
from src.core.exceptions.config import ModelNotFoundError, ProviderNotFoundError

# 模块级单例
_llm_config_instance: Optional["LLMConfigManager"] = None

# 配置文件路径
CONFIG_FILE_PATH = Path("config/models/llm.yaml")


class LLMConfigManager:
    """LLM 配置管理器"""

    def __init__(self, config: dict[str, Any] | None = None):
        """
        初始化 LLM 配置管理器

        Args:
            config: LLM 配置字典，如果为 None 则从文件加载
        """
        self._raw_config: dict[str, Any] = {}

        if config is None:
            loader = ConfigLoader()
            try:
                self._raw_config = loader.load("models/llm.yaml")
            except Exception:
                self._raw_config = {}
        else:
            self._raw_config = config

        self._models: dict[str, ModelConfig] = {}
        self._providers: dict[str, ProviderConfig] = {}
        self._embeddings: dict[str, EmbeddingConfig] = {}
        self._defaults = LLMDefaults()

        self._parse_config(self._raw_config)

    def _parse_config(self, config: dict[str, Any]) -> None:
        """解析配置"""
        # 解析模型配置
        for alias, model_data in config.get("models", {}).items():
            self._models[alias] = ModelConfig(**model_data)

        # 解析提供商配置
        for name, provider_data in config.get("providers", {}).items():
            self._providers[name] = ProviderConfig(**provider_data)

        # 解析嵌入模型配置
        for name, embedding_data in config.get("embeddings", {}).items():
            self._embeddings[name] = EmbeddingConfig(**embedding_data)

        # 解析默认配置
        defaults_data = config.get("defaults", {})
        if defaults_data:
            self._defaults = LLMDefaults(**defaults_data)

    def get_model(self, alias: str) -> ModelConfig:
        """
        通过内部别名获取模型配置

        Args:
            alias: 模型别名

        Returns:
            模型配置

        Raises:
            ModelNotFoundError: 模型别名不存在
        """
        if alias not in self._models:
            raise ModelNotFoundError(alias)
        return self._models[alias]

    def get_default(self, purpose: str = "chat") -> ModelConfig:
        """
        获取指定用途的默认模型

        Args:
            purpose: 用途（chat/reasoning/embedding/fallback）

        Returns:
            默认模型配置

        Raises:
            ModelNotFoundError: 默认模型不存在
        """
        alias = getattr(self._defaults, purpose, None)
        if alias is None:
            alias = self._defaults.chat
        return self.get_model(alias)

    def get_default_alias(self, purpose: str = "chat") -> str:
        """获取指定用途默认模型的别名（alias 字符串）。"""
        alias = getattr(self._defaults, purpose, None)
        return alias or self._defaults.chat

    def get_provider(self, name: str) -> ProviderConfig:
        """
        获取提供商配置

        Args:
            name: 提供商名称

        Returns:
            提供商配置

        Raises:
            ProviderNotFoundError: 提供商不存在
        """
        if name not in self._providers:
            raise ProviderNotFoundError(name)
        return self._providers[name]

    def get_embedding(self, name: str) -> EmbeddingConfig:
        """
        获取嵌入模型配置

        Args:
            name: 嵌入模型名称

        Returns:
            嵌入模型配置
        """
        if name not in self._embeddings:
            raise ModelNotFoundError(f"embedding:{name}")
        return self._embeddings[name]

    def list_models(self) -> list[str]:
        """
        列出所有可用模型别名

        Returns:
            模型别名列表
        """
        return list(self._models.keys())

    def list_providers(self) -> list[str]:
        """
        列出所有提供商

        Returns:
            提供商名称列表
        """
        return list(self._providers.keys())

    def has_model(self, alias: str) -> bool:
        """检查模型是否存在"""
        return alias in self._models

    def find_model_by_name_or_alias(self, identifier: str) -> ModelConfig | None:
        """按 alias 或 model_name 查找模型配置。

        消费方传入的标识可能是 llm.yaml 的 alias（如 minimax-m3），
        也可能是底层 model_name（如 MiniMax-M3）。此方法统一两者，
        避免下游模块各自实现遍历逻辑（信息泄漏，code_reviewer §0.2 散点检查）。

        Args:
            identifier: alias 或 model_name

        Returns:
            ModelConfig 实例，未找到返回 None
        """
        if self.has_model(identifier):
            return self.get_model(identifier)
        for alias in self.list_models():
            m = self.get_model(alias)
            if m.model_name == identifier:
                return m
        return None

    def has_provider(self, name: str) -> bool:
        """检查提供商是否存在"""
        return name in self._providers

    def add_model(
        self,
        alias: str,
        provider: str,
        model_name: str,
        display_name: str,
        api_base: str | None = None,
        default_params: dict[str, Any] | None = None,
    ) -> ModelConfig:
        """
        添加模型配置

        Args:
            alias: 模型别名
            provider: 提供商名称
            model_name: 模型名称
            display_name: 显示名称
            api_base: API 基础 URL
            default_params: 默认参数

        Returns:
            新添加的模型配置
        """
        model_config = ModelConfig(
            provider=provider,
            model_name=model_name,
            display_name=display_name,
            api_base=api_base,
            default_params=default_params or {},
        )
        self._models[alias] = model_config
        return model_config

    def remove_model(self, alias: str) -> None:
        """
        删除模型配置

        Args:
            alias: 模型别名

        Raises:
            ModelNotFoundError: 模型不存在
        """
        if alias not in self._models:
            raise ModelNotFoundError(alias)
        del self._models[alias]

    def save_to_file(self) -> None:
        """
        将当前配置保存到 YAML 文件

        将内存中的配置持久化到 config/llm.yaml
        """
        # 构建要保存的配置字典
        config_to_save: dict[str, Any] = {}

        # 保存模型配置
        models_dict: dict[str, Any] = {}
        for alias, model in self._models.items():
            model_data: dict[str, Any] = {
                "provider": model.provider,
                "model_name": model.model_name,
                "display_name": model.display_name,
            }
            if model.api_base:
                model_data["api_base"] = model.api_base
            if model.api_key:
                model_data["api_key"] = model.api_key
            if model.context_window:
                model_data["context_window"] = model.context_window
            if model.reasoning_model:
                model_data["reasoning_model"] = model.reasoning_model
            if model.dimension:
                model_data["dimension"] = model.dimension
            if model.default_params:
                model_data["default_params"] = model.default_params
            models_dict[alias] = model_data
        config_to_save["models"] = models_dict

        # 保存默认配置
        config_to_save["defaults"] = {
            "chat": self._defaults.chat,
            "embedding": self._defaults.embedding,
            "fallback": self._defaults.fallback,
            "tiers": self._defaults.tiers,
        }

        # 保存提供商配置
        providers_dict: dict[str, Any] = {}
        for name, provider in self._providers.items():
            provider_data: dict[str, Any] = {}
            if provider.api_key:
                provider_data["api_key"] = provider.api_key
            if provider.api_base:
                provider_data["api_base"] = provider.api_base
            providers_dict[name] = provider_data
        config_to_save["providers"] = providers_dict

        # 保存嵌入模型配置
        embeddings_dict: dict[str, Any] = {}
        for name, embedding in self._embeddings.items():
            embedding_data: dict[str, Any] = {
                "provider": embedding.provider,
                "model_name": embedding.model_name,
            }
            if embedding.dimension:
                embedding_data["dimension"] = embedding.dimension
            embeddings_dict[name] = embedding_data
        config_to_save["embeddings"] = embeddings_dict

        # 写入文件
        CONFIG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
            # 添加文件头注释
            f.write("# LLM 模型配置\n")
            f.write("# 此文件由系统自动管理，请勿手动编辑\n\n")
            yaml.dump(
                config_to_save,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

    # ============================================
    # 统一接口实现 (IConfigManager)
    # ============================================

    def load(self, key: str) -> dict[str, Any]:
        """
        统一加载接口

        Args:
            key: 配置键 (model, provider, embedding, defaults)

        Returns:
            配置字典
        """
        if key == "models":
            return {alias: model.__dict__ for alias, model in self._models.items()}
        if key == "providers":
            return {name: provider.__dict__ for name, provider in self._providers.items()}
        if key == "embeddings":
            return {name: emb.__dict__ for name, emb in self._embeddings.items()}
        if key == "defaults":
            return self._defaults.__dict__
        if key.startswith("model:"):
            # 获取单个模型: model:deepseek-chat
            model_alias = key.split(":", 1)[1]
            model = self.get_model(model_alias)
            return model.__dict__
        if key.startswith("provider:"):
            # 获取单个提供商: provider:deepseek
            provider_name = key.split(":", 1)[1]
            provider = self.get_provider(provider_name)
            return provider.__dict__
        raise KeyError(f"未知的配置键: {key}")

    def save(self, key: str, config: dict[str, Any]) -> None:
        """
        统一保存接口

        Args:
            key: 配置键
            config: 配置字典
        """
        if key == "models":
            # 批量更新模型
            for alias, model_data in config.items():
                self._models[alias] = ModelConfig(**model_data)
        elif key == "providers":
            # 批量更新提供商
            for name, provider_data in config.items():
                self._providers[name] = ProviderConfig(**provider_data)
        elif key == "embeddings":
            # 批量更新嵌入模型
            for name, emb_data in config.items():
                self._embeddings[name] = EmbeddingConfig(**emb_data)
        elif key == "defaults":
            # 更新默认配置
            self._defaults = LLMDefaults(**config)
        elif key.startswith("model:"):
            # 更新单个模型
            model_alias = key.split(":", 1)[1]
            self._models[model_alias] = ModelConfig(**config)
        elif key.startswith("provider:"):
            # 更新单个提供商
            provider_name = key.split(":", 1)[1]
            self._providers[provider_name] = ProviderConfig(**config)
        else:
            raise KeyError(f"未知的配置键: {key}")

        # 保存到文件
        self.save_to_file()

    def get_all_keys(self) -> list[str]:
        """
        获取所有可用的配置键

        Returns:
            配置键列表
        """
        keys = ["models", "providers", "embeddings", "defaults"]
        # 添加所有模型键
        keys.extend([f"model:{alias}" for alias in self._models])
        # 添加所有提供商键
        keys.extend([f"provider:{name}" for name in self._providers])
        return keys

    def has_key(self, key: str) -> bool:
        """
        检查配置键是否存在

        Args:
            key: 配置键

        Returns:
            是否存在
        """
        if key in ["models", "providers", "embeddings", "defaults"]:
            return True
        if key.startswith("model:"):
            model_alias = key.split(":", 1)[1]
            return self.has_model(model_alias)
        if key.startswith("provider:"):
            provider_name = key.split(":", 1)[1]
            return self.has_provider(provider_name)
        return False

    def get_metadata(self) -> dict[str, Any]:
        """
        获取配置管理器的元数据

        Returns:
            元数据字典
        """
        return {
            "name": "llm",
            "description": "LLM 模型配置管理器",
            "version": "1.0.0",
            "model_count": len(self._models),
            "provider_count": len(self._providers),
            "embedding_count": len(self._embeddings),
            "available_keys": ["models", "providers", "embeddings", "defaults"],
        }


def get_llm_config() -> LLMConfigManager:
    """
    获取 LLM 配置管理器单例

    Returns:
        LLMConfigManager 实例
    """
    global _llm_config_instance  # noqa: PLW0603
    if _llm_config_instance is None:
        _llm_config_instance = LLMConfigManager()
    return _llm_config_instance


def get_model_context_window(model_alias: str) -> int:
    """
    获取模型的上下文窗口大小

    Args:
        model_alias: 模型别名（如 glm-4.7, deepseek-chat）

    Returns:
        上下文窗口大小（tokens），默认 128000
    """
    try:
        config = get_llm_config()
        model = config.get_model(model_alias)
        return model.context_window
    except Exception:
        return 128000  # 默认值


def reset_llm_config() -> None:
    """重置 LLM 配置单例（用于测试）"""
    global _llm_config_instance  # noqa: PLW0603
    _llm_config_instance = None
