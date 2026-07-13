"""统一消息格式定义。

定义多渠道消息网关的统一消息类型，所有渠道消息
在进入管道前都需标准化为 UnifiedMessage，响应也统一为 UnifiedResponse。

Attributes:
    UnifiedMessage: 统一入站消息格式
    UnifiedResponse: 统一出站响应格式
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UnifiedMessage:
    """统一入站消息格式。

    所有渠道（飞书、钉钉、WebSocket、CLI）的消息经过标准化后
    都转换为此格式，供管道引擎统一处理。

    Attributes:
        message_id: 统一消息 ID，全局唯一
        channel_type: 来源通道标识，如 "feishu" | "dingtalk" | "websocket" | "cli"
        channel_user_id: 通道内的用户 ID（如飞书 open_id、钉钉 staff_id）
        unified_user_id: 跨通道统一用户 ID，格式 "{channel_type}:{channel_user_id}"
        content: 消息文本内容
        content_type: 内容类型，"text" | "card" | "image" | "file"
        raw_message: 渠道原始消息字典，保留用于回写和调试
        timestamp: 消息时间戳（Unix 秒）
        metadata: 附加元数据（如消息来源群聊、引用消息等）
    """

    message_id: str
    channel_type: str
    channel_user_id: str
    unified_user_id: str
    content: str
    content_type: str
    raw_message: dict[str, Any]
    timestamp: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class UnifiedResponse:
    """统一出站响应格式。

    管道引擎的处理结果统一转换为此格式，再由各渠道适配器
    反标准化为渠道特定的发送格式。

    Attributes:
        message_id: 关联的原始消息 ID
        channel_type: 目标通道标识
        content: 响应文本内容
        content_type: 响应内容类型，"text" | "card"
        card_config: 卡片配置（仅飞书等支持卡片的渠道使用）
        metadata: 附加元数据
    """

    message_id: str
    channel_type: str
    content: str
    content_type: str
    card_config: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
