"""WebSocket 通道模块（FastAPI 实现）。

提供基于 FastAPI WebSocket 的实时双向通信：
- app_factory: FastAPI 应用工厂，WS 路由入口
- ws_handler: WebSocketInteractionNotifier，连接管理与消息推送
- stream_handler: 流式请求处理，PipelineContext 管理
- static_files: 静态文件挂载
"""

from channels.websocket.static_files import mount_media_static_files
from channels.websocket.stream_handler import PipelineContext, _init_pipeline_context
from channels.websocket.ws_handler import WebSocketInteractionNotifier, ws_interaction_notifier

__all__ = [
    "WebSocketInteractionNotifier",
    "ws_interaction_notifier",
    "PipelineContext",
    "_init_pipeline_context",
    "mount_media_static_files",
]
