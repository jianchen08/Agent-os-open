"""媒体 Provider 抽象层模块。

提供统一的媒体生成/合成 Provider 抽象和配置体系。

暴露接口：
- MediaType：媒体类型枚举
- MediaResult：统一返回格式
- MediaProviderConfig：Provider 配置模型
- MediaProvider：Provider 抽象基类
- FallbackStrategy：Fallback 策略枚举
- ProviderChain：Fallback 链
- MediaProviderRegistry：Provider 注册表
"""

from tools.media.base import (
    MediaProvider,
    MediaProviderConfig,
    MediaResult,
    MediaType,
)
from tools.media.fallback import FallbackStrategy, ProviderChain
from tools.media.provider_registry import MediaProviderRegistry

__all__ = [
    "MediaType",
    "MediaResult",
    "MediaProviderConfig",
    "MediaProvider",
    "FallbackStrategy",
    "ProviderChain",
    "MediaProviderRegistry",
]
