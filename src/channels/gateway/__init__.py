"""多渠道消息网关模块。

提供多 IM 平台统一接入的消息网关系统：
- ChannelGateway: 网关主入口，管理适配器生命周期和消息路由
- MessageNormalizer: 消息标准化器，渠道格式 ↔ 统一格式双向转换
- SessionBridge: 跨通道会话状态桥接
- UnifiedMessage: 统一入站消息格式
- UnifiedResponse: 统一出站响应格式
"""

from channels.gateway.channel_gateway import ChannelGateway
from channels.gateway.message_normalizer import MessageNormalizer
from channels.gateway.session_bridge import SessionBridge
from channels.gateway.unified_types import UnifiedMessage, UnifiedResponse

__all__ = [
    "ChannelGateway",
    "MessageNormalizer",
    "SessionBridge",
    "UnifiedMessage",
    "UnifiedResponse",
]
