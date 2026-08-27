"""钉钉通道适配器。

实现 IInputAdapter 和 IOutputAdapter 接口，将钉钉 Stream 消息
适配为管道引擎可用的输入/输出通道。收发骨架来自 channel_common
共享包（QueuedChannelInputAdapter / BufferedChannelOutputAdapter），
本文件只承载钉钉特有的差异点：原始报文解析与 Stream 客户端投递。

采用组合模式（与 WebSocketAdapter 一致）：
- DingTalkInputAdapter: 从钉钉消息队列获取消息
- DingTalkOutputAdapter: 通过钉钉 Stream 客户端发送响应
- DingTalkAdapter: 组合入口，管理生命周期
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from base_combo_adapter import BaseComboAdapter
from input_adapter import QueuedChannelInputAdapter, build_channel_state
from output_adapter import BufferedChannelOutputAdapter
from stream_client import DingTalkStreamClient

logger = logging.getLogger(__name__)


class DingTalkInputAdapter(QueuedChannelInputAdapter):
    """钉钉输入适配器：队列缓冲 + 钉钉报文解析（_raw_to_state）。"""

    @staticmethod
    def _raw_to_state(raw: dict[str, Any]) -> dict[str, Any]:
        """将钉钉原始消息转换为管道 state。

        Args:
            raw: 钉钉消息事件数据

        Returns:
            管道初始 state 字典
        """
        sender_staff_id = raw.get("senderStaffId", "")
        sender_id = raw.get("senderId", "")
        msg_type = raw.get("msgtype", "text")
        conversation_id = raw.get("conversationId", "")

        # 提取文本内容
        user_input = _extract_dingtalk_text(msg_type, raw)
        message_id = raw.get("messageId", uuid.uuid4().hex[:12])

        return build_channel_state(
            channel_type="dingtalk",
            user_input=user_input,
            session_id=message_id,
            channel_user_id=sender_staff_id,
            raw_message=raw,
            _sender_id=sender_id,
            _conversation_id=conversation_id,
        )


class DingTalkOutputAdapter(BufferedChannelOutputAdapter):
    """钉钉输出适配器：经 DingTalkStreamClient 投递文本。

    目标用户标识为钉钉 staff_id。
    """

    channel_name = "dingtalk"

    def __init__(self, stream_client: DingTalkStreamClient) -> None:
        """初始化钉钉输出适配器。

        Args:
            stream_client: 钉钉 Stream 客户端实例
        """
        super().__init__()
        self._stream_client = stream_client

    async def _deliver(self, target: Any, text: str, state: dict[str, Any]) -> None:
        """经钉钉 Stream 客户端投递（纯文本通道，不消费 state）。"""
        await self._stream_client.send_message(target, text)


class DingTalkAdapter(BaseComboAdapter):
    """钉钉通道适配器（组合模式）。

    组合 DingTalkInputAdapter 和 DingTalkOutputAdapter，
    提供钉钉通道的完整输入/输出能力。

    同时负责：
    - 创建和管理 DingTalkStreamClient
    - 将 stream_client 的 on_message 回调连接到 input_adapter 的队列
    - 生命周期管理

    Example::

        adapter = DingTalkAdapter(client_id="xxx", client_secret="secret")
        await adapter.start()
        # ... 使用 adapter.input_adapter / adapter.output_adapter ...
        await adapter.stop()
    """

    def __init__(self, client_id: str, client_secret: str, **kwargs: Any) -> None:
        """初始化钉钉通道适配器。

        Args:
            client_id: 钉钉应用 client_id（AppKey）
            client_secret: 钉钉应用 client_secret（AppSecret）
            **kwargs: 传递给 DingTalkStreamClient 的额外参数
        """
        self.stream_client = DingTalkStreamClient(
            client_id=client_id,
            client_secret=client_secret,
            **kwargs,
        )
        self.input_adapter = DingTalkInputAdapter()
        self.output_adapter = DingTalkOutputAdapter(
            stream_client=self.stream_client,
        )

        # 绑定 stream_client 的消息回调到 input_adapter
        self.stream_client.on_message = self.input_adapter.enqueue_message

    @property
    def channel_type(self) -> str:
        """通道类型标识。"""
        return "dingtalk"

    async def start(self) -> None:
        """启动钉钉适配器：建立连接并开始接收消息。"""
        await self.stream_client.connect()
        logger.info("DingTalk adapter started")

    async def stop(self) -> None:
        """停止钉钉适配器：断开连接。"""
        await self.stream_client.disconnect()
        logger.info("DingTalk adapter stopped")


def _extract_dingtalk_text(msg_type: str, raw: dict[str, Any]) -> str:
    """从钉钉消息中提取文本。

    Args:
        msg_type: 消息类型
        raw: 钉钉原始消息

    Returns:
        提取的纯文本
    """
    if msg_type == "text":
        return raw.get("text", {}).get("content", "")
    if msg_type == "richText":
        return raw.get("richText", {}).get("content", "")
    # 其他类型降级
    return str(raw.get(msg_type, ""))
