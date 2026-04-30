"""WebSocket 服务器。

基于 aiohttp 实现轻量级 WebSocket 服务器，提供：
- WebSocket 连接的接受与管理
- 消息接收与分发
- 生命周期管理（启动 / 停止）
- 与 SessionManager 集成
- ACK 确认处理
- 断线重连消息补发
- 协议版本协商

不引入 Django/Flask 等重框架，仅依赖 aiohttp。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

from aiohttp import web

from channels.websocket.protocol import (
    ACK_REQUIRED_EVENTS,
    ACK_TIMEOUT_SECONDS,
    ConnectionConfirmationData,
    ControlCommand,
    EventEnvelope,
    EventType,
    MessageAckData,
    MissedMessagesData,
    PROTOCOL_VERSION,
    RequestMissedData,
    create_event,
    is_version_compatible,
    negotiate_version,
)
from channels.websocket.session_manager import SessionManager

logger = logging.getLogger(__name__)

# 消息处理器类型：接收 session_id 和解析后的消息
MessageHandler = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


class WebSocketServer:
    """WebSocket 服务器。

    基于 aiohttp.web 实现的轻量级 WebSocket 服务器。
    负责管理 WebSocket 连接的建立、消息收发和连接断开。

    Attributes:
        host: 监听地址
        port: 监听端口
        session_manager: 会话管理器
        on_message: 消息处理器回调

    Example::

        server = WebSocketServer(host="0.0.0.0", port=8765)
        server.on_message = my_handler
        await server.start()
        # ... 运行中 ...
        await server.stop()
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        session_manager: SessionManager | None = None,
        ping_interval: float = 30.0,
        ping_timeout: float = 10.0,
    ) -> None:
        """初始化 WebSocket 服务器。

        Args:
            host: 监听地址，默认 "0.0.0.0"
            port: 监听端口，默认 8765
            session_manager: 会话管理器实例，不传则自动创建
            ping_interval: 心跳间隔（秒），默认 30
            ping_timeout: 心跳超时（秒），默认 10
        """
        self.host = host
        self.port = port
        self.session_manager = session_manager or SessionManager()
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._on_message_handler: MessageHandler | None = None
        self._on_disconnect_handler: Callable[[str], Coroutine[Any, Any, None]] | None = None

    @property
    def on_message(self) -> MessageHandler | None:
        """获取消息处理器。"""
        return self._on_message_handler

    @on_message.setter
    def on_message(self, handler: MessageHandler) -> None:
        """设置消息处理器。

        当收到前端消息时调用该处理器。

        Args:
            handler: 异步消息处理函数，签名为 async (session_id, parsed_message) -> None
        """
        self._on_message_handler = handler

    @property
    def on_disconnect(self) -> Callable[[str], Coroutine[Any, Any, None]] | None:
        """获取断连处理器。"""
        return self._on_disconnect_handler

    @on_disconnect.setter
    def on_disconnect(self, handler: Callable[[str], Coroutine[Any, Any, None]]) -> None:
        """设置断连处理器。

        当 WebSocket 连接断开时调用该处理器。

        Args:
            handler: 异步断连处理函数，签名为 async (session_id) -> None
        """
        self._on_disconnect_handler = handler

    async def start(self) -> None:
        """启动 WebSocket 服务器。

        创建 aiohttp Application 和 AppRunner，开始监听指定端口。
        """
        self._app = web.Application()
        self._app.router.add_get("/ws", self._handle_websocket)
        self._app.router.add_get("/ws/{thread_id}", self._handle_websocket)
        self._app.router.add_get("/ws/chat/{thread_id}", self._handle_websocket)
        self._app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        logger.info("WebSocket server started on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        """停止 WebSocket 服务器。

        优雅关闭所有连接，清理资源。
        """
        if self._runner is not None:
            await self._runner.cleanup()
            logger.info("WebSocket server stopped")

        self._app = None
        self._runner = None
        self._site = None

    async def send_event(
        self,
        session_id: str,
        event: EventEnvelope,
    ) -> bool:
        """向指定会话发送事件。

        将事件信封序列化为 JSON 字符串后通过 WebSocket 发送。
        如果事件类型在 ACK_REQUIRED_EVENTS 集合中，
        自动标记 requires_ack 并追踪 ACK。

        Args:
            session_id: 目标会话 ID
            event: 事件信封对象

        Returns:
            发送成功返回 True，失败返回 False
        """
        # 对关键消息自动启用 ACK
        if event.type in ACK_REQUIRED_EVENTS:
            event.requires_ack = True
            self.session_manager.track_pending_ack(
                session_id, event.request_id,
            )

        event.version = PROTOCOL_VERSION
        message = json.dumps(
            event.to_dict(), ensure_ascii=False,
        )
        return await self.session_manager.send_to(
            session_id, message,
        )

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """处理 WebSocket 连接请求。

        流程：
        1. 升级为 WebSocket 连接
        2. 注册会话
        3. 发送 connection_confirmation
        4. 进入消息接收循环
        5. 连接断开后注销会话

        Args:
            request: HTTP 请求对象

        Returns:
            WebSocket 响应对象
        """
        # 验证 token（从 query 参数获取）
        token = request.query.get("token", "")
        if token:
            try:
                from channels.api.auth import verify_token
                payload = verify_token(token)
                if payload is None:
                    ws = web.WebSocketResponse()
                    await ws.prepare(request)
                    await ws.close(code=4001, message=b"Invalid or expired token")
                    return ws
            except ImportError:
                logger.warning("auth 模块不可用，跳过 token 验证")

        ws = web.WebSocketResponse(
            heartbeat=self.ping_interval,
            receive_timeout=self.ping_timeout,
        )
        await ws.prepare(request)

        # 提取 thread_id（用于重连恢复）
        thread_id = request.match_info.get("thread_id", "")

        # 提取客户端协议版本（用于版本协商）
        client_version = request.query.get(
            "version", "",
        )
        if client_version:
            negotiated = negotiate_version(client_version)
            if not is_version_compatible(client_version):
                logger.warning(
                    "Client version %s incompatible, "
                    "negotiated to %s",
                    client_version, negotiated,
                )
        else:
            negotiated = PROTOCOL_VERSION

        # 注册会话
        session_id = await self.session_manager.register(
            ws=ws,
            thread_id=thread_id,
            metadata={
                "client_version": client_version,
                "negotiated_version": negotiated,
            },
        )

        # 发送连接确认（包含协商后的协议版本）
        confirmation = create_event(
            EventType.CONNECTION_CONFIRMATION,
            ConnectionConfirmationData(
                session_id=session_id,
                thread_id=thread_id,
                version=negotiated,
            ).to_dict(),
        )
        await self.send_event(session_id, confirmation)

        # 消息接收循环
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._process_text_message(session_id, msg.data)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(
                        "WebSocket error for session %s: %s",
                        session_id, ws.exception(),
                    )
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.CLOSED):
                    break
        except Exception as exc:
            logger.error("WebSocket loop error for session %s: %s", session_id, exc)
        finally:
            # 注销会话
            await self.session_manager.unregister(session_id)

            # 触发断连处理器
            if self._on_disconnect_handler is not None:
                try:
                    await self._on_disconnect_handler(session_id)
                except Exception as exc:
                    logger.error("Disconnect handler error: %s", exc)

        return ws

    async def _process_text_message(
        self, session_id: str, raw_data: str,
    ) -> None:
        """处理接收到的文本消息。

        解析 JSON 消息，提取事件类型，分发给对应的处理器。
        支持 ACK 确认和请求遗漏消息。

        Args:
            session_id: 发送方的会话 ID
            raw_data: 原始文本数据（JSON 字符串）
        """
        try:
            parsed = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Invalid JSON from session %s: %s",
                session_id, exc,
            )
            return

        # 尝试解析为事件信封
        try:
            envelope = EventEnvelope.from_dict(parsed)
        except ValueError as exc:
            logger.warning(
                "Invalid event envelope from session %s: %s",
                session_id, exc,
            )
            return

        # 处理控制命令
        event_type = envelope.type
        if event_type == ControlCommand.STOP_GENERATION.value:
            logger.info(
                "Stop generation requested: session=%s",
                session_id,
            )
        elif event_type == ControlCommand.RESUME_ACTION.value:
            logger.info(
                "Resume action received: session=%s",
                session_id,
            )

        # 处理 ACK 确认
        elif event_type == EventType.MESSAGE_ACK.value:
            ack_request_id = envelope.data.get(
                "request_id", "",
            )
            if ack_request_id:
                self.session_manager.acknowledge(
                    session_id, ack_request_id,
                )
            return

        # 处理请求遗漏消息
        elif event_type == EventType.REQUEST_MISSED.value:
            await self._handle_request_missed(
                session_id, envelope,
            )
            return

        # 调用消息处理器
        if self._on_message_handler is not None:
            try:
                await self._on_message_handler(
                    session_id, envelope.to_dict(),
                )
            except Exception as exc:
                logger.error(
                    "Message handler error: %s", exc,
                )

    async def _handle_request_missed(
        self,
        session_id: str,
        envelope: EventEnvelope,
    ) -> None:
        """处理前端请求遗漏消息。

        重连后前端发送 request_missed，后端从缓冲区
        中提取遗漏消息并发送。

        Args:
            session_id: 会话 ID
            envelope: request_missed 事件信封
        """
        last_request_id = envelope.data.get(
            "last_received_request_id", "",
        )

        missed = self.session_manager.get_missed_messages(
            session_id, last_request_id,
        )

        missed_event = create_event(
            EventType.MISSED_MESSAGES,
            MissedMessagesData(
                messages=missed,
                total=len(missed),
                has_more=False,
            ).to_dict(),
        )
        await self.send_event(session_id, missed_event)

    async def _handle_health(self, request: web.Request) -> web.Response:
        """健康检查端点。

        Args:
            request: HTTP 请求对象

        Returns:
            JSON 格式的健康状态响应
        """
        return web.json_response({
            "status": "ok",
            "active_sessions": self.session_manager.active_count,
        })
