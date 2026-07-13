"""钉钉通道适配器。

实现 IInputAdapter 和 IOutputAdapter 接口，将钉钉 Stream 消息
适配为管道引擎可用的输入/输出通道。

采用组合模式（与 WebSocketAdapter 一致）：
- DingTalkInputAdapter: 从钉钉消息队列获取消息
- DingTalkOutputAdapter: 通过钉钉 Stream 客户端发送响应
- DingTalkAdapter: 组合入口，管理生命周期
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from channels.base_combo_adapter import BaseComboAdapter
from channels.dingtalk.stream_client import DingTalkStreamClient
from channels.input_adapter import IInputAdapter
from channels.output_adapter import IOutputAdapter
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)


class DingTalkInputAdapter(IInputAdapter):
    """钉钉输入适配器。

    从钉钉消息队列中获取消息，转换为管道初始 state。
    使用 asyncio.Queue 作为消息缓冲区。

    Attributes:
        _message_queue: 消息缓冲队列
    """

    def __init__(self) -> None:
        """初始化钉钉输入适配器。"""
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def enqueue_message(self, raw_message: dict[str, Any]) -> None:
        """将钉钉消息放入处理队列。

        由 DingTalkStreamClient 的 on_message 回调调用。

        Args:
            raw_message: 钉钉消息事件数据
        """
        await self._message_queue.put(raw_message)

    async def receive(self) -> dict[str, Any]:
        """从队列中取出下一条钉钉消息，转换为管道初始 state。

        阻塞等待直到有消息可用。

        Returns:
            管道初始 state 字典
        """
        raw_message = await self._message_queue.get()
        return self._raw_to_state(raw_message)

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

        return {
            "user_input": user_input,
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.SESSION_ID: message_id,
            StateKeys.SHOULD_STOP: False,
            "iteration": 1,
            "_channel_type": "dingtalk",
            "_channel_user_id": sender_staff_id,
            "_sender_id": sender_id,
            "_conversation_id": conversation_id,
            "_raw_message": raw,
        }


class DingTalkOutputAdapter(IOutputAdapter):
    """钉钉输出适配器。

    通过钉钉 Stream 客户端发送管道处理结果。

    Attributes:
        _stream_client: 钉钉 Stream 客户端
        _channel_user_id: 当前消息的目标用户 ID
        _accumulated_text: 流式累积的文本
    """

    def __init__(self, stream_client: DingTalkStreamClient) -> None:
        """初始化钉钉输出适配器。

        Args:
            stream_client: 钉钉 Stream 客户端实例
        """
        self._stream_client = stream_client
        self._channel_user_id: str = ""
        self._accumulated_text: str = ""

    def set_channel_user_id(self, user_id: str) -> None:
        """设置当前消息的目标用户 ID。

        Args:
            user_id: 钉钉用户 staff_id
        """
        self._channel_user_id = user_id

    async def send(self, state: dict[str, Any]) -> None:
        """输出管道最终 state 到钉钉。

        Args:
            state: 管道最终 state 字典
        """
        user_id = state.get("_channel_user_id", self._channel_user_id)
        if not user_id:
            logger.warning("No user_id for dingtalk output, skipping")
            return

        # 处理错误
        error = state.get(StateKeys.RAW_ERROR)
        if error:
            await self._stream_client.send_message(user_id, f"❌ 错误: {error}")
            return

        # 发送正常结果
        result = state.get(StateKeys.RAW_RESULT, "")
        if result:
            await self._stream_client.send_message(user_id, str(result))

    async def send_stream(self, chunk: dict[str, Any]) -> None:
        """流式输出 chunk 到钉钉。

        钉钉不完全支持逐 token 流式推送，因此累积文本，
        在流结束时一次性发送。

        Args:
            chunk: 流式数据块
        """
        text = chunk.get("text", "")
        self._accumulated_text += text

        # 如果标记了 flush 或 stream end，发送累积内容
        if chunk.get("flush", False) or chunk.get("type") == "end":  # noqa: SIM102
            if self._channel_user_id and self._accumulated_text:
                await self._stream_client.send_message(self._channel_user_id, self._accumulated_text)
                self._accumulated_text = ""


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
