"""QQ 输出适配器。"""

from __future__ import annotations

import logging
from typing import Any

from channels.output_adapter import IOutputAdapter
from channels.qq.onebot_client import OneBotClient
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)


class QQOutputAdapter(IOutputAdapter):
    """QQ 输出适配器。

    通过 OneBot HTTP API 发送管道处理结果。

    Attributes:
        _onebot_client: OneBot 客户端
        _channel_user_id: 当前消息的目标用户 ID
        _message_type: 当前消息类型（private/group）
        _accumulated_text: 流式累积的文本
    """

    def __init__(self, onebot_client: OneBotClient) -> None:
        """初始化 QQ 输出适配器。

        Args:
            onebot_client: OneBot 客户端实例
        """
        self._onebot_client = onebot_client
        self._channel_user_id: str = ""
        self._message_type: str = "private"
        self._accumulated_text: str = ""

    def set_channel_user_id(self, user_id: str) -> None:
        """设置当前消息的目标用户 ID。

        Args:
            user_id: QQ 用户号
        """
        self._channel_user_id = user_id

    def set_message_type(self, message_type: str) -> None:
        """设置当前消息类型。

        Args:
            message_type: "private" 或 "group"
        """
        self._message_type = message_type

    async def send(self, state: dict[str, Any]) -> None:
        """输出管道最终 state 到 QQ。

        Args:
            state: 管道最终 state 字典
        """
        user_id_str = state.get("_channel_user_id", self._channel_user_id)
        if not user_id_str:
            logger.warning("No user_id for QQ output, skipping")
            return

        try:
            user_id = int(user_id_str)
        except (ValueError, TypeError):
            logger.warning("Invalid QQ user_id: %s, skipping", user_id_str)
            return

        msg_type = state.get("_message_type", self._message_type)

        # 处理错误
        error = state.get(StateKeys.RAW_ERROR)
        if error:
            await self._onebot_client.send_message(
                user_id=user_id,
                content=f"❌ 错误: {error}",
                message_type=msg_type,
            )
            return

        # 发送正常结果
        result = state.get(StateKeys.RAW_RESULT, "")
        if result:
            await self._onebot_client.send_message(
                user_id=user_id,
                content=str(result),
                message_type=msg_type,
            )

    async def send_stream(self, chunk: dict[str, Any]) -> None:
        """流式输出 chunk 到 QQ。

        QQ 不支持逐 token 流式推送，因此累积文本，
        在流结束时一次性发送。

        Args:
            chunk: 流式数据块
        """
        text = chunk.get("text", "")
        self._accumulated_text += text

        # 如果标记了 flush 或 stream end，发送累积内容
        should_flush = chunk.get("flush", False) or chunk.get("type") == "end"
        if should_flush and self._channel_user_id and self._accumulated_text:
            try:
                user_id = int(self._channel_user_id)
            except (ValueError, TypeError):
                logger.warning("Invalid QQ user_id for stream: %s", self._channel_user_id)
                return

            await self._onebot_client.send_message(
                user_id=user_id,
                content=self._accumulated_text,
                message_type=self._message_type,
            )
            self._accumulated_text = ""
