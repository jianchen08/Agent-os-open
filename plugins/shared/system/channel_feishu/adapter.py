"""飞书通道适配器。

实现 IInputAdapter 和 IOutputAdapter 接口，将飞书 Stream 消息
适配为管道引擎可用的输入/输出通道。收发骨架来自 channel_common
共享包（QueuedChannelInputAdapter / BufferedChannelOutputAdapter），
本文件只承载飞书特有的差异点：原始报文解析与 Stream 客户端投递。

采用组合模式（与 WebSocketAdapter 一致）：
- FeishuInputAdapter: 从飞书消息队列获取消息
- FeishuOutputAdapter: 通过飞书 Stream 客户端发送响应
- FeishuAdapter: 组合入口，管理生命周期
"""

from __future__ import annotations

import json  # noqa: PLC0415
import logging
import uuid
from typing import Any

from base_combo_adapter import BaseComboAdapter
from input_adapter import QueuedChannelInputAdapter, build_channel_state
from output_adapter import BufferedChannelOutputAdapter
from stream_client import FeishuStreamClient

logger = logging.getLogger(__name__)


class FeishuInputAdapter(QueuedChannelInputAdapter):
    """飞书输入适配器：队列缓冲 + 飞书信封解析（_raw_to_state）。"""

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

        return build_channel_state(
            channel_type="feishu",
            user_input=user_input,
            session_id=session_id,
            channel_user_id=open_id,
            raw_message=raw,
        )


class FeishuOutputAdapter(BufferedChannelOutputAdapter):
    """飞书输出适配器：经 FeishuStreamClient 投递文本。

    目标用户标识为飞书 open_id。
    """

    channel_name = "feishu"

    def __init__(self, stream_client: FeishuStreamClient) -> None:
        """初始化飞书输出适配器。

        Args:
            stream_client: 飞书 Stream 客户端实例
        """
        super().__init__()
        self._stream_client = stream_client

    async def _deliver(self, target: Any, text: str, state: dict[str, Any]) -> None:
        """经飞书 Stream 客户端投递（纯文本通道，不消费 state）。"""
        await self._stream_client.send_message(target, text)


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
    try:
        parsed = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        return content_str

    if msg_type == "text":
        return parsed.get("text", "")
    return parsed.get("text", content_str)
