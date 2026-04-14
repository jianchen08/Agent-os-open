"""消息注入 Input 插件 — 从 MessageQueue 中弹出消息注入到管道状态。

负责在管道循环的输入阶段从消息队列中获取待注入消息，
将消息内容作为 user 角色消息注入到 state["messages"] 前部。

参考 plugins/input/knowledge_inject.py 的插件风格。

State 命名空间：
    - messages: 本插件向 messages 列表前部注入 user 消息
"""

from __future__ import annotations

import logging
from typing import Any

from infrastructure.message_queue import MessageQueue
from pipeline.plugin import IInputPlugin, PluginContext, PluginResult
from pipeline.types import ErrorPolicy

logger = logging.getLogger(__name__)


class MessageInjectPlugin(IInputPlugin):
    """消息注入 Input 插件。

    从 MessageQueue 中按 session_id 弹出消息，将消息内容
    以 user 角色注入到 state["messages"] 前部，使管道
    在下一轮处理时能消费该消息。

    优先级：5（最先执行，在 context_build 之前）
    错误策略：FALLBACK（消息队列不可用时降级为无注入）

    Attributes:
        _config: 插件配置字典
    """

    error_policy = ErrorPolicy.FALLBACK
    fallback_state: dict[str, Any] = {}

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化消息注入插件。

        Args:
            config: 插件配置字典，支持以下键：
                - priority: 插件优先级，默认 5
        """
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "message_inject"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return self._config.get("priority", 5)

    async def execute(self, ctx: PluginContext) -> PluginResult:
        """从消息队列弹出消息并注入到管道状态。

        通过 ctx.get_service("message_queue") 获取队列实例，
        按 ctx.state["session_id"] 弹出最高优先级消息，
        将消息内容以 {"role": "user", "content": ...} 格式
        插入到 state["messages"] 列表前部。

        队列为空时不注入任何内容。消息队列服务不可用时降级处理。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含 messages 更新的插件执行结果
        """
        # 获取消息队列服务
        try:
            queue = ctx.get_service("message_queue")
        except KeyError:
            logger.debug("[%s] No message_queue service, skipping", self.name)
            return PluginResult()

        if not isinstance(queue, MessageQueue):
            logger.warning("[%s] message_queue is not a MessageQueue instance", self.name)
            return PluginResult()

        # 获取 session_id
        session_id = ctx.state.get("session_id")
        if not session_id:
            logger.debug("[%s] No session_id in state, skipping", self.name)
            return PluginResult()

        # 从队列弹出消息
        try:
            message = queue.pop(session_id)
        except Exception as exc:
            logger.error("[%s] Failed to pop message: %s", self.name, exc)
            return PluginResult()

        if message is None:
            logger.debug("[%s] No messages in queue for session %s", self.name, session_id)
            return PluginResult()

        # 构造 user 消息并注入到 messages 前部
        user_msg: dict[str, str] = {
            "role": "user",
            "content": message.content,
        }

        # 获取现有 messages 列表
        messages = ctx.state.get("messages", [])
        if not isinstance(messages, list):
            messages = []

        # 注入到前部
        updated_messages = [user_msg] + list(messages)

        logger.info(
            "[%s] Message injected | session_id=%s | message_id=%s | "
            "target_id=%s | content_len=%d",
            self.name, session_id, message.id,
            message.target_id, len(message.content),
        )

        return PluginResult(state_updates={"messages": updated_messages})
