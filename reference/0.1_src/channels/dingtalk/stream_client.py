"""钉钉 Stream 模式客户端。

基于钉钉 Stream 协议通过 WebSocket 长连接接收消息事件，
并通过 HTTP API 发送消息。使用 aiohttp 实现，不依赖钉钉官方 SDK。

钉钉 Stream 协议参考：
https://open.dingtalk.com/document/orgapp/stream-mode-protocol

核心能力：
- WebSocket 长连接接收事件
- HTTP API 发送文本消息
- 自动重连（指数退避）
- on_message 回调机制
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# 类型别名
MessageCallback = Callable[[dict[str, Any]], Awaitable[None]]


class DingTalkStreamClient:
    """钉钉 Stream 模式客户端。

    通过 WebSocket 长连接接收钉钉事件，通过 HTTP API 发送消息。
    支持自动重连和 on_message 回调。

    Example::

        client = DingTalkStreamClient(client_id="xxx", client_secret="secret")
        client.on_message = handle_message
        await client.connect()
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = "https://api.dingtalk.com",
        stream_url: str = "https://stream.dingtalk.com",
        max_retries: int = 5,
        base_delay: float = 1.0,
    ) -> None:
        """初始化钉钉 Stream 客户端。

        Args:
            client_id: 钉钉应用 client_id（AppKey）
            client_secret: 钉钉应用 client_secret（AppSecret）
            base_url: 钉钉 API 基础 URL
            stream_url: 钉钉 Stream 服务 URL
            max_retries: 最大重连次数
            base_delay: 重连基础延迟（秒）
        """
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._stream_url = stream_url.rstrip("/")
        self._max_retries = max_retries
        self._base_delay = base_delay

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._access_token: str = ""
        self._token_expires: float = 0.0
        self._running = False
        self._receive_task: asyncio.Task[None] | None = None

        self.on_message: MessageCallback | None = None

    @property
    def is_connected(self) -> bool:
        """是否已连接。"""
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        """建立 Stream 连接。

        获取 access_token → 获取 Stream endpoint → 建立连接 → 开始接收循环。
        """
        self._running = True
        self._session = aiohttp.ClientSession()

        await self._ensure_token()

        retry_count = 0
        while self._running and retry_count < self._max_retries:
            try:
                endpoint = await self._get_endpoint()
                if not endpoint:
                    raise RuntimeError("Failed to get stream endpoint")

                self._ws = await self._session.ws_connect(endpoint)
                logger.info("DingTalk stream connected")
                retry_count = 0

                await self._receive_loop()

            except Exception as exc:
                retry_count += 1
                delay = self._base_delay * (2 ** min(retry_count - 1, 5))
                logger.warning(
                    "DingTalk stream error (retry %d/%d): %s, retrying in %.1fs",
                    retry_count,
                    self._max_retries,
                    exc,
                    delay,
                )
                if self._running and retry_count < self._max_retries:
                    await asyncio.sleep(delay)
                else:
                    break

        logger.info("DingTalk stream client stopped")

    async def start_receive_loop(self) -> None:
        """启动接收循环（非阻塞，后台任务）。"""
        self._receive_task = asyncio.create_task(self.connect())

    async def disconnect(self) -> None:
        """断开连接。"""
        self._running = False

        if self._ws and not self._ws.closed:
            await self._ws.close()
            self._ws = None

        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task
            self._receive_task = None

        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

        logger.info("DingTalk stream client disconnected")

    async def send_message(
        self,
        user_id: str,
        content: str,
        msg_type: str = "text",
    ) -> dict[str, Any]:
        """发送消息给钉钉用户。

        Args:
            user_id: 接收用户的 staff_id
            content: 消息内容
            msg_type: 消息类型 "text" | "markdown"

        Returns:
            钉钉 API 响应字典

        Raises:
            RuntimeError: 未连接或发送失败
        """
        await self._ensure_token()

        url = f"{self._base_url}/v1.0/robot/oToMessages/batchSend"
        headers = {
            "x-acs-dingtalk-access-token": self._access_token,
            "Content-Type": "application/json",
        }

        body = {
            "robotCode": self._client_id,
            "userIds": [user_id],
            "msgKey": msg_type,
            "msgParam": content,
        }

        if self._session is None:
            raise RuntimeError("Session not initialized")

        async with self._session.post(url, json=body, headers=headers) as resp:
            result = await resp.json()
            if result.get("code") and result.get("code") != "0":
                logger.error(
                    "DingTalk send message failed: code=%s, msg=%s",
                    result.get("code"),
                    result.get("message"),
                )
            return result

    # ── 内部方法 ──────────────────────────────────────

    async def _ensure_token(self) -> None:
        """确保 access_token 有效。"""
        if self._access_token and time.time() < self._token_expires - 60:
            return

        if self._session is None:
            raise RuntimeError("Session not initialized")

        url = f"{self._base_url}/v1.0/oauth2/accessToken"
        body = {
            "appKey": self._client_id,
            "appSecret": self._client_secret,
        }

        async with self._session.post(url, json=body) as resp:
            result = await resp.json()
            self._access_token = result.get("accessToken", "")
            expire = result.get("expireIn", 7200)
            self._token_expires = time.time() + expire
            logger.debug("DingTalk token refreshed, expires in %ds", expire)

    async def _get_endpoint(self) -> str:
        """获取 Stream WebSocket endpoint。

        Returns:
            WebSocket 连接 URL
        """
        await self._ensure_token()

        if self._session is None:
            raise RuntimeError("Session not initialized")

        # 钉钉 Stream 端点获取
        timestamp = str(int(time.time() * 1000))
        sign = self._compute_sign(timestamp)

        url = f"{self._stream_url}/connect"
        params = {
            "clientId": self._client_id,
            "timestamp": timestamp,
            "sign": sign,
        }

        async with self._session.post(url, json=params) as resp:
            result = await resp.json()
            endpoint = result.get("endpoint", "")
            if not endpoint:
                logger.error("No stream endpoint in response: %s", result)
            return endpoint

    def _compute_sign(self, timestamp: str) -> str:
        """计算签名。

        Args:
            timestamp: 毫秒时间戳字符串

        Returns:
            签名字符串
        """
        string_to_sign = f"{timestamp}\n{self._client_secret}"
        hmac_code = hmac.new(
            self._client_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        import base64  # noqa: PLC0415

        return base64.b64encode(hmac_code).decode("utf-8")

    async def _receive_loop(self) -> None:
        """接收消息循环。"""
        if self._ws is None:
            return

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await self._handle_event(data)
                except (json.JSONDecodeError, Exception) as exc:
                    logger.warning("Error handling stream message: %s", exc)
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                logger.warning("DingTalk stream connection closed/error")
                break

        if self._running:
            logger.info("DingTalk stream disconnected, will reconnect")

    async def _handle_event(self, data: dict[str, Any]) -> None:
        """处理接收到的钉钉事件。

        Args:
            data: 钉钉事件数据
        """
        headers = data.get("headers", {})
        event_type = headers.get("eventType", "")
        event_id = headers.get("eventId", "")

        # 回复 ACK（钉钉 Stream 协议要求）
        if data.get("code") or event_id:
            ack_body = {
                "code": data.get("code", ""),
                "headers": data.get("headers", {}),
                "message": "OK",
                "data": "ack",
            }
            if self._ws and not self._ws.closed:
                await self._ws.send_json(ack_body)

        # 仅处理消息事件
        if "message" in event_type.lower() or event_type == "":
            payload = data.get("data", data)
            if self.on_message:
                await self.on_message(payload)
