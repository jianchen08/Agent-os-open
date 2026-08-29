"""模型能力注册表。

暴露接口：
- get_capability(model_name) -> ModelCapability：从 config/models/llm.yaml 的
  models.<id>.multimodal 节读取能力
- get_adapter(provider) -> MultimodalAdapter：按提供商获取适配器
- register_adapter(provider, adapter_class)：注册适配器
- is_multimodal_supported(model_name) -> bool：是否支持多模态
- ModelCapabilityRegistry：注册表类
"""

import logging
from pathlib import Path
from typing import Any

import yaml
from adapter import (
    ClaudeVisionAdapter,
    DefaultAdapter,
    MultimodalAdapter,
    OpenAIVisionAdapter,
)
from mm_types import ModelCapability

logger = logging.getLogger(__name__)

# llm.yaml 路径（同 asr.py：插件目录上溯 4 层到项目根）
_LLM_YAML_PATH = Path(__file__).resolve().parents[4] / "config" / "models" / "llm.yaml"

# (mtime, models 节) 缓存：设置页直写 yaml 后无需重启 sidecar 即时生效
_LLM_MODELS_CACHE: tuple[float, dict[str, Any]] | None = None


def _load_llm_models() -> dict[str, Any] | None:
    """读 llm.yaml 的 models 节（mtime 缓存）。

    Returns:
        models 字典；文件缺失/不可读/YAML 损坏时返回 None（内部已发 WARNING），
        调用方按配置断链降级（degraded=True）。
    """
    global _LLM_MODELS_CACHE  # noqa: PLW0603
    try:
        mtime = _LLM_YAML_PATH.stat().st_mtime
    except OSError:
        logger.warning("llm.yaml 不可读（%s），多模态能力按断链降级(degraded=True)", _LLM_YAML_PATH)
        return None
    if _LLM_MODELS_CACHE and _LLM_MODELS_CACHE[0] == mtime:
        return _LLM_MODELS_CACHE[1]
    try:
        with open(_LLM_YAML_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("llm.yaml 解析失败（%s），多模态能力按断链降级(degraded=True): %s", _LLM_YAML_PATH, exc)
        return None
    models = raw.get("models")
    if not isinstance(models, dict):
        models = {}
    _LLM_MODELS_CACHE = (mtime, models)
    return models


def _find_model_conf(models: dict[str, Any], model_name: str) -> dict[str, Any] | None:
    """按 models 键（大小写不敏感）或 model_name 字段匹配模型配置。"""
    lowered = model_name.lower()
    for key, conf in models.items():
        if key.lower() == lowered and isinstance(conf, dict):
            return conf
    for conf in models.values():
        if isinstance(conf, dict) and str(conf.get("model_name", "")).lower() == lowered:
            return conf
    return None


class ModelCapabilityRegistry:
    """模型能力注册表。

    集中管理各模型的多模态能力配置，提供能力查询和适配器获取功能。
    能力元数据从 config/models/llm.yaml 的 multimodal 子节点读取，
    实现"模型升级只改配置"；文件变更经 mtime 缓存即时生效。

    Attributes:
        ADAPTER_MAPPING: 提供商到适配器类的映射

    Example:
        >>> capability = ModelCapabilityRegistry.get_capability("glm-5.2")
        >>> print(capability.supports_image)
        True
        >>>
        >>> adapter = ModelCapabilityRegistry.get_adapter("zhipu_coding")
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

        从 llm.yaml 的 multimodal 配置读取；模型未配置该节点则返回默认空
        能力（degraded=False，语义为"模型未声明多模态"）；配置文件缺失或
        损坏按断链降级——记 WARNING 并置 degraded=True，调用方可区分
        "未配置"与"配置断链"。

        Args:
            model_name: 模型 id（如 minimax-m3，键与 model_name 字段均可、
                大小写不敏感）

        Returns:
            ModelCapability 实例（断链时 degraded=True）
        """
        models = _load_llm_models()
        if models is None:
            return ModelCapability(model_name=model_name, degraded=True)

        conf = _find_model_conf(models, model_name)
        mm = conf.get("multimodal") if conf else None
        if not isinstance(mm, dict):
            return ModelCapability(model_name=model_name)
        return ModelCapability(
            model_name=model_name,
            supports_image=bool(mm.get("supports_image", False)),
            supports_audio=bool(mm.get("supports_audio", False)),
            supports_video=bool(mm.get("supports_video", False)),
            supports_document=bool(mm.get("supports_document", False)),
            supported_image_types=[str(t) for t in mm.get("supported_image_types", [])],
            supported_audio_types=[str(t) for t in mm.get("supported_audio_types", [])],
            supported_video_types=[str(t) for t in mm.get("supported_video_types", [])],
            max_image_size=int(mm.get("max_image_size", 0)),
            max_audio_size=int(mm.get("max_audio_size", 0)),
            max_video_size=int(mm.get("max_video_size", 0)),
            max_document_size=int(mm.get("max_document_size", 0)),
        )

    @classmethod
    def get_adapter(cls, provider: str) -> MultimodalAdapter:
        """获取适配器实例"""
        adapter_class = cls.ADAPTER_MAPPING.get(provider, DefaultAdapter)
        return adapter_class()

    @classmethod
    def register_adapter(cls, provider: str, adapter_class: type[MultimodalAdapter]) -> None:
        """注册适配器"""
        cls.ADAPTER_MAPPING[provider] = adapter_class

    @classmethod
    def is_multimodal_supported(cls, model_name: str) -> bool:
        """检查模型是否支持多模态"""
        capability = cls.get_capability(model_name)
        return capability.supports_image or capability.supports_audio or capability.supports_video
