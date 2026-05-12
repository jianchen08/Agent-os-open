"""
WebSocket 处理器组件

将 UserInputHandler 的职责拆分为独立组件：
- MessageValidator: 消息格式验证
- MessagePersistence: 消息持久化
- StreamProcessor: 流式输出处理
"""

from .message_persistence import MessagePersistence
from .message_validator import MessageValidator
from .stream_processor import StreamProcessor

__all__ = ["MessageValidator", "MessagePersistence", "StreamProcessor"]
