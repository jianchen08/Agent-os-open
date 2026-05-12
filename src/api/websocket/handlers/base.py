"""
WebSocket 消息处理器基类

定义消息处理器的通用接口和上下文
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.loop import AgentLoop

logger = logging.getLogger(__name__)


@dataclass
class HandlerContext:
    """处理器上下文，包含处理消息所需的所有依赖"""

    websocket: WebSocket
    thread_id: str
    user_id: str
    db: AsyncSession
    agent_loop: AgentLoop
    agent_config: Any  # AgentConfig 类型


class BaseHandler(ABC):
    """消息处理器基类"""

    @abstractmethod
    async def handle(
        self, ctx: HandlerContext, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        处理消息

        Args:
            ctx: 处理器上下文
            data: 消息数据

        Returns:
            处理结果，None 表示无需返回
        """
        ...

    @abstractmethod
    def can_handle(self, message_type: str) -> bool:
        """
        判断是否能处理该消息类型

        Args:
            message_type: 消息类型字符串

        Returns:
            是否能处理
        """
        ...
