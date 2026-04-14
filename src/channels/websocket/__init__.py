"""WebSocket 通道模块。

提供基于 WebSocket 的实时双向通信通道：
- WebSocketAdapter: 组合输入/输出适配器，管理 WebSocket 服务器生命周期
- WebSocketInputAdapter: 从 WebSocket 接收前端消息，转换为管道 state
- WebSocketOutputAdapter: 将管道结果通过 WebSocket 推送回前端
- WebSocketServer: 基于 aiohttp 的 WebSocket 服务器
- SessionManager: WebSocket 会话管理器
- 事件类型和数据格式定义（protocol 模块）
"""

from channels.websocket.adapter import (
    WebSocketAdapter,
    WebSocketInputAdapter,
    WebSocketOutputAdapter,
)
from channels.websocket.protocol import (
    ConnectionConfirmationData,
    ControlCommand,
    ErrorData,
    EventEnvelope,
    EventType,
    ExecutionDoneData,
    ExecutionProgressData,
    ExecutionStartData,
    PipelineEndData,
    PipelineStartData,
    StreamChunkData,
    StreamEndData,
    StreamStartData,
    create_event,
)
from channels.websocket.server import WebSocketServer
from channels.websocket.session_manager import SessionManager

__all__ = [
    # 适配器
    "WebSocketAdapter",
    "WebSocketInputAdapter",
    "WebSocketOutputAdapter",
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
]
