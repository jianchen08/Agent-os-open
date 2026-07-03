"""媒体 Provider 抽象基类与核心数据模型。

暴露接口：
- MediaType：媒体类型枚举（tts/image/video/music）
- MediaResult：统一返回格式数据类
- MediaProviderConfig：Provider 配置 Pydantic 模型
- MediaProvider：媒体 Provider 抽象基类
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class MediaType(str, Enum):
    """媒体类型枚举。"""

    TTS = "tts"
    IMAGE = "image"
    VIDEO = "video"
    MUSIC = "music"


class MediaResult(BaseModel):
    """媒体生成/合成的统一返回格式。

    Attributes:
        file_path: 生成文件的路径
        media_type: 媒体类型
        duration_seconds: 音视频时长（秒），可选
        metadata: 扩展元数据（如 voice、prompt、size 等）
        provider_name: 产生此结果的 Provider 名称
        error: 错误信息（部分失败场景使用）
    """

    file_path: Path = Field(..., description="生成文件的路径")
    media_type: MediaType = Field(..., description="媒体类型")
    duration_seconds: float | None = Field(default=None, description="音视频时长（秒）")
    metadata: dict[str, Any] = Field(default_factory=dict, description="扩展元数据")
    provider_name: str = Field(..., description="产生此结果的 Provider 名称")
    error: str | None = Field(default=None, description="错误信息")


class MediaProviderConfig(BaseModel):
    """Provider 配置模型，对应 YAML 中每个 provider 条目。

    Attributes:
        class_name: Provider 实现类名
        enabled: 是否启用
        priority: 优先级（数值越小越优先）
        config: Provider 特有的配置参数
    """

    class_name: str = Field(..., description="Provider 实现类名")
    enabled: bool = Field(default=True, description="是否启用")
    priority: int = Field(default=99, description="优先级，数值越小越优先")
    config: dict[str, Any] = Field(default_factory=dict, description="Provider 特有的配置参数")


class MediaProvider(ABC):
    """媒体 Provider 抽象基类。

    所有媒体 Provider（TTS、图像、视频、音乐）均需继承此类。

    子类必须实现：
    - is_available(): 检查 Provider 当前是否可用
    - synthesize() 或 generate() 之一（TTS 用 synthesize，其余用 generate）

    Attributes:
        provider_name: Provider 唯一名称
        media_type: Provider 处理的媒体类型
        config: Provider 配置
    """

    def __init__(
        self,
        *,
        provider_name: str,
        media_type: MediaType,
        config: MediaProviderConfig,
    ) -> None:
        """初始化 Provider。

        Args:
            provider_name: Provider 唯一名称
            media_type: Provider 处理的媒体类型
            config: Provider 配置
        """
        self._provider_name = provider_name
        self._media_type = media_type
        self._config = config

    @property
    def provider_name(self) -> str:
        """Provider 唯一名称。"""
        return self._provider_name

    @property
    def media_type(self) -> MediaType:
        """Provider 处理的媒体类型。"""
        return self._media_type

    @property
    def config(self) -> MediaProviderConfig:
        """Provider 配置。"""
        return self._config

    @abstractmethod
    async def is_available(self) -> bool:
        """检查 Provider 当前是否可用。

        Returns:
            True 表示可用，False 表示不可用
        """

    async def synthesize(self, text: str, **kwargs: Any) -> MediaResult:
        """合成媒体内容（主要用于 TTS）。

        Args:
            text: 要合成的文本
            **kwargs: Provider 特有参数（如 voice、rate 等）

        Returns:
            MediaResult 统一返回格式

        Raises:
            NotImplementedError: 此 Provider 不支持 synthesize 操作
        """
        raise NotImplementedError(f"Provider '{self.provider_name}' 不支持 synthesize 操作")

    async def generate(self, prompt: str, **kwargs: Any) -> MediaResult:
        """生成媒体内容（用于图像/视频/音乐）。

        Args:
            prompt: 生成提示词
            **kwargs: Provider 特有参数（如 size、style 等）

        Returns:
            MediaResult 统一返回格式

        Raises:
            NotImplementedError: 此 Provider 不支持 generate 操作
        """
        raise NotImplementedError(f"Provider '{self.provider_name}' 不支持 generate 操作")

    def __repr__(self) -> str:
        """返回 Provider 的字符串表示。"""
        return f"{self.__class__.__name__}(name={self.provider_name!r}, type={self.media_type.value!r})"
