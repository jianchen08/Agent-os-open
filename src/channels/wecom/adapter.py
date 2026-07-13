"""企业微信通道适配器。

实现 IInputAdapter 和 IOutputAdapter 接口，将企业微信回调消息
适配为管道引擎可用的输入/输出通道。

采用组合模式（与 FeishuAdapter/DingTalkAdapter 一致）：
- WeComInputAdapter: 从消息队列获取消息
- WeComOutputAdapter: 通过 HTTP API 发送消息（见 output_adapter.py）
- WeComAdapter: 组合入口，管理生命周期 + 处理回调
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from channels.base_combo_adapter import BaseComboAdapter
from channels.input_adapter import IInputAdapter
from channels.wecom.crypto import WecomCrypto
from channels.wecom.helpers import (
    _extract_encrypt,
    _extract_wecom_text,
    _parse_message_xml,
)
from channels.wecom.output_adapter import WeComOutputAdapter  # noqa: F401 re-export
from channels.wecom.stream_client import WeComStreamClient
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)


class WeComInputAdapter(IInputAdapter):
    """企业微信输入适配器。

    从企业微信消息队列中获取消息，转换为管道初始 state。
    使用 asyncio.Queue 作为消息缓冲区。

    Attributes:
        _message_queue: 消息缓冲队列
    """

    def __init__(self) -> None:
        """初始化企业微信输入适配器。"""
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def enqueue_message(self, raw_message: dict[str, Any]) -> None:
        """将企业微信消息放入处理队列。

        由 WeComStreamClient 的 on_message 回调调用。

        Args:
            raw_message: 企业微信回调消息数据（已解密的 XML 解析结果）
        """
        await self._message_queue.put(raw_message)

    async def receive(self) -> dict[str, Any]:
        """从队列中取出下一条企业微信消息，转换为管道初始 state。

        阻塞等待直到有消息可用。

        Returns:
            管道初始 state 字典
        """
        raw_message = await self._message_queue.get()
        return self._raw_to_state(raw_message)

    @staticmethod
    def _raw_to_state(raw: dict[str, Any]) -> dict[str, Any]:
        """将企业微信原始消息转换为管道 state。

        Args:
            raw: 企业微信消息字典（XML 解析结果）

        Returns:
            管道初始 state 字典
        """
        from_user = raw.get("FromUserName", "")
        to_user = raw.get("ToUserName", "")
        msg_type = raw.get("MsgType", "text")
        content = raw.get("Content", "")
        msg_id = raw.get("MsgId", uuid.uuid4().hex[:12])
        agent_id = raw.get("AgentID", "")

        # 提取文本内容
        user_input = _extract_wecom_text(msg_type, content, raw)

        session_id = str(msg_id)

        return {
            "user_input": user_input,
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.SESSION_ID: session_id,
            StateKeys.SHOULD_STOP: False,
            "iteration": 1,
            "_channel_type": "wecom",
            "_channel_user_id": from_user,
            "_agent_id": agent_id,
            "_to_user": to_user,
            "_raw_message": raw,
        }


class WeComAdapter(BaseComboAdapter):
    """企业微信通道适配器（组合模式）。

    组合 WeComInputAdapter 和 WeComOutputAdapter，
    提供企业微信通道的完整输入/输出能力。

    同时负责：
    - 创建和管理 WeComStreamClient 和 WecomCrypto
    - 将 stream_client 的 on_message 回调连接到 input_adapter 的队列
    - 处理回调 URL 验证和消息解密
    - 生命周期管理

    Example::

        adapter = WeComAdapter(
            corp_id="ww123456",
            agent_id=1000001,
            secret="your_secret",
            token="your_token",
            encoding_aes_key="your_aes_key",
        )
        await adapter.start()
        # 处理回调: result = await adapter.handle_callback(...)
        await adapter.stop()
    """

    def __init__(
        self,
        corp_id: str,
        agent_id: int,
        secret: str,
        token: str,
        encoding_aes_key: str,
        **kwargs: Any,
    ) -> None:
        """初始化企业微信通道适配器。

        Args:
            corp_id: 企业微信 CorpID
            agent_id: 应用 AgentID
            secret: 应用 Secret
            token: 回调配置 Token
            encoding_aes_key: 回调配置 EncodingAESKey
            **kwargs: 传递给 WeComStreamClient 的额外参数
        """
        self.stream_client = WeComStreamClient(
            corp_id=corp_id,
            agent_id=agent_id,
            secret=secret,
            **kwargs,
        )
        self.crypto = WecomCrypto(
            token=token,
            encoding_aes_key=encoding_aes_key,
            corp_id=corp_id,
        )
        self.input_adapter = WeComInputAdapter()
        self.output_adapter = WeComOutputAdapter(
            stream_client=self.stream_client,
        )

        # 绑定 stream_client 的消息回调到 input_adapter
        self.stream_client.on_message = self.input_adapter.enqueue_message

    @property
    def channel_type(self) -> str:
        """通道类型标识。"""
        return "wecom"

    async def start(self) -> None:
        """启动企业微信适配器：初始化 HTTP 客户端并获取 access_token。"""
        await self.stream_client.connect()
        logger.info("WeCom adapter started")

    async def stop(self) -> None:
        """停止企业微信适配器：关闭 HTTP 客户端。"""
        await self.stream_client.disconnect()
        logger.info("WeCom adapter stopped")

    async def handle_callback(
        self,
        timestamp: str,
        nonce: str,
        msg_signature: str,
        body: str,
    ) -> str:
        """处理企业微信回调请求。

        同时处理：
        - GET 请求（验证回调 URL）
        - POST 请求（接收消息）

        Args:
            timestamp: 回调请求的时间戳
            nonce: 回调请求的随机字符串
            msg_signature: 回调请求的签名
            body: 回调请求体（加密的 XML）

        Returns:
            解密后的消息内容（用于验证）或空字符串
        """
        # 解析 XML 获取加密内容
        encrypt_content = _extract_encrypt(body)

        # 验证签名
        if not self.crypto.verify_signature(timestamp, nonce, encrypt_content, msg_signature):
            logger.warning("WeCom callback signature verification failed")
            return ""

        # 判断是验证回调 URL 还是接收消息
        try:
            decrypted_xml = self.crypto.decrypt_message(body)
        except ValueError:
            logger.warning("WeCom callback decrypt failed")
            return ""

        # 解析解密后的 XML
        message = _parse_message_xml(decrypted_xml)
        if not message:
            return decrypted_xml

        # 触发消息回调
        await self.stream_client.trigger_on_message(message)

        return decrypted_xml

    async def handle_verify_url(
        self,
        timestamp: str,
        nonce: str,
        msg_signature: str,
        echostr: str,
    ) -> str:
        """处理验证回调 URL 的 GET 请求。

        Args:
            timestamp: 请求时间戳
            nonce: 随机字符串
            msg_signature: 消息签名
            echostr: 加密的验证字符串

        Returns:
            解密后的明文 echostr
        """
        if not self.crypto.verify_signature(timestamp, nonce, echostr, msg_signature):
            logger.warning("WeCom verify URL signature failed")
            return ""

        return self.crypto.decrypt_echo(echostr)
