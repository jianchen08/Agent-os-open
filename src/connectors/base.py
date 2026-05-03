"""
连接器抽象基类

定义所有连接器必须实现的标准接口，包括连接生命周期管理、
上下文获取、操作执行和状态变更通知。

暴露接口：
- BaseConnector: 连接器抽象基类
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from connectors.types import (
    ActionResult,
    ConnectorAction,
    ConnectorContext,
    ConnectorInfo,
    ConnectorState,
)

logger = logging.getLogger(__name__)


class BaseConnector(ABC):
    """连接器抽象基类。

    所有 IDE 连接器必须继承此类并实现所有抽象方法。
    连接器负责在 Agent OS 和外部 IDE 之间建立双向通信通道。

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

    def _set_state(self, state: ConnectorState) -> None:
        """设置连接器状态（内部方法）。

        Args:
            state: 新的连接器状态
        """
        old_state = self._state
        self._state = state
        self._logger.debug(f"状态变更: {old_state.value} -> {state.value}")
