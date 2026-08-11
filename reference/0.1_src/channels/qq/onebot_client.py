"""OneBot v11 协议客户端。

基于 OneBot v11 标准（go-cqhttp / OneBot 实现），通过反向 WebSocket 接收事件，
通过 HTTP API 发送消息。使用 aiohttp 实现。

反向 WebSocket 模式：
- 我们启动 WebSocket 服务端，go-cqhttp 主动连接到我们
- 通过 HTTP API（http://onebot-server:5700/send_msg）发送消息

OneBot v11 协议参考：
https://github.com/botuniverse/onebot-11

核心能力：
- WebSocket 服务端接收事件（反向 WS）
- HTTP API 发送文本/图片等消息
- 自动重连（指数退避）
- on_message 回调机制
- 支持 Array 格式和 CQ 码格式的消息段
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

# 类型别名
MessageCallback = Callable[[dict[str, Any]], Awaitable[None]]


class OneBotClient:
    """OneBot v11 协议客户端。

    通过反向 WebSocket 接收 OneBot 事件，通过 HTTP API 发送消息。
    支持自动重连和 on_message 回调。

    Example::

        client = OneBotClient(ws_port=8080, http_api_url="http://127.0.0.1:5700")
        client.on_message = handle_message
        await client.connect()
    """

    def __init__(
        self,
        ws_host: str = "0.0.0.0",
        ws_port: int = 8080,
        http_api_url: str = "http://127.0.0.1:5700",
        *,
        max_retries: int = 5,
        base_delay: float = 1.0,
    ) -> None:
        """初始化 OneBot 客户端。

        Args:
            ws_host: WebSocket 服务端监听地址
            ws_port: WebSocket 服务端监听端口
            http_api_url: OneBot HTTP API 地址
            max_retries: 最大重连次数
            base_delay: 重连基础延迟（秒）
        """
        self._ws_host = ws_host
        self._ws_port = ws_port
        self._http_api_url = http_api_url.rstrip("/")
        self._max_retries = max_retries
        self._base_delay = base_delay

        self._session: aiohttp.ClientSession | None = None
        self._ws_server: web.AppRunner | None = None
        self._ws_connections: list[web.WebSocketResponse] = []
        self._running = False
        self._receive_task: asyncio.Task[None] | None = None

        self.on_message: MessageCallback | None = None

    @property
    def is_connected(self) -> bool:
        """是否已有 OneBot 实例连接。"""
        return len(self._ws_connections) > 0

    async def connect(self) -> None:
        """启动 WebSocket 服务端和 HTTP session。

        创建 aiohttp Web 服务器监听反向 WS 连接，
        同时创建 HTTP session 用于发送消息。
        """
        self._running = True
        self._session = aiohttp.ClientSession()

        retry_count = 0
        while self._running and retry_count < self._max_retries:
            try:
                app = web.Application()
                app.router.add_route("GET", "/ws", self._ws_handler)
                # 可选：添加 /onebot/v11/ws 路径兼容
                app.router.add_route("GET", "/onebot/v11/ws", self._ws_handler)

                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, self._ws_host, self._ws_port)
                await site.start()

                self._ws_server = runner
                logger.info(
                    "OneBot WS server started on %s:%d",
                    self._ws_host,
                    self._ws_port,
                )
                retry_count = 0

                # 保持服务运行
                while self._running:
                    await asyncio.sleep(1)

            except Exception as exc:
                retry_count += 1
                delay = self._base_delay * (2 ** min(retry_count - 1, 5))
                logger.warning(
                    "OneBot WS server error (retry %d/%d): %s, retrying in %.1fs",
                    retry_count,
                    self._max_retries,
                    exc,
                    delay,
                )
                if self._running and retry_count < self._max_retries:
                    await asyncio.sleep(delay)
                else:
                    break

        logger.info("OneBot client stopped")

    async def start_receive_loop(self) -> None:
        """启动接收循环（非阻塞，后台任务）。"""
        self._receive_task = asyncio.create_task(self.connect())

    async def disconnect(self) -> None:
        """断开连接，清理所有资源。"""
        self._running = False

        # 关闭所有 WS 连接
        for ws in self._ws_connections:
            if not ws.closed:
                await ws.close()
        self._ws_connections.clear()

        # 关闭 WS 服务端
        if self._ws_server:
            await self._ws_server.cleanup()
            self._ws_server = None

        # 取消接收任务
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._receive_task
            self._receive_task = None

        # 关闭 HTTP session
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

        logger.info("OneBot client disconnected")

    async def send_message(
        self,
        user_id: int,
        content: str | list[dict[str, Any]],
        message_type: str = "private",
        group_id: int | None = None,
    ) -> dict[str, Any]:
        """通过 HTTP API 发送消息。

        Args:
            user_id: 接收用户 QQ 号
            content: 消息内容（文本字符串或消息段数组）
            message_type: 消息类型 "private" | "group"
            group_id: 群号（群消息时必填）

        Returns:
            OneBot API 响应字典

        Raises:
            RuntimeError: session 未初始化
        """
        if self._session is None:
            raise RuntimeError("Session not initialized")

        message = self._build_message(content)

        body: dict[str, Any] = {
            "message_type": message_type,
            "message": message,
        }

        if message_type == "private":
            body["user_id"] = user_id
        elif message_type == "group":
            body["group_id"] = group_id or user_id

        url = f"{self._http_api_url}/send_msg"

        async with self._session.post(url, json=body) as resp:
            result: dict[str, Any] = await resp.json()
            if result.get("status") != "ok" and result.get("retcode", 0) != 0:
                logger.error(
                    "OneBot send message failed: status=%s, retcode=%s, msg=%s",
                    result.get("status"),
                    result.get("retcode"),
                    result.get("msg", ""),
                )
            return result

    # ── WebSocket 处理 ──────────────────────────────────────

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """处理 WebSocket 连接。

        当 go-cqhttp 连接到我们的 WS 服务端时触发。

        Args:
            request: aiohttp 请求对象

        Returns:
            WebSocket 响应
        """
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self._ws_connections.append(ws)
        logger.info(
            "OneBot client connected from %s, total connections: %d",
            request.remote,
            len(self._ws_connections),
        )

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_event(data)
                    except (json.JSONDecodeError, Exception) as exc:
                        logger.warning("Error handling OneBot event: %s", exc)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    logger.warning("OneBot WS connection closed/error")
                    break
        finally:
            if ws in self._ws_connections:
                self._ws_connections.remove(ws)
            logger.info("OneBot client disconnected, remaining: %d", len(self._ws_connections))

        return ws

    async def _handle_event(self, data: dict[str, Any]) -> None:
        """处理接收到的 OneBot 事件。

        仅处理 post_type 为 "message" 的事件，忽略其他类型
        （如 notice、meta_event、request 等）。

        Args:
            data: OneBot 事件数据
        """
        post_type = data.get("post_type", "")

        if post_type != "message":
            logger.debug("Ignoring non-message event: %s", post_type)
            return

        if self.on_message:
            await self.on_message(data)
        else:
            logger.debug("No on_message callback, message dropped")

    @staticmethod
    def _build_message(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
        """构建 OneBot 消息段数组。

        将文本字符串转为 Array 格式消息段，如果传入的已经是
        消息段数组则直接返回。

        Args:
            content: 文本字符串或消息段数组

        Returns:
            OneBot v11 消息段数组
        """
        if isinstance(content, list):
            return content
        return [{"type": "text", "data": {"text": content}}]
