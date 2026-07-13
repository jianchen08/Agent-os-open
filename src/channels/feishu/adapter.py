"""飞书通道适配器。

实现 IInputAdapter 和 IOutputAdapter 接口，将飞书 Stream 消息
适配为管道引擎可用的输入/输出通道。

采用组合模式（与 WebSocketAdapter 一致）：
- FeishuInputAdapter: 从飞书消息队列获取消息
- FeishuOutputAdapter: 通过飞书 Stream 客户端发送响应
- FeishuAdapter: 组合入口，管理生命周期
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from channels.base_combo_adapter import BaseComboAdapter
from channels.feishu.stream_client import FeishuStreamClient
from channels.input_adapter import IInputAdapter
from channels.output_adapter import IOutputAdapter
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)


class FeishuInputAdapter(IInputAdapter):
    """飞书输入适配器。

    从飞书消息队列中获取消息，转换为管道初始 state。
    使用 asyncio.Queue 作为消息缓冲区。

    Attributes:
        _message_queue: 消息缓冲队列
    """

    def __init__(self) -> None:
        """初始化飞书输入适配器。"""
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def enqueue_message(self, raw_message: dict[str, Any]) -> None:
        """将飞书消息放入处理队列。

        由 FeishuStreamClient 的 on_message 回调调用。

        Args:
            raw_message: 飞书 im.message.receive_v1 事件数据
        """
        await self._message_queue.put(raw_message)

    async def receive(self) -> dict[str, Any]:
        """从队列中取出下一条飞书消息，转换为管道初始 state。

        阻塞等待直到有消息可用。

        Returns:
            管道初始 state 字典
        """
        raw_message = await self._message_queue.get()
        return self._raw_to_state(raw_message)

    @staticmethod
    def _raw_to_state(raw: dict[str, Any]) -> dict[str, Any]:
        """将飞书原始消息转换为管道 state。

        Args:
            raw: 飞书 im.message.receive_v1 事件数据

        Returns:
            管道初始 state 字典
        """
        event = raw.get("event", raw)
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        open_id = sender_id.get("open_id", "")
        message = event.get("message", {})
        msg_type = message.get("message_type", "text")
        content_str = message.get("content", "{}")

        # 提取文本内容
        user_input = _extract_text(msg_type, content_str)

        session_id = raw.get("header", {}).get("event_id", uuid.uuid4().hex[:12])

        return {
            "user_input": user_input,
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.SESSION_ID: session_id,
            StateKeys.SHOULD_STOP: False,
            "iteration": 1,
            "_channel_type": "feishu",
            "_channel_user_id": open_id,
            "_raw_message": raw,
        }


class FeishuOutputAdapter(IOutputAdapter):
    """飞书输出适配器。

    通过飞书 Stream 客户端发送管道处理结果。

    Attributes:
        _stream_client: 飞书 Stream 客户端
        _channel_user_id: 当前消息的目标用户 ID
        _accumulated_text: 流式累积的文本
    """

    def __init__(self, stream_client: FeishuStreamClient) -> None:
        """初始化飞书输出适配器。

        Args:
            stream_client: 飞书 Stream 客户端实例
        """
        self._stream_client = stream_client
        self._channel_user_id: str = ""
        self._accumulated_text: str = ""

    def set_channel_user_id(self, user_id: str) -> None:
        """设置当前消息的目标用户 ID。

        Args:
            user_id: 飞书用户 open_id
        """
        self._channel_user_id = user_id

    async def send(self, state: dict[str, Any]) -> None:
        """输出管道最终 state 到飞书。

        Args:
            state: 管道最终 state 字典
        """
        user_id = state.get("_channel_user_id", self._channel_user_id)
        if not user_id:
            logger.warning("No user_id for feishu output, skipping")
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
        """流式输出 chunk 到飞书。

        飞书不完全支持逐 token 流式推送，因此累积文本，
        在流结束时一次性发送。如果 chunk 中有 flush 标记则立即发送。

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


class FeishuAdapter(BaseComboAdapter):
    """飞书通道适配器（组合模式）。

    组合 FeishuInputAdapter 和 FeishuOutputAdapter，
    提供飞书通道的完整输入/输出能力。

    同时负责：
    - 创建和管理 FeishuStreamClient
    - 将 stream_client 的 on_message 回调连接到 input_adapter 的队列
    - 生命周期管理

    Example::

        adapter = FeishuAdapter(app_id="cli_xxx", app_secret="secret")
        await adapter.start()
        # ... 使用 adapter.input_adapter / adapter.output_adapter ...
        await adapter.stop()
    """

    def __init__(self, app_id: str, app_secret: str, **kwargs: Any) -> None:
        """初始化飞书通道适配器。

        Args:
            app_id: 飞书应用 app_id
            app_secret: 飞书应用 app_secret
            **kwargs: 传递给 FeishuStreamClient 的额外参数
        """
        self.stream_client = FeishuStreamClient(
            app_id=app_id,
            app_secret=app_secret,
            **kwargs,
        )
        self.input_adapter = FeishuInputAdapter()
        self.output_adapter = FeishuOutputAdapter(
            stream_client=self.stream_client,
        )

        # 绑定 stream_client 的消息回调到 input_adapter
        self.stream_client.on_message = self.input_adapter.enqueue_message

    @property
    def channel_type(self) -> str:
        """通道类型标识。"""
        return "feishu"

    async def start(self) -> None:
        """启动飞书适配器：建立连接并开始接收消息。"""
        await self.stream_client.connect()
        logger.info("Feishu adapter started")

    async def stop(self) -> None:
        """停止飞书适配器：断开连接。"""
        await self.stream_client.disconnect()
        logger.info("Feishu adapter stopped")


def _extract_text(msg_type: str, content_str: str) -> str:
    """从飞书消息内容中提取文本。

    Args:
        msg_type: 消息类型
        content_str: content JSON 字符串

    Returns:
        提取的纯文本
    """
    import json  # noqa: PLC0415

    try:
        parsed = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        return content_str

    if msg_type == "text":
        return parsed.get("text", "")
    return parsed.get("text", content_str)
