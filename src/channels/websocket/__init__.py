"""WebSocket 通道模块。

提供基于 WebSocket 的实时双向通信通道：
- WebSocketServer: 基于 aiohttp 的 WebSocket 服务器
- SessionManager: WebSocket 会话管理器
- 事件类型和数据格式定义（protocol 模块）
- ACK 确认机制
- 协议版本协商
- 断线重连消息恢复

流式输出由 PipelineStreamBridge（pipeline.stream_bridge）统一管理，
不再使用 WebSocketOutputAdapter。
"""

from channels.websocket.protocol import (
    ACK_MAX_RETRIES,
    ACK_REQUIRED_EVENTS,
    ACK_TIMEOUT_SECONDS,
    MIN_SUPPORTED_VERSION,
    PROTOCOL_VERSION,
    ConnectionConfirmationData,
    ControlCommand,
    ErrorData,
    EventEnvelope,
    EventType,
    ExecutionDoneData,
    ExecutionProgressData,
    ExecutionStartData,
    MessageAckData,
    MissedMessagesData,
    PipelineEndData,
    PipelineStartData,
    RequestMissedData,
    StreamChunkData,
    StreamEndData,
    StreamStartData,
    create_event,
    is_version_compatible,
    negotiate_version,
    parse_version,
)
from channels.websocket.server import WebSocketServer
from channels.websocket.session_manager import SessionManager

__all__ = [
    # 服务器
    "WebSocketServer",
    # 会话管理
    "SessionManager",
    # 协议
    "EventEnvelope",
    "EventType",
    "ControlCommand",
    "create_event",
    "StreamStartData",
    "StreamChunkData",
    "StreamEndData",
    "ExecutionStartData",
    "ExecutionProgressData",
    "ExecutionDoneData",
    "PipelineStartData",
    "PipelineEndData",
    "ErrorData",
    "ConnectionConfirmationData",
    # ACK
    "MessageAckData",
    "ACK_REQUIRED_EVENTS",
    "ACK_TIMEOUT_SECONDS",
    "ACK_MAX_RETRIES",
    # 重连
    "RequestMissedData",
    "MissedMessagesData",
    # 版本协商
    "PROTOCOL_VERSION",
    "MIN_SUPPORTED_VERSION",
    "parse_version",
    "is_version_compatible",
    "negotiate_version",
]
