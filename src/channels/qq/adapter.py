"""QQ 通道适配器。

实现 IInputAdapter 和 IOutputAdapter 接口，将 QQ（OneBot v11 协议）消息
适配为管道引擎可用的输入/输出通道。

采用组合模式（与 FeishuAdapter/DingTalkAdapter 一致）：
- QQInputAdapter: 从消息队列获取消息，转换为管道初始 state
- QQOutputAdapter: 通过 OneBot HTTP API 发送响应（见 output_adapter.py）
- QQAdapter: 组合入口，管理生命周期

消息流：
go-cqhttp → 反向 WS → OneBotClient → on_message → input_adapter 队列 →
receive() → 管道 state → 管道处理 → output_adapter.send() → HTTP API → go-cqhttp
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from channels.base_combo_adapter import BaseComboAdapter
from channels.input_adapter import IInputAdapter
from channels.qq.helpers import _extract_qq_text
from channels.qq.onebot_client import OneBotClient
from channels.qq.output_adapter import QQOutputAdapter  # noqa: F401 re-export
from pipeline.types import StateKeys

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class QQInputAdapter(IInputAdapter):
    """QQ 输入适配器。

    从 QQ 消息队列中获取消息，转换为管道初始 state。
    使用 asyncio.Queue 作为消息缓冲区。

    Attributes:
        _message_queue: 消息缓冲队列
    """

    def __init__(self) -> None:
        """初始化 QQ 输入适配器。"""
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def enqueue_message(self, raw_message: dict[str, Any]) -> None:
        """将 QQ 消息放入处理队列。

        由 OneBotClient 的 on_message 回调调用。

        Args:
            raw_message: OneBot v11 消息事件数据
        """
        await self._message_queue.put(raw_message)

    async def receive(self) -> dict[str, Any]:
        """从队列中取出下一条 QQ 消息，转换为管道初始 state。

        阻塞等待直到有消息可用。

        Returns:
            管道初始 state 字典
        """
        raw_message = await self._message_queue.get()
        return self._raw_to_state(raw_message)

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

        # 使用 message_id 作为 session_id
        session_id = message_id

        state: dict[str, Any] = {
            "user_input": user_input,
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.SESSION_ID: session_id,
            StateKeys.SHOULD_STOP: False,
            "iteration": 1,
            "_channel_type": "qq",
            "_channel_user_id": user_id,
            "_message_type": message_type,
            "_raw_message": raw,
        }

        # 群消息额外携带 group_id
        if group_id is not None:
            state["_group_id"] = group_id

        return state


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
