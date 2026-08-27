"""多渠道消息网关主入口。

ChannelGateway 是所有通道适配器的统一管理入口，负责：
- 注册和管理通道适配器的生命周期
- 统一消息接入：raw_message → normalize → session → state → pipeline
- 统一响应发送：pipeline result → denormalize → channel send
- 跨通道会话状态共享

消息流:
raw_message → normalize → session_bridge(获取session) →
create_initial_state → pipeline → denormalize → send

Example::

    gateway = ChannelGateway()
    gateway.register_adapter("feishu", feishu_adapter)
    gateway.on_pipeline_request = engine.handle
    await gateway.start()
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from message_normalizer import MessageNormalizer
from session_bridge import SessionBridge
from unified_types import UnifiedResponse

from agentos_plugin_sdk.pipeline_types import StateKeys, create_initial_state

logger = logging.getLogger(__name__)

# 管道请求回调类型：接收 initial_state dict
PipelineRequestCallback = Callable[[dict[str, Any]], Awaitable[None]]


class ChannelGateway:
    """多渠道消息网关。

    管理所有通道适配器的生命周期，提供统一的消息接入和响应发送。

    Example::

        gateway = ChannelGateway()
        gateway.register_adapter("feishu", feishu_adapter)
        gateway.register_adapter("dingtalk", dingtalk_adapter)
        await gateway.start()
    """

    def __init__(
        self,
        session_bridge: SessionBridge | None = None,
        normalizer: MessageNormalizer | None = None,
    ) -> None:
        """初始化消息网关。

        Args:
            session_bridge: 跨通道会话桥接实例（可选，默认自动创建）
            normalizer: 消息标准化器实例（可选，默认自动创建）
        """
        self._adapters: dict[str, Any] = {}
        self._normalizer = normalizer or MessageNormalizer()
        self._session_bridge = session_bridge or SessionBridge()

        # 共享服务字典（由 Application 注入）
        self._services: dict[str, Any] = {}

        # 外部管道请求回调
        self.on_pipeline_request: PipelineRequestCallback | None = None

    @property
    def services(self) -> dict[str, Any]:
        """获取共享服务字典。"""
        return self._services

    @services.setter
    def services(self, value: dict[str, Any]) -> None:
        """设置共享服务字典。

        Args:
            value: 服务名称到实例的映射字典
        """
        self._services = value

    def get_service(self, name: str) -> Any | None:
        """获取已注册的服务实例。

        Args:
            name: 服务名称

        Returns:
            服务实例，未找到返回 None
        """
        return self._services.get(name)

    def register_adapter(self, channel_type: str, adapter: Any) -> None:
        """注册通道适配器。

        Args:
            channel_type: 通道类型标识
            adapter: 适配器实例，需有 start/stop 方法

        Raises:
            ValueError: 重复注册同一通道类型
        """
        if channel_type in self._adapters:
            raise ValueError(f"Channel type '{channel_type}' already registered")
        self._adapters[channel_type] = adapter
        logger.info("Adapter registered: %s", channel_type)

    async def start(self) -> None:
        """启动所有已注册的适配器。"""
        for channel_type, adapter in self._adapters.items():
            try:
                await adapter.start()
                logger.info("Adapter started: %s", channel_type)
            except Exception as exc:
                logger.error("Failed to start adapter %s: %s", channel_type, exc)

    async def stop(self) -> None:
        """停止所有适配器。"""
        for channel_type, adapter in self._adapters.items():
            try:
                await adapter.stop()
                logger.info("Adapter stopped: %s", channel_type)
            except Exception as exc:
                logger.error("Failed to stop adapter %s: %s", channel_type, exc)

    async def handle_message(
        self,
        channel_type: str,
        raw_message: dict[str, Any],
    ) -> dict[str, Any]:
        """统一消息入口。

        将渠道原始消息标准化为 UnifiedMessage，获取/创建会话，
        构建管道初始 state，并通过回调传递给管道引擎。

        失败语义契约（fail-closed）：可预期失败——渠道不支持、管道
        回调未接线——返回 ``{"handled": False, "error": ...}`` 失败值；
        管道回调自身的异常原样传播给调用方。本方法不吞任何异常，
        调用方必须能区分"已处理"与"已丢弃"。

        Args:
            channel_type: 来源通道类型
            raw_message: 渠道原始消息字典

        Returns:
            成功：``{"handled": True, "session_id": ...}``；
            失败：``{"handled": False, "error": <原因>}``
        """
        try:
            # 1. 标准化消息
            unified = self._normalizer.normalize(channel_type, raw_message)
        except ValueError as exc:
            logger.error("Failed to handle message from %s: %s", channel_type, exc)
            return {"handled": False, "error": str(exc)}

        # 2. 获取或创建跨通道会话
        session_id = self._session_bridge.get_or_create_session(
            unified_user_id=unified.unified_user_id,
            channel_type=channel_type,
        )

        # 3. 更新活跃通道
        self._session_bridge.switch_channel(unified.unified_user_id, channel_type)

        # 4. 构建管道初始 state
        initial_state = create_initial_state(
            **{
                "user_input": unified.content,
                StateKeys.SESSION_ID: session_id,
                "_channel_type": unified.channel_type,
                "_channel_user_id": unified.channel_user_id,
                "_unified_user_id": unified.unified_user_id,
                "_message_id": unified.message_id,
                "_raw_message": unified.raw_message,
            }
        )

        logger.info(
            "Message handled: channel=%s, user=%s, session=%s, content_len=%d",
            channel_type,
            unified.unified_user_id,
            session_id,
            len(unified.content),
        )

        # 5. 传递给管道引擎；未接线即丢消息，必须如实上报失败
        if not self.on_pipeline_request:
            logger.warning("No pipeline request handler set, message dropped")
            return {
                "handled": False,
                "error": "pipeline request handler not configured",
            }

        await self.on_pipeline_request(initial_state)
        return {"handled": True, "session_id": session_id}

    async def send_response(self, response: UnifiedResponse) -> dict[str, Any]:
        """发送响应到指定渠道。

        将 UnifiedResponse 反标准化为渠道格式，通过对应适配器发送。

        失败语义契约（fail-closed）：目标渠道无适配器/output 适配器未
        初始化时返回 ``{"sent": False, "error": ...}`` 失败值；底层适配
        器发送失败的异常原样传播。本方法不吞异常，调用方必须能区分
        "已发送"与"未送达"。

        Args:
            response: 统一响应

        Returns:
            成功：``{"sent": True}``；
            失败：``{"sent": False, "error": <原因>}``
        """
        adapter = self._adapters.get(response.channel_type)
        if adapter is None:
            logger.error(
                "No adapter for channel %s, cannot send response",
                response.channel_type,
            )
            return {
                "sent": False,
                "error": f"no adapter registered for channel {response.channel_type}",
            }

        output_adapter = adapter.output_adapter
        if output_adapter is None:
            logger.error(
                "output_adapter 未初始化: channel=%s",
                response.channel_type,
            )
            return {
                "sent": False,
                "error": f"output_adapter not initialized for channel {response.channel_type}",
            }

        # 反标准化后经适配器的 output_adapter 发送；发送失败异常向上传播
        channel_payload = self._normalizer.denormalize(response.channel_type, response)

        state = {
            StateKeys.RAW_RESULT: response.content,
            "_channel_user_id": "",
            "_channel_type": response.channel_type,
            "_response_payload": channel_payload,
            "ended": True,
        }

        await output_adapter.send(state)

        logger.debug(
            "Response sent: channel=%s, type=%s",
            response.channel_type,
            response.content_type,
        )
        return {"sent": True}
