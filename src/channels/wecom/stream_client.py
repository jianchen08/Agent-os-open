"""企业微信 HTTP API 客户端。

企业微信使用回调模式（Webhook 接收消息 + HTTP API 发送消息），
不同于飞书/钉钉的 WebSocket 长连接模式。

核心能力：
- HTTP API 发送文本/Markdown 消息
- 自动获取并刷新 access_token（有效期 7200 秒）
- on_message 回调机制（由外部回调 handler 调用）

企业微信 API 参考：
https://developer.work.weixin.qq.com/document/path/90236
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# 类型别名
MessageCallback = Callable[[dict[str, Any]], Awaitable[None]]


class WeComStreamClient:
    """企业微信 HTTP API 客户端。

    通过 HTTP API 发送消息，支持自动 access_token 管理。
    消息接收通过回调 URL 模式，由外部调用 trigger_on_message 触发。

    Example::

        client = WeComStreamClient(
            corp_id="ww123456",
            agent_id=1000001,
            secret="your_secret",
        )
        client.on_message = handle_message
        await client.connect()
        await client.send_message("user_id", "Hello")
    """

    def __init__(
        self,
        corp_id: str,
        agent_id: int,
        secret: str,
        *,
        base_url: str = "https://qyapi.weixin.qq.com",
    ) -> None:
        """初始化企业微信客户端。

        Args:
            corp_id: 企业微信 CorpID
            agent_id: 应用 AgentID
            secret: 应用 Secret
            base_url: 企业微信 API 基础 URL
        """
        self._corp_id = corp_id
        self._agent_id = agent_id
        self._secret = secret
        self._base_url = base_url.rstrip("/")

        self._session: aiohttp.ClientSession | None = None
        self._access_token: str = ""
        self._token_expires: float = 0.0

        # 消息回调（由 adapter 触发）
        self.on_message: MessageCallback | None = None

    @property
    def is_connected(self) -> bool:
        """是否已初始化（HTTP 模式下始终为 True 连接后）。"""
        return self._session is not None and not self._session.closed

    async def connect(self) -> None:
        """初始化 HTTP 会话并获取 access_token。"""
        self._session = aiohttp.ClientSession()
        await self._ensure_token()
        logger.info(
            "WeCom client initialized, corp_id=%s, agent_id=%d",
            self._corp_id,
            self._agent_id,
        )

    async def disconnect(self) -> None:
        """关闭 HTTP 会话。"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("WeCom client disconnected")

    async def trigger_on_message(self, raw_message: dict[str, Any]) -> None:
        """触发 on_message 回调。

        由 WeComAdapter 的 handle_callback 调用，
        将解密后的消息传递给注册的回调函数。

        Args:
            raw_message: 解密后的企业微信消息字典
        """
        if self.on_message:
            await self.on_message(raw_message)
        else:
            logger.warning("No on_message callback registered, message dropped")

    async def send_message(
        self,
        user_id: str,
        content: str,
        msg_type: str = "text",
    ) -> dict[str, Any]:
        """发送消息给企业微信用户。

        Args:
            user_id: 接收用户的 UserID
            content: 消息内容
            msg_type: 消息类型 "text" | "markdown"

        Returns:
            企业微信 API 响应字典

        Raises:
            RuntimeError: 未连接或发送失败
        """
        await self._ensure_token()

        body = self._build_send_body(user_id, content, msg_type)
        url = f"{self._base_url}/cgi-bin/message/send?access_token={self._access_token}"
        headers = {"Content-Type": "application/json"}

        if self._session is None:
            raise RuntimeError("Session not initialized")

        async with self._session.post(url, json=body, headers=headers) as resp:
            result = await resp.json()
            errcode = result.get("errcode", 0)
            if errcode != 0:
                logger.error(
                    "WeCom send message failed: errcode=%s, errmsg=%s",
                    errcode,
                    result.get("errmsg", ""),
                )
            return result

    # ── 内部方法 ──────────────────────────────────────

    def _build_send_body(
        self,
        user_id: str,
        content: str,
        msg_type: str,
    ) -> dict[str, Any]:
        """构建发送消息的请求体。

        Args:
            user_id: 接收用户的 UserID
            content: 消息内容
            msg_type: 消息类型

        Returns:
            企业微信发送消息 API 请求体
        """
        if msg_type == "markdown":
            return {
                "touser": user_id,
                "msgtype": "markdown",
                "agentid": self._agent_id,
                "markdown": {"content": content},
            }
        # 默认 text 类型
        return {
            "touser": user_id,
            "msgtype": "text",
            "agentid": self._agent_id,
            "text": {"content": content},
        }

    async def _ensure_token(self) -> None:
        """确保 access_token 有效，过期则自动刷新。

        access_token 有效期 7200 秒，提前 60 秒刷新。

        Raises:
            RuntimeError: HTTP 会话未初始化
        """
        if self._access_token and time.time() < self._token_expires - 60:
            return

        if self._session is None:
            raise RuntimeError("Session not initialized")

        url = f"{self._base_url}/cgi-bin/gettoken?corpid={self._corp_id}&corpsecret={self._secret}"

        async with self._session.get(url) as resp:
            result = await resp.json()
            errcode = result.get("errcode", 0)
            if errcode != 0:
                logger.error(
                    "WeCom get token failed: errcode=%s, errmsg=%s",
                    errcode,
                    result.get("errmsg", ""),
                )
                raise RuntimeError(f"WeCom get token failed: {result.get('errmsg', 'unknown')}")

            self._access_token = result.get("access_token", "")
            expire = result.get("expires_in", 7200)
            self._token_expires = time.time() + expire
            logger.debug("WeCom token refreshed, expires in %ds", expire)
