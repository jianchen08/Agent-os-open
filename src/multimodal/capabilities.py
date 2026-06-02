"""
模型能力注册表

暴露接口：
- get_capability(cls, model_name: str) -> ModelCapability：get_capability功能
- get_adapter(cls, provider: str) -> MultimodalAdapter：get_adapter功能
- get_adapter_for_model(cls, model_name: str) -> MultimodalAdapter：get_adapter_for_model功能
- register_capability(cls, capability: ModelCapability) -> None：register_capability功能
- register_adapter(cls, provider: str, adapter_class: type[MultimodalAdapter]) -> None：register_adapter功能
- is_multimodal_supported(cls, model_name: str) -> bool：is_multimodal_supported功能
- ModelCapabilityRegistry：ModelCapabilityRegistry类
"""

from .adapter import (
    ClaudeVisionAdapter,
    DefaultAdapter,
    MultimodalAdapter,
    OpenAIVisionAdapter,
)
from .types import ModelCapability


class ModelCapabilityRegistry:
    """
    模型能力注册表

    集中管理各模型的多模态能力配置，提供能力查询和适配器获取功能。

    功能:
        - 预定义常见模型能力（GPT-4V、GPT-4o、Claude-3、DeepSeek等）
        - 提供模型能力查询接口
        - 提供提供商到适配器的映射

    Attributes:
        CAPABILITIES: 预定义的模型能力字典
        ADAPTER_MAPPING: 提供商到适配器类的映射
        PROVIDER_MODEL_MAPPING: 模型名称到提供商的映射

    Example:
        >>> # 获取模型能力
        >>> capability = ModelCapabilityRegistry.get_capability("gpt-4o")
        >>> print(capability.supports_image)
        True
        >>>
        >>> # 获取适配器
        >>> adapter = ModelCapabilityRegistry.get_adapter("openai")
        >>> messages = adapter.convert("描述图片", [attachment])
    """

    # 预定义模型能力配置
    CAPABILITIES: dict[str, ModelCapability] = {
        # === OpenAI 模型 ===
        "gpt-4-vision-preview": ModelCapability(
            model_name="gpt-4-vision-preview",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp"
            ],
            max_image_size=20 * 1024 * 1024,  # 20MB
        ),
        "gpt-4-turbo": ModelCapability(
            model_name="gpt-4-turbo",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp"
            ],
            max_image_size=20 * 1024 * 1024,
        ),
        "gpt-4o": ModelCapability(
            model_name="gpt-4o",
            supports_image=True,
            supports_audio=True,
            supports_video=False,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp"
            ],
            max_image_size=20 * 1024 * 1024,
            max_audio_size=25 * 1024 * 1024,
        ),
        "gpt-4o-mini": ModelCapability(
            model_name="gpt-4o-mini",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp"
            ],
            max_image_size=20 * 1024 * 1024,
        ),
        # === Claude 模型 ===
        "claude-3-opus": ModelCapability(
            model_name="claude-3-opus",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp"
            ],
            max_image_size=20 * 1024 * 1024,
        ),
        "claude-3-sonnet": ModelCapability(
            model_name="claude-3-sonnet",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp"
            ],
            max_image_size=20 * 1024 * 1024,
        ),
        "claude-3-haiku": ModelCapability(
            model_name="claude-3-haiku",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp"
            ],
            max_image_size=20 * 1024 * 1024,
        ),
        "claude-3-5-sonnet": ModelCapability(
            model_name="claude-3-5-sonnet",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supports_document=True,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp"
            ],
            supported_document_types=[
                "application/pdf",
                "text/plain",
                "text/markdown",
                "text/csv",
            ],
            max_image_size=20 * 1024 * 1024,
            max_document_size=10 * 1024 * 1024,
        ),
        "claude-3-7-sonnet": ModelCapability(
            model_name="claude-3-7-sonnet",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supports_document=True,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp"
            ],
            supported_document_types=[
                "application/pdf",
                "text/plain",
                "text/markdown",
                "text/csv",
            ],
            max_image_size=20 * 1024 * 1024,
            max_document_size=10 * 1024 * 1024,
        ),
        # === DeepSeek 模型 ===
        "deepseek-chat": ModelCapability(
            model_name="deepseek-chat",
            supports_image=False,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[],
        ),
        "deepseek-reasoner": ModelCapability(
            model_name="deepseek-reasoner",
            supports_image=False,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[],
        ),
        # === Gemini 模型 ===
        "gemini-pro-vision": ModelCapability(
            model_name="gemini-pro-vision",
            supports_image=True,
            supports_audio=False,
            supports_video=True,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp"
            ],
            max_image_size=20 * 1024 * 1024,
            max_video_size=100 * 1024 * 1024,
        ),
        "gemini-1.5-pro": ModelCapability(
            model_name="gemini-1.5-pro",
            supports_image=True,
            supports_audio=True,
            supports_video=True,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp"
            ],
            max_image_size=20 * 1024 * 1024,
            max_audio_size=25 * 1024 * 1024,
            max_video_size=100 * 1024 * 1024,
        ),
        # === 智谱 GLM 模型 ===
        "glm-4v": ModelCapability(
            model_name="glm-4v",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp"
            ],
            max_image_size=20 * 1024 * 1024,
        ),
        "glm-4.7": ModelCapability(
            model_name="glm-4.7",
            supports_image=False,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[],
        ),
        "glm-5.1": ModelCapability(
            model_name="glm-5.1",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
            ],
            max_image_size=20 * 1024 * 1024,
        ),
        "glm-5-turbo": ModelCapability(
            model_name="glm-5-turbo",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
            ],
            max_image_size=20 * 1024 * 1024,
        ),
        "MiniMax-M3": ModelCapability(
            model_name="MiniMax-M3",
            supports_image=True,
            supports_audio=False,
            supports_video=True,
            supported_image_types=[
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
            ],
            max_image_size=20 * 1024 * 1024,
        ),
        # === Ollama 本地模型 ===
        "llava": ModelCapability(
            model_name="llava",
            supports_image=True,
            supports_audio=False,
            supports_video=False,
            supported_image_types=[
                "image/jpeg",
                "image/png"
            ],
            max_image_size=20 * 1024 * 1024,
        ),
    }

    # 提供商到适配器类的映射
    ADAPTER_MAPPING: dict[str, type[MultimodalAdapter]] = {
        "openai": OpenAIVisionAdapter,
        "openai_reasoning": OpenAIVisionAdapter,
        "anthropic": ClaudeVisionAdapter,
        "anthropic_reasoning": ClaudeVisionAdapter,
        "google": OpenAIVisionAdapter,  # Gemini 使用类似 OpenAI 的格式
        "zhipu": OpenAIVisionAdapter,   # 智谱使用类似 OpenAI 的格式
        "zhipu_coding": OpenAIVisionAdapter,  # GLM-5 系列支持图片
        "minimax": OpenAIVisionAdapter,  # MiniMax-M3 支持图片和视频
        "deepseek": DefaultAdapter,
        "deepseek_reasoning": DefaultAdapter,
        "ollama": OpenAIVisionAdapter,   # Ollama 使用类似 OpenAI 的格式
    }

    # 模型名称到提供商的映射（用于自动推断提供商）
    PROVIDER_MODEL_MAPPING: dict[str, str] = {
        # OpenAI
        "gpt-4-vision-preview": "openai",
        "gpt-4-turbo": "openai",
        "gpt-4o": "openai",
        "gpt-4o-mini": "openai",
        "o1": "openai_reasoning",
        "o1-mini": "openai_reasoning",
        "o3-mini": "openai_reasoning",
        # Anthropic
        "claude-3-opus": "anthropic",
        "claude-3-sonnet": "anthropic",
        "claude-3-haiku": "anthropic",
        "claude-3-5-sonnet": "anthropic",
        "claude-3-7-sonnet": "anthropic",
        "claude-3-7-sonnet-20241022": "anthropic_reasoning",
        # DeepSeek
        "deepseek-chat": "deepseek",
        "deepseek-reasoner": "deepseek_reasoning",
        # Google
        "gemini-pro-vision": "google",
        "gemini-1.5-pro": "google",
        # 智谱
        "glm-4v": "zhipu",
        "glm-4.7": "zhipu_coding",
        "glm-5.1": "zhipu_coding",
        "glm-5-turbo": "zhipu_coding",
        # MiniMax
        "MiniMax-M3": "minimax",
        # Ollama
        "llava": "ollama",
    }

    @classmethod
    def get_capability(cls, model_name: str) -> ModelCapability:
        """获取模型能力"""
        return cls.CAPABILITIES.get(
            model_name,
            ModelCapability(model_name=model_name)
        )

    @classmethod
    def get_adapter(cls, provider: str) -> MultimodalAdapter:
        """获取适配器实例"""
        adapter_class = cls.ADAPTER_MAPPING.get(provider, DefaultAdapter)
        return adapter_class()

    @classmethod
    def get_adapter_for_model(cls, model_name: str) -> MultimodalAdapter:
        """根据模型名称获取适配器"""
        provider = cls.PROVIDER_MODEL_MAPPING.get(model_name, "default")
        return cls.get_adapter(provider)

    @classmethod
    def register_capability(cls, capability: ModelCapability) -> None:
        """注册模型能力"""
        cls.CAPABILITIES[capability.model_name] = capability

    @classmethod
    def register_adapter(cls, provider: str, adapter_class: type[MultimodalAdapter]) -> None:
        """注册适配器"""
        cls.ADAPTER_MAPPING[provider] = adapter_class

    @classmethod
    def is_multimodal_supported(cls, model_name: str) -> bool:
        """检查模型是否支持多模态"""
        capability = cls.get_capability(model_name)
        return (
            capability.supports_image
            or capability.supports_audio
            or capability.supports_video
        )
