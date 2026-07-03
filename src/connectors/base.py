"""
连接器抽象基类

定义所有连接器必须实现的标准接口，包括连接生命周期管理、
上下文获取、操作执行、健康检查和状态变更通知。

暴露接口：
- BaseConnector: 连接器抽象基类
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

from .types import (
    ActionResult,
    ConnectorAction,
    ConnectorContext,
    ConnectorInfo,
    ConnectorState,
)

logger = logging.getLogger(__name__)

# 重连默认配置
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_BASE_RETRY_DELAY: float = 1.0  # 秒


class BaseConnector(ABC):
    """连接器抽象基类。

    所有 IDE 连接器必须继承此类并实现所有抽象方法。
    连接器负责在 Agent OS 和外部 IDE 之间建立双向通信通道。

    提供标准接口：
    - connect()/disconnect(): 连接生命周期
    - health_check(): 健康检查（含指数退避重连）
    - is_connected: 连接状态属性
    - get_context()/execute_action(): 上下文获取和操作执行

    Attributes:
        _state: 当前连接器状态
        _logger: 日志记录器
    """

    def __init__(self) -> None:
        """初始化连接器。"""
        self._state: ConnectorState = ConnectorState.DISCONNECTED
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    @abstractmethod
    def connector_type(self) -> str:
        """连接器类型标识。

        Returns:
            连接器类型字符串，如 "vscode"
        """

    @property
    def is_connected(self) -> bool:
        """连接是否处于活跃状态。

        Returns:
            True 表示连接器处于 CONNECTED 或 ACTIVE 状态
        """
        return self._state in (ConnectorState.CONNECTED, ConnectorState.ACTIVE)

    @property
    def state(self) -> ConnectorState:
        """当前连接器状态。

        Returns:
            当前状态枚举值
        """
        return self._state

    async def health_check(self) -> bool:
        """检查连接器是否健康。

        默认实现基于 is_connected 属性。子类可重写以执行
        更深入的检查（如发送 ping 请求）。

        如果检查失败且连接器处于异常状态，会尝试指数退避重连。

        Returns:
            True 表示连接器可正常工作
        """
        if self.is_connected:
            return True

        # 不健康时尝试重连
        if self._state == ConnectorState.ERROR:
            try:
                await self._reconnect_with_backoff()
                return self.is_connected
            except Exception as e:
                self._logger.warning("健康检查重连失败: %s", e)
                return False

        return False

    async def _reconnect_with_backoff(
        self,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_delay: float = DEFAULT_BASE_RETRY_DELAY,
    ) -> None:
        """指数退避重连。

        在连接异常时尝试重新建立连接，使用指数退避策略
        避免对目标服务造成过大压力。

        Args:
            max_retries: 最大重试次数
            base_delay: 基础延迟（秒），实际延迟为 base_delay * 2^attempt
        """
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                await self.disconnect()
                await self.connect()
                self._logger.info(
                    "重连成功 (尝试 %d/%d)",
                    attempt,
                    max_retries,
                )
                return
            except Exception as e:
                last_error = e
                self._logger.warning(
                    "重连失败 (尝试 %d/%d): %s",
                    attempt,
                    max_retries,
                    e,
                )
                if attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))
                    self._logger.info("等待 %.1f 秒后重试...", delay)
                    await asyncio.sleep(delay)

        msg = f"重连失败，已重试 {max_retries} 次: {last_error}"
        self._logger.error(msg)
        raise ConnectionError(msg)

    @abstractmethod
    async def get_context(self) -> ConnectorContext:
        """获取 IDE 当前上下文。

        从 IDE 获取活动文件、选中文本、光标位置等信息。

        Returns:
            包含 IDE 当前状态的上下文对象
        """

    @abstractmethod
    async def execute_action(self, action: ConnectorAction) -> ActionResult:
        """向 IDE 发送操作指令。

        Args:
            action: 要执行的操作指令

        Returns:
            操作执行结果
        """

    async def on_state_update(self, state: ConnectorState) -> None:
        """连接器状态变更通知。

        当连接器状态发生变化时调用，子类可重写以执行额外逻辑。

        Args:
            state: 新的连接器状态
        """
        self._logger.info(f"连接器状态变更: {self._state.value} -> {state.value}")

    @abstractmethod
    async def connect(self) -> None:
        """建立连接。

        初始化与 IDE 的通信通道，将状态从 DISCONNECTED 变更为 CONNECTED。
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接。

        关闭与 IDE 的通信通道，释放资源，将状态变更为 DISCONNECTED。
        """

    def get_info(self) -> ConnectorInfo:
        """获取连接器描述信息。

        子类可重写以提供更详细的信息。

        Returns:
            连接器描述信息对象
        """
        return ConnectorInfo(
            connector_type=self.connector_type,
            display_name=self.connector_type,
            capabilities=[],
            priority=0,
        )

    def get_status(self) -> dict[str, Any]:
        """获取连接器状态信息。

        Returns:
            包含连接器状态的字典
        """
        return {
            "type": self.connector_type,
            "state": self._state.value,
            "connected": self.is_connected,
            "info": {
                "display_name": self.get_info().display_name,
                "capabilities": self.get_info().capabilities,
                "priority": self.get_info().priority,
            },
        }

    def _set_state(self, state: ConnectorState) -> None:
        """设置连接器状态（内部方法）。

        Args:
            state: 新的连接器状态
        """
        old_state = self._state
        self._state = state
        self._logger.debug(f"状态变更: {old_state.value} -> {state.value}")
