"""模型能力注册表。

暴露接口：
- get_capability(model_name) -> ModelCapability：从 llm.yaml multimodal 配置读取能力
- get_adapter(provider) -> MultimodalAdapter：按提供商获取适配器
- get_adapter_for_model(model_name) -> MultimodalAdapter：按模型名获取适配器
- register_adapter(provider, adapter_class)：注册适配器
- is_multimodal_supported(model_name) -> bool：是否支持多模态
- ModelCapabilityRegistry：注册表类
"""

from .adapter import (
    ClaudeVisionAdapter,
    DefaultAdapter,
    MultimodalAdapter,
    OpenAIVisionAdapter,
)
from .types import ModelCapability


class ModelCapabilityRegistry:
    """模型能力注册表。

    集中管理各模型的多模态能力配置，提供能力查询和适配器获取功能。
    能力元数据从 llm.yaml 的 multimodal 子节点读取，实现"模型升级只改配置"。

    功能:
        - 从 llm.yaml 配置读取模型多模态能力
        - 提供模型能力查询接口
        - 提供提供商到适配器的映射

    Attributes:
        ADAPTER_MAPPING: 提供商到适配器类的映射

    Example:
        >>> capability = ModelCapabilityRegistry.get_capability("glm-5.2")
        >>> print(capability.supports_image)
        True
        >>>
        >>> adapter = ModelCapabilityRegistry.get_adapter("zhipu_coding")
        >>> messages = adapter.convert("描述图片", [attachment])
    """

    # 提供商到适配器类的映射
    ADAPTER_MAPPING: dict[str, type[MultimodalAdapter]] = {
        "openai": OpenAIVisionAdapter,
        "openai_reasoning": OpenAIVisionAdapter,
        "anthropic": ClaudeVisionAdapter,
        "anthropic_reasoning": ClaudeVisionAdapter,
        "google": OpenAIVisionAdapter,  # Gemini 使用类似 OpenAI 的格式
        "zhipu": OpenAIVisionAdapter,  # 智谱使用类似 OpenAI 的格式
        "zhipu_coding": OpenAIVisionAdapter,  # GLM-5 系列支持图片
        "minimax": OpenAIVisionAdapter,  # MiniMax-M3 支持图片和视频
        "deepseek": DefaultAdapter,
        "deepseek_reasoning": DefaultAdapter,
        "ollama": OpenAIVisionAdapter,  # Ollama 使用类似 OpenAI 的格式
    }

    @classmethod
    def get_capability(cls, model_name: str) -> ModelCapability:
        """获取模型能力。

        从 llm.yaml 的 multimodal 配置读取；未配置则返回默认空能力。

        Args:
            model_name: 模型 alias（如 glm-5.2）或底层 model_name（如 GLM-5.2）

        Returns:
            ModelCapability 实例（未配置时返回全 False 的默认能力）
        """
        from src.config.llm_config import get_llm_config  # noqa: PLC0415

        mgr = get_llm_config()
        model = mgr.find_model_by_name_or_alias(model_name)
        if model and model.multimodal:
            mm = model.multimodal
            return ModelCapability(
                model_name=model_name,
                supports_image=mm.supports_image,
                supports_audio=mm.supports_audio,
                supports_video=mm.supports_video,
                supports_document=mm.supports_document,
                supported_image_types=list(mm.supported_image_types),
                supported_audio_types=list(mm.supported_audio_types),
                supported_video_types=list(mm.supported_video_types),
                max_image_size=mm.max_image_size,
                max_audio_size=mm.max_audio_size,
                max_video_size=mm.max_video_size,
                max_document_size=mm.max_document_size,
            )
        return ModelCapability(model_name=model_name)

    @classmethod
    def get_adapter(cls, provider: str) -> MultimodalAdapter:
        """获取适配器实例"""
        adapter_class = cls.ADAPTER_MAPPING.get(provider, DefaultAdapter)
        return adapter_class()

    @classmethod
    def get_adapter_for_model(cls, model_name: str) -> MultimodalAdapter:
        """根据模型名称获取适配器（provider 从 llm.yaml 配置推断）"""
        from src.llm.router_factory import get_provider_for_model  # noqa: PLC0415

        provider = get_provider_for_model(model_name) or "default"
        return cls.get_adapter(provider)

    @classmethod
    def register_adapter(cls, provider: str, adapter_class: type[MultimodalAdapter]) -> None:
        """注册适配器"""
        cls.ADAPTER_MAPPING[provider] = adapter_class

    @classmethod
    def is_multimodal_supported(cls, model_name: str) -> bool:
        """检查模型是否支持多模态"""
        capability = cls.get_capability(model_name)
        return capability.supports_image or capability.supports_audio or capability.supports_video
