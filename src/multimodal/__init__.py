"""
多模态支持模块

提供多模态输入（图片、音频、视频等）的核心支持功能，包括：
- 类型定义：媒体类型、附件信息、模型能力等
- 模型适配器：不同LLM提供商的消息格式转换
- 能力注册表：模型多模态能力的管理和查询
- 文件存储：上传文件的存储抽象

主要组件:
    - MediaType: 媒体类型枚举（IMAGE、AUDIO、VIDEO、DOCUMENT）
    - AttachmentInfo: 附件信息模型
    - ModelCapability: 模型多模态能力模型
    - MultimodalContent: 多模态内容块模型
    - MultimodalAdapter: 多模态适配器抽象基类
    - OpenAIVisionAdapter: OpenAI Vision 适配器
    - ClaudeVisionAdapter: Claude Vision 适配器
    - DefaultAdapter: 默认适配器（仅文本）
    - ModelCapabilityRegistry: 模型能力注册表
    - IFileStorage: 文件存储接口
    - LocalFileStorage: 本地文件存储实现

使用示例:
    >>> from src.multimodal import (
    ...     MediaType,
    ...     AttachmentInfo,
    ...     ModelCapabilityRegistry,
    ...     OpenAIVisionAdapter,
    ...     LocalFileStorage
    ... )
    >>>
    >>> # 创建附件信息
    >>> attachment = AttachmentInfo(
    ...     file_id="file-123",
    ...     filename="photo.jpg",
    ...     mime_type="image/jpeg",
    ...     size=102400,
    ...     media_type=MediaType.IMAGE,
    ...     base64_data="..."
    ... )
    >>>
    >>> # 获取模型能力
    >>> capability = ModelCapabilityRegistry.get_capability("gpt-4o")
    >>> print(capability.supports_image)  # True
    >>>
    >>> # 使用适配器转换
    >>> adapter = ModelCapabilityRegistry.get_adapter("openai")
    >>> messages = adapter.convert("描述这张图片", [attachment])
"""

# 类型定义
# 适配器
from .adapter import (
    ClaudeVisionAdapter,
    DefaultAdapter,
    MultimodalAdapter,
    OpenAIVisionAdapter,
)
from .asr import ASRConfig, ASRService, get_asr_service, reset_asr_service

# 能力注册表
from .capabilities import ModelCapabilityRegistry

# 文件存储
from .storage import (
    DiskFileStorage,
    IFileStorage,
    LocalFileStorage,
    StorageError,
)
from .types import (
    AttachmentInfo,
    MediaType,
    ModelCapability,
    MultimodalContent,
)

__all__ = [
    # 类型
    "MediaType",
    "AttachmentInfo",
    "ModelCapability",
    "MultimodalContent",
    # 适配器
    "MultimodalAdapter",
    "OpenAIVisionAdapter",
    "ClaudeVisionAdapter",
    "DefaultAdapter",
    # 注册表
    "ModelCapabilityRegistry",
    # ASR 语音识别
    "ASRConfig",
    "ASRService",
    "get_asr_service",
    "reset_asr_service",
    # 存储
    "IFileStorage",
    "DiskFileStorage",
    "LocalFileStorage",
    "StorageError",
]

__version__ = "1.0.0"
