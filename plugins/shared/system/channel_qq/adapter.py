"""QQ 通道适配器。

实现 IInputAdapter 和 IOutputAdapter 接口，将 QQ（OneBot v11 协议）消息
适配为管道引擎可用的输入/输出通道。收发骨架来自 channel_common
共享包（QueuedChannelInputAdapter / BufferedChannelOutputAdapter），
本文件只承载 QQ 特有的差异点：原始报文解析、数字用户号校验与
OneBot API 投递（含 private/group 消息类型路由）。

采用组合模式（与 FeishuAdapter/DingTalkAdapter 一致）：
- QQInputAdapter: 从消息队列获取消息，转换为管道初始 state
- QQOutputAdapter: 通过 OneBot HTTP API 发送响应
- QQAdapter: 组合入口，管理生命周期

消息流：
go-cqhttp → 反向 WS → OneBotClient → on_message → input_adapter 队列 →
receive() → 管道 state → 管道处理 → output_adapter.send() → HTTP API → go-cqhttp
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from base_combo_adapter import BaseComboAdapter
from helpers import _extract_qq_text
from input_adapter import QueuedChannelInputAdapter, build_channel_state
from onebot_client import OneBotClient
from output_adapter import BufferedChannelOutputAdapter

logger = logging.getLogger(__name__)


class QQInputAdapter(QueuedChannelInputAdapter):
    """QQ 输入适配器：队列缓冲 + OneBot v11 报文解析（_raw_to_state）。"""

    @staticmethod
    def _raw_to_state(raw: dict[str, Any]) -> dict[str, Any]:
        """将 QQ 原始消息转换为管道 state。

        支持 OneBot v11 的 Array 格式消息段和 CQ 码字符串格式。

        Args:
            raw: OneBot v11 消息事件数据

        Returns:
            管道初始 state 字典
        """
        user_id = str(raw.get("user_id", ""))
        message_id = str(raw.get("message_id", uuid.uuid4().hex[:12]))
        message_type = raw.get("message_type", "private")
        group_id = raw.get("group_id")

        # 提取文本内容
        user_input = _extract_qq_text(raw)

        extra: dict[str, Any] = {"_message_type": message_type}
        # 群消息额外携带 group_id
        if group_id is not None:
            extra["_group_id"] = group_id

        return build_channel_state(
            channel_type="qq",
            user_input=user_input,
            session_id=message_id,
            channel_user_id=user_id,
            raw_message=raw,
            **extra,
        )


class QQOutputAdapter(BufferedChannelOutputAdapter):
    """QQ 输出适配器：经 OneBot HTTP API 投递文本。

    渠道特有语义：
    - 目标用户号必须为整数，非数字目标跳过投递；
    - 消息类型按来源路由（private/group）：send() 路径优先取
      state 的 _message_type，回退实例属性；stream 路径无 state，
      直接使用实例属性。
    """

    channel_name = "QQ"

    def __init__(self, onebot_client: OneBotClient) -> None:
        """初始化 QQ 输出适配器。

        Args:
            onebot_client: OneBot 客户端实例
        """
        super().__init__()
        self._onebot_client = onebot_client
        self._message_type: str = "private"

    def set_message_type(self, message_type: str) -> None:
        """设置当前消息类型。

        Args:
            message_type: "private" 或 "group"
        """
        self._message_type = message_type

    def _resolve_target(self, raw_user_id: str) -> int | None:
        """OneBot API 要求数字用户号，非数字目标视为无效。

        Args:
            raw_user_id: state 内解析到的原始用户标识

        Returns:
            整数用户号；非数字返回 None（调用方跳过投递）
        """
        try:
            return int(raw_user_id)
        except (ValueError, TypeError):
            logger.warning("Invalid QQ user_id: %s, skipping", raw_user_id)
            return None

    async def _deliver(self, target: Any, text: str, state: dict[str, Any]) -> None:
        """经 OneBot 客户端投递，消息类型按消息来源路由。"""
        msg_type = state.get("_message_type", self._message_type)
        await self._onebot_client.send_message(
            user_id=target,
            content=text,
            message_type=msg_type,
        )


class QQAdapter(BaseComboAdapter):
    """QQ 通道适配器（组合模式）。

    组合 QQInputAdapter 和 QQOutputAdapter，
    提供 QQ 通道的完整输入/输出能力。

    同时负责：
    - 创建和管理 OneBotClient
    - 将 onebot_client 的 on_message 回调连接到 input_adapter 的队列
    - 生命周期管理

    Example::

        adapter = QQAdapter(ws_port=8080, http_api_url="http://localhost:5700")
        await adapter.start()
        # ... 使用 adapter.input_adapter / adapter.output_adapter ...
        await adapter.stop()
    """

    def __init__(
        self,
        ws_host: str = "0.0.0.0",
        ws_port: int = 8080,
        http_api_url: str = "http://127.0.0.1:5700",
        **kwargs: Any,
    ) -> None:
        """初始化 QQ 通道适配器。

        Args:
            ws_host: WebSocket 服务端监听地址
            ws_port: WebSocket 服务端监听端口
            http_api_url: OneBot HTTP API 地址
            **kwargs: 传递给 OneBotClient 的额外参数
        """
        self.stream_client = OneBotClient(
            ws_host=ws_host,
            ws_port=ws_port,
            http_api_url=http_api_url,
            **kwargs,
        )
        self.input_adapter = QQInputAdapter()
        self.output_adapter = QQOutputAdapter(
            onebot_client=self.stream_client,
        )

        # 绑定 stream_client 的消息回调到 input_adapter
        self.stream_client.on_message = self.input_adapter.enqueue_message

    @property
    def channel_type(self) -> str:
        """通道类型标识。"""
        return "qq"

    async def start(self) -> None:
        """启动 QQ 适配器：建立连接并开始接收消息。"""
        await self.stream_client.connect()
        logger.info("QQ adapter started")

    async def stop(self) -> None:
        """停止 QQ 适配器：断开连接。"""
        await self.stream_client.disconnect()
        logger.info("QQ adapter stopped")
