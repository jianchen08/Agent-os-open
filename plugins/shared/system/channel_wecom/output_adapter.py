"""企业微信输出适配器。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from pipeline_types import StateKeys
from stream_client import WeComStreamClient

logger = logging.getLogger(__name__)


class IOutputAdapter(ABC):
    """输出适配器抽象基类（与 channel_dingtalk/channel_feishu 同构）。

    负责将管道引擎的处理结果转换为外部系统可识别的响应格式，
    支持一次性输出和流式输出。
    """

    @abstractmethod
    async def send(self, state: dict[str, Any]) -> None:
        """输出管道最终 state。"""

    @abstractmethod
    async def send_stream(self, chunk: dict[str, Any]) -> None:
        """流式输出一个 chunk。"""

    async def health_check(self) -> bool:
        """检查输出适配器是否健康（默认 True）。"""
        return True


class WeComOutputAdapter(IOutputAdapter):
    """企业微信输出适配器。

    通过企业微信 HTTP API 发送管道处理结果。

    Attributes:
        _stream_client: 企业微信客户端
        _channel_user_id: 当前消息的目标用户 ID
        _accumulated_text: 流式累积的文本
    """

    def __init__(self, stream_client: WeComStreamClient) -> None:
        """初始化企业微信输出适配器。

        Args:
            stream_client: 企业微信客户端实例
        """
        self._stream_client = stream_client
        self._channel_user_id: str = ""
        self._accumulated_text: str = ""

    def set_channel_user_id(self, user_id: str) -> None:
        """设置当前消息的目标用户 ID。

        Args:
            user_id: 企业微信用户 UserID
        """
        self._channel_user_id = user_id

    async def send(self, state: dict[str, Any]) -> None:
        """输出管道最终 state 到企业微信。

        Args:
            state: 管道最终 state 字典
        """
        user_id = state.get("_channel_user_id", self._channel_user_id)
        if not user_id:
            logger.warning("No user_id for wecom output, skipping")
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
        """流式输出 chunk 到企业微信。

        企业微信不支持逐 token 流式推送，因此累积文本，
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
