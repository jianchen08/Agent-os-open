"""飞书 Stream 模式客户端。

基于飞书 Stream 协议通过 WebSocket 长连接接收消息事件，
并通过 HTTP API 发送消息。不依赖飞书官方 SDK，使用 aiohttp 实现。

飞书 Stream 协议参考：
https://open.feishu.cn/document/server-docs/event-subscription-guide/stream-mode

核心能力：
- WebSocket 长连接接收事件
- HTTP API 发送文本/卡片消息
- 自动重连（指数退避）
- on_message 回调机制
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# 类型别名
MessageCallback = Callable[[dict[str, Any]], Awaitable[None]]


class FeishuStreamClient:
    """飞书 Stream 模式客户端。

    通过 WebSocket 长连接接收飞书事件，通过 HTTP API 发送消息。
    支持自动重连和 on_message 回调。

    Example::

        client = FeishuStreamClient(app_id="cli_xxx", app_secret="secret")
        client.on_message = handle_message
        await client.connect()
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        base_url: str = "https://open.feishu.cn",
        max_retries: int = 5,
        base_delay: float = 1.0,
    ) -> None:
        """初始化飞书 Stream 客户端。

        Args:
            app_id: 飞书应用 app_id
            app_secret: 飞书应用 app_secret
            base_url: 飞书 API 基础 URL
            max_retries: 最大重连次数
            base_delay: 重连基础延迟（秒）
        """
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._base_delay = base_delay

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._tenant_token: str = ""
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

        获取 tenant_access_token → 获取 WebSocket endpoint → 建立连接 → 开始接收循环。
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
                logger.info("Feishu stream connected")
                retry_count = 0  # 连接成功，重置计数

                # 启动接收循环
                await self._receive_loop()

            except Exception as exc:
                retry_count += 1
                delay = self._base_delay * (2 ** min(retry_count - 1, 5))
                logger.warning(
                    "Feishu stream error (retry %d/%d): %s, retrying in %.1fs",
                    retry_count,
                    self._max_retries,
                    exc,
                    delay,
                )
                if self._running and retry_count < self._max_retries:
                    await asyncio.sleep(delay)
                else:
                    break

        logger.info("Feishu stream client stopped")

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

        logger.info("Feishu stream client disconnected")

    async def send_message(
        self,
        user_id: str,
        content: str,
        msg_type: str = "text",
    ) -> dict[str, Any]:
        """发送消息给飞书用户。

        Args:
            user_id: 接收用户的 open_id
            content: 消息内容
            msg_type: 消息类型 "text" | "interactive"

        Returns:
            飞书 API 响应字典

        Raises:
            RuntimeError: 未连接或发送失败
        """
        await self._ensure_token()

        body: dict[str, Any] = {
            "receive_id": user_id,
            "msg_type": msg_type,
            "content": json.dumps({"text": content}) if msg_type == "text" else content,
        }

        url = f"{self._base_url}/open-apis/im/v1/messages?receive_id_type=open_id"
        headers = {
            "Authorization": f"Bearer {self._tenant_token}",
            "Content-Type": "application/json",
        }

        if self._session is None:
            raise RuntimeError("Session not initialized")

        async with self._session.post(url, json=body, headers=headers) as resp:
            result = await resp.json()
            if result.get("code") != 0:
                logger.error(
                    "Feishu send message failed: code=%s, msg=%s",
                    result.get("code"),
                    result.get("msg"),
                )
            return result

    async def send_card(self, user_id: str, card_config: dict[str, Any]) -> dict[str, Any]:
        """发送卡片消息给飞书用户。

        Args:
            user_id: 接收用户的 open_id
            card_config: 卡片配置字典

        Returns:
            飞书 API 响应字典
        """
        await self._ensure_token()

        body = {
            "receive_id": user_id,
            "msg_type": "interactive",
            "content": json.dumps(card_config),
        }

        url = f"{self._base_url}/open-apis/im/v1/messages?receive_id_type=open_id"
        headers = {
            "Authorization": f"Bearer {self._tenant_token}",
            "Content-Type": "application/json",
        }

        if self._session is None:
            raise RuntimeError("Session not initialized")

        async with self._session.post(url, json=body, headers=headers) as resp:
            result = await resp.json()
            if result.get("code") != 0:
                logger.error(
                    "Feishu send card failed: code=%s, msg=%s",
                    result.get("code"),
                    result.get("msg"),
                )
            return result

    # ── 内部方法 ──────────────────────────────────────

    async def _ensure_token(self) -> None:
        """确保 tenant_access_token 有效。"""
        if self._tenant_token and time.time() < self._token_expires - 60:
            return

        if self._session is None:
            raise RuntimeError("Session not initialized")

        url = f"{self._base_url}/open-apis/auth/v3/tenant_access_token/internal"
        body = {
            "app_id": self._app_id,
            "app_secret": self._app_secret,
        }

        async with self._session.post(url, json=body) as resp:
            result = await resp.json()
            self._tenant_token = result.get("tenant_access_token", "")
            expire = result.get("expire", 7200)
            self._token_expires = time.time() + expire
            logger.debug("Feishu token refreshed, expires in %ds", expire)

    async def _get_endpoint(self) -> str:
        """获取 Stream WebSocket endpoint。

        Returns:
            WebSocket 连接 URL
        """
        await self._ensure_token()

        if self._session is None:
            raise RuntimeError("Session not initialized")

        url = f"{self._base_url}/open-apis/callback/ws/endpoint"
        headers = {"Authorization": f"Bearer {self._tenant_token}"}

        async with self._session.post(url, headers=headers) as resp:
            result = await resp.json()
            endpoint = result.get("data", {}).get("endpoint", "")
            if not endpoint:
                logger.error("No stream endpoint in response: %s", result)
            return endpoint

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
                logger.warning("Feishu stream connection closed/error")
                break

        if self._running:
            logger.info("Feishu stream disconnected, will reconnect")

    async def _handle_event(self, data: dict[str, Any]) -> None:
        """处理接收到的飞书事件。

        Args:
            data: 飞书事件数据
        """
        # 飞书 Stream 协议：需要回复 ACK
        headers = data.get("headers", {})
        event_type = headers.get("event_type", "")
        headers.get("message_id", "")

        # 回复 ACK
        if data.get("schema") == "2.0":
            ack_body = {"schema": "2.0", "header": data.get("header", {})}
            if self._ws and not self._ws.closed:
                await self._ws.send_json(ack_body)

        # 仅处理消息事件
        if "im.message.receive_v1" in event_type or event_type == "":
            payload = data.get("data", data)
            if self.on_message:
                await self.on_message(payload)
