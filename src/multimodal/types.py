"""
多模态类型定义

暴露接口：
- MediaType：MediaType类
- AttachmentInfo：AttachmentInfo类
- ModelCapability：ModelCapability类
- MultimodalContent：MultimodalContent类
"""

from enum import Enum

from pydantic import BaseModel, Field


class MediaType(str, Enum):
    """
    媒体类型枚举

    定义支持的媒体文件类型，用于标识附件的媒体类别。

    Attributes:
        IMAGE: 图片类型（支持 jpeg、png、gif、webp 等）
        AUDIO: 音频类型（支持 mp3、wav、m4a 等）
        VIDEO: 视频类型（支持 mp4、mov、avi 等）
        DOCUMENT: 文档类型（支持 pdf、doc、docx 等）
    """

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


class AttachmentInfo(BaseModel):
    """
    附件信息模型

    存储上传文件的完整信息，包括文件标识、元数据和内容数据。

    Attributes:
        file_id: 文件唯一标识符（UUID格式）
        filename: 原始文件名
        mime_type: MIME类型（如 image/jpeg、audio/mp3）
        size: 文件大小（字节）
        media_type: 媒体类型（图片、音频、视频、文档）
        base64_data: Base64编码的文件内容（可选）
        url: 文件访问URL（可选，用于外部存储）

    Example:
        >>> attachment = AttachmentInfo(
        ...     file_id="550e8400-e29b-41d4-a716-446655440000",
        ...     filename="photo.jpg",
        ...     mime_type="image/jpeg",
        ...     size=102400,
        ...     media_type=MediaType.IMAGE,
        ...     base64_data="/9j/4AAQSkZJRg..."
        ... )
    """

    file_id: str = Field(..., description="文件唯一标识符")
    filename: str = Field(..., description="原始文件名")
    mime_type: str = Field(..., description="MIME类型")
    size: int = Field(..., ge=0, description="文件大小（字节）")
    media_type: MediaType = Field(..., description="媒体类型")
    base64_data: str | None = Field(None, description="Base64编码的文件内容")
    url: str | None = Field(None, description="文件访问URL")


class ModelCapability(BaseModel):
    """
    模型多模态能力模型

    定义特定模型支持的多模态能力，包括支持的媒体类型和限制。

    Attributes:
        model_name: 模型名称（如 gpt-4o、claude-3-opus）
        supports_image: 是否支持图片输入
        supports_audio: 是否支持音频输入
        supports_video: 是否支持视频输入
        supports_document: 是否支持文档输入
        supported_image_types: 支持的图片MIME类型列表
        supported_audio_types: 支持的音频MIME类型列表
        supported_video_types: 支持的视频MIME类型列表
        supported_document_types: 支持的文档MIME类型列表
        max_image_size: 最大图片文件大小（字节），默认20MB
        max_audio_size: 最大音频文件大小（字节），默认25MB
        max_video_size: 最大视频文件大小（字节），默认100MB
        max_document_size: 最大文档文件大小（字节），默认10MB

    Example:
        >>> capability = ModelCapability(
        ...     model_name="gpt-4o",
        ...     supports_image=True,
        ...     supports_audio=True,
        ...     supported_image_types=["image/jpeg", "image/png"]
        ... )
    """

    model_name: str = Field(..., description="模型名称")
    supports_image: bool = Field(default=False, description="是否支持图片")
    supports_audio: bool = Field(default=False, description="是否支持音频")
    supports_video: bool = Field(default=False, description="是否支持视频")
    supports_document: bool = Field(default=False, description="是否支持文档")
    supported_image_types: list[str] = Field(default_factory=list, description="支持的图片MIME类型")
    supported_audio_types: list[str] = Field(default_factory=list, description="支持的音频MIME类型")
    supported_video_types: list[str] = Field(default_factory=list, description="支持的视频MIME类型")
    supported_document_types: list[str] = Field(default_factory=list, description="支持的文档MIME类型")
    max_image_size: int = Field(default=20 * 1024 * 1024, ge=0, description="最大图片大小（字节）")
    max_audio_size: int = Field(default=25 * 1024 * 1024, ge=0, description="最大音频大小（字节）")
    max_video_size: int = Field(default=100 * 1024 * 1024, ge=0, description="最大视频大小（字节）")
    max_document_size: int = Field(default=10 * 1024 * 1024, ge=0, description="最大文档大小（字节）")


class MultimodalContent(BaseModel):
    """
    多模态内容块模型

    表示消息中的一个内容块，可以是文本、图片或其他媒体类型。
    不同模型提供商使用不同的格式。

    Attributes:
        type: 内容类型（text、image_url、image 等）
        text: 文本内容（当 type="text" 时使用）
        image_url: 图片URL信息（OpenAI格式，当 type="image_url" 时使用）
        source: 图片源信息（Claude格式，当 type="image" 时使用）

    Example:
        >>> # 文本内容
        >>> text_content = MultimodalContent(
        ...     type="text",
        ...     text="描述这张图片"
        ... )
        >>>
        >>> # OpenAI 图片格式
        >>> image_content = MultimodalContent(
        ...     type="image_url",
        ...     image_url={"url": "data:image/jpeg;base64,/9j/4AAQ..."}
        ... )
        >>>
        >>> # Claude 图片格式
        >>> claude_image = MultimodalContent(
        ...     type="image",
        ...     source={
        ...         "type": "base64",
        ...         "media_type": "image/jpeg",
        ...         "data": "/9j/4AAQ..."
        ...     }
        ... )
    """

    type: str = Field(..., description="内容类型")
    text: str | None = Field(None, description="文本内容")
    image_url: dict | None = Field(None, description="OpenAI格式的图片URL")
    source: dict | None = Field(None, description="Claude格式的图片源")
