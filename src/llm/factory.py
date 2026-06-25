"""
LLM 工厂

根据配置创建 LLM 客户端实例，支持返回 LangChain BaseChatModel

重构说明:
- 继承 CachedFactory 提供类型映射和实例缓存功能
- 保留 LLM 特定的配置获取逻辑
"""

from typing import Optional

from langchain_core.language_models import BaseChatModel

from src.config.llm_config import LLMConfigManager, get_llm_config
from src.config.schemas import ModelConfig
from src.core.exceptions import ModelNotAvailableError
from src.core.registry_base import CachedFactory
from src.llm.base import LLMClient
from src.llm.clients.anthropic import AnthropicClient
from src.llm.clients.mock import MockClient
from src.llm.clients.ollama import OllamaClient
from src.llm.clients.openai import OpenAIClient
from src.llm.clients.reasoning import (
    AnthropicReasoningClient,
    DeepSeekReasoningClient,
    OpenAIReasoningClient,
)
from src.llm.clients.zhipu import ZhipuClient

# 模块级单例（向后兼容）
_llm_factory_instance: Optional["LLMFactory"] = None


class LLMFactory(CachedFactory[str, LLMClient]):
    """
    LLM 工厂

    根据配置创建 LLM 客户端实例，支持：
    - 类型映射：提供商 -> 客户端类
    - 实例缓存：避免重复创建相同配置的客户端
    - LangChain 集成：返回 BaseChatModel

    继承:
        - CachedFactory: 提供类型映射和实例缓存功能

    注意:
        此工厂使用模型别名作为缓存键，但使用提供商类型来创建实例。
        这是通过重写 _get_cache_key 和 _validate_key 方法实现的。
    """

    # 提供商到客户端类的映射
    TYPE_MAPPING = {
        "openai": OpenAIClient,
        "anthropic": AnthropicClient,
        "ollama": OllamaClient,
        "openai_compatible": OpenAIClient,  # 兼容 OpenAI 格式
        "zhipu": ZhipuClient,  # 智谱 AI 普通 API
        "zhipu_coding": ZhipuClient,  # 智谱 AI Coding Plan API
        "mock": MockClient,  # Mock 客户端（用于测试）
        # 思考模型提供商
        "deepseek_reasoning": DeepSeekReasoningClient,  # DeepSeek R1
        "openai_reasoning": OpenAIReasoningClient,  # OpenAI o1/o3
        "anthropic_reasoning": AnthropicReasoningClient,  # Claude 3.7 Sonnet
    }

    def __init__(self, config_manager: LLMConfigManager | None = None):
        """
        初始化 LLM 工厂

        Args:
            config_manager: LLM 配置管理器，如果为 None 则使用默认配置
        """
        super().__init__()
        self._config = config_manager or get_llm_config()

    def _get_model_config(self, model_alias: str) -> ModelConfig:
        """
        获取模型配置

        Args:
            model_alias: 模型别名

        Returns:
            模型配置

        Raises:
            ModelNotAvailableError: 模型配置不存在
        """
        try:
            return self._config.get_model(model_alias)
        except Exception as e:
            raise ModelNotAvailableError(f"模型 '{model_alias}' 不可用: {e}") from e

    def _validate_key(self, model_alias: str) -> None:
        """
        验证模型别名是否有效

        重写父类方法：验证模型别名是否存在，而不是验证提供商类型

        Args:
            model_alias: 模型别名

        Raises:
            ModelNotAvailableError: 模型别名无效
        """
        try:
            model_config = self._get_model_config(model_alias)
            # 同时验证提供商是否支持
            if model_config.provider not in self.TYPE_MAPPING:
                available = list(self.TYPE_MAPPING.keys())
                raise ModelNotAvailableError(
                    f"不支持的提供商: {model_config.provider}。"
                    f"可用提供商: {available}"
                )
        except ModelNotAvailableError:
            raise
        except Exception as e:
            raise ModelNotAvailableError(f"模型 '{model_alias}' 不可用: {e}") from e

    def _get_cache_key(self, model_alias: str, **kwargs) -> str:
        """
        获取缓存键

        重写父类方法：直接使用模型别名作为缓存键

        Args:
            model_alias: 模型别名
            **kwargs: 额外的参数

        Returns:
            缓存键
        """
        return model_alias

    def _create_instance(self, model_alias: str, **kwargs) -> LLMClient:
        """
        创建 LLM 客户端实例

        Args:
            model_alias: 模型别名
            **kwargs: 额外的创建参数（此处未使用）

        Returns:
            LLM 客户端实例

        Raises:
            ModelNotAvailableError: 模型配置不存在或提供商不支持
        """
        # 获取模型配置
        model_config = self._get_model_config(model_alias)
        provider = model_config.provider

        # 获取客户端类
        client_class = self.TYPE_MAPPING.get(provider)
        if not client_class:
            raise ModelNotAvailableError(f"不支持的提供商: {provider}")

        # 获取 API 配置
        api_key = None
        api_base = model_config.api_base

        if self._config.has_provider(provider):
            provider_config = self._config.get_provider(provider)
            api_key = provider_config.api_key
            if not api_base:
                api_base = provider_config.api_base

        # 从环境变量读取 API Key（如果配置中没有）
        api_key = self._get_api_key_from_env(provider, api_key)

        # 创建客户端
        return client_class(
            model_name=model_config.model_name,
            api_key=api_key,
            api_base=api_base,
            default_params=model_config.default_params,
        )

    def _get_api_key_from_env(self, provider: str, current_key: str | None) -> str | None:
        """
        从环境变量获取 API Key

        Args:
            provider: 提供商名称
            current_key: 当前已配置的 API Key

        Returns:
            API Key 或 None
        """
        import os

        if current_key:
            return current_key

        env_mapping = {
            "zhipu": ["ZHIPU_API_KEY", "APP_ZHIPU_API_KEY"],
            "zhipu_coding": ["ZHIPU_API_KEY", "APP_ZHIPU_API_KEY"],
            "openai": ["OPENAI_API_KEY"],
            "anthropic": ["ANTHROPIC_API_KEY"],
            "deepseek_reasoning": ["DEEPSEEK_API_KEY"],
            "openai_reasoning": ["OPENAI_API_KEY"],
            "anthropic_reasoning": ["ANTHROPIC_API_KEY"],
        }

        env_vars = env_mapping.get(provider, [])
        for env_var in env_vars:
            key = os.getenv(env_var)
            if key:
                return key

        return None

    def get_client(self, model_alias: str) -> LLMClient:
        """
        获取 LLM 客户端（带缓存）

        Args:
            model_alias: 模型别名

        Returns:
            LLM 客户端实例

        Raises:
            ModelNotAvailableError: 模型不可用
        """
        return self.get(model_alias)

    def get_langchain_model(self, model_alias: str) -> BaseChatModel:
        """
        获取 LangChain BaseChatModel 实例

        Args:
            model_alias: 模型别名

        Returns:
            LangChain BaseChatModel 实例
        """
        client = self.get_client(model_alias)
        return client.as_langchain()

    def get_default_client(self, purpose: str = "chat") -> LLMClient:
        """
        获取默认客户端

        Args:
            purpose: 用途（chat/reasoning/embedding/fallback）

        Returns:
            默认 LLM 客户端
        """
        # 获取默认模型配置
        model_config = self._config.get_default(purpose)

        # 查找对应的别名
        for alias in self._config.list_models():
            if self._config.get_model(alias) == model_config:
                return self.get_client(alias)

        # 直接创建（不使用缓存）
        return self._create_instance_from_config(model_config)

    def get_default_langchain_model(self, purpose: str = "chat") -> BaseChatModel:
        """
        获取默认 LangChain BaseChatModel 实例

        Args:
            purpose: 用途（chat/reasoning/embedding/fallback）

        Returns:
            LangChain BaseChatModel 实例
        """
        client = self.get_default_client(purpose)
        return client.as_langchain()

    def _create_instance_from_config(self, model_config: ModelConfig) -> LLMClient:
        """
        直接从配置创建客户端（不经过缓存）

        Args:
            model_config: 模型配置

        Returns:
            LLM 客户端实例
        """
        provider = model_config.provider
        client_class = self.TYPE_MAPPING.get(provider)

        if not client_class:
            raise ModelNotAvailableError(f"不支持的提供商: {provider}")

        api_key = None
        api_base = model_config.api_base

        if self._config.has_provider(provider):
            provider_config = self._config.get_provider(provider)
            api_key = provider_config.api_key
            if not api_base:
                api_base = provider_config.api_base

        api_key = self._get_api_key_from_env(provider, api_key)

        return client_class(
            model_name=model_config.model_name,
            api_key=api_key,
            api_base=api_base,
            default_params=model_config.default_params,
        )

    def list_available_models(self) -> list[str]:
        """列出所有可用模型"""
        return self._config.list_models()

    def list_available_providers(self) -> list[str]:
        """列出所有可用提供商"""
        return self.list_available_types()
