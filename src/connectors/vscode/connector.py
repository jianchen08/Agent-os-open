"""
VSCode 连接器

实现与 VSCode IDE 的双向通信，支持上下文获取和操作执行。

暴露接口：
- VSCodeConnector: VSCode 连接器实现
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from ..base import BaseConnector
from ..config_mixin import ConfigSubscriberMixin
from ..types import (
    ActionResult,
    ConnectorAction,
    ConnectorContext,
    ConnectorInfo,
    ConnectorState,
)
from .channel import VSCodeChannel

logger = logging.getLogger(__name__)

# 重连配置
MAX_RETRIES: int = 3
BASE_RETRY_DELAY: float = 1.0  # 秒


class VSCodeConnector(BaseConnector, ConfigSubscriberMixin):
    """VSCode 连接器。

    通过 HTTP 短轮询与 VSCode 扩展通信，支持：
    - 获取 VSCode 当前上下文（活动文件、选中文本等）
    - 向 VSCode 发送操作指令（打开文件、显示差异等）
    - 自动重连机制（最多 3 次重试，指数退避）
    - ConfigCenter 配置热加载

    使用方式:
        connector = VSCodeConnector()
        await connector.connect()
        context = await connector.get_context()
        await connector.disconnect()
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 9741,
        timeout: float = 5.0,
    ) -> None:
        """初始化 VSCode 连接器。

        Args:
            host: VSCode 扩展 HTTP 服务地址
            port: VSCode 扩展 HTTP 服务端口
            timeout: 请求超时时间（秒）
        """
        super().__init__()
        self._channel = VSCodeChannel(host=host, port=port, timeout=timeout)
        self._host = host
        self._port = port

    @property
    def connector_type(self) -> str:
        """连接器类型标识。

        Returns:
            "vscode"
        """
        return "vscode"

    @property
    def channel(self) -> VSCodeChannel:
        """获取消息通道（用于测试）。

        Returns:
            消息通道实例
        """
        return self._channel

    async def connect(self) -> None:
        """建立与 VSCode 扩展的连接。

        包含自动重连机制，最多重试 MAX_RETRIES 次，使用指数退避策略。
        """
        if self.is_connected:
            self._logger.warning("连接器已处于连接状态")
            return

        self._set_state(ConnectorState.CONNECTING)

        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                available = self._channel.is_available()
                if available:
                    self._set_state(ConnectorState.CONNECTED)
                    self._logger.info(f"VSCode 连接成功 ({self._host}:{self._port})")
                    return
                msg = f"VSCode 扩展不可用 (尝试 {attempt}/{MAX_RETRIES})"
                raise ConnectionError(msg)
            except Exception as e:
                last_error = e
                self._logger.warning(f"VSCode 连接失败 (尝试 {attempt}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES:
                    delay = BASE_RETRY_DELAY * (2 ** (attempt - 1))
                    self._logger.info(f"等待 {delay:.1f} 秒后重试...")
                    await asyncio.sleep(delay)

        self._set_state(ConnectorState.ERROR)
        msg = f"VSCode 连接失败，已重试 {MAX_RETRIES} 次: {last_error}"
        self._logger.error(msg)
        raise ConnectionError(msg)

    async def disconnect(self) -> None:
        """断开与 VSCode 扩展的连接。"""
        self.unsubscribe_config()

        if self._state == ConnectorState.DISCONNECTED:
            return

        self._set_state(ConnectorState.DISCONNECTING)
        self._set_state(ConnectorState.DISCONNECTED)
        self._logger.info("VSCode 连接已断开")

    async def get_context(self) -> ConnectorContext:
        """获取 VSCode 当前上下文。

        Returns:
            包含活动文件、选中文本、光标位置等的上下文对象
        """
        if not self.is_connected:
            self._logger.warning("连接器未连接，返回空上下文")
            return ConnectorContext()

        try:
            context = await self._channel.listen_for_context()
            self._set_state(ConnectorState.ACTIVE)
            return context
        except Exception as e:
            self._logger.error(f"获取上下文失败: {e}")
            return ConnectorContext()

    async def execute_action(self, action: ConnectorAction) -> ActionResult:
        """向 VSCode 发送操作指令。

        Args:
            action: 要执行的操作指令

        Returns:
            操作执行结果
        """
        if not self.is_connected:
            return ActionResult(
                success=False,
                error="连接器未连接，无法执行操作",
            )

        try:
            if not action.action_id:
                action.action_id = str(uuid.uuid4())

            response = await self._channel.send_request(
                "/action",
                {
                    "action_type": action.action_type,
                    "parameters": action.parameters,
                    "action_id": action.action_id,
                },
            )

            success = response.get("success", False)
            if success:
                self._set_state(ConnectorState.ACTIVE)
                return ActionResult(
                    success=True,
                    data=response.get("data"),
                )
            return ActionResult(
                success=False,
                error=response.get("error", "未知错误"),
            )
        except ConnectionError as e:
            self._set_state(ConnectorState.ERROR)
            return ActionResult(
                success=False,
                error=f"VSCode 连接失败: {str(e)}",
            )
        except Exception as e:
            return ActionResult(
                success=False,
                error=f"执行操作失败: {str(e)}",
            )

    def get_info(self) -> ConnectorInfo:
        """获取 VSCode 连接器描述信息。

        Returns:
            包含类型、显示名称、能力列表和优先级的连接器信息
        """
        return ConnectorInfo(
            connector_type=self.connector_type,
            display_name="Visual Studio Code",
            capabilities=[
                "open_file",
                "open_folder",
                "insert_content",
                "jump_to",
                "show_diff",
                "get_selection",
            ],
            priority=10,
        )

    def _on_config_changed(
        self,
        event_type: str,
        file_path: str,
        context: dict[str, Any],
    ) -> None:
        """配置变更回调：记录日志。

        Args:
            event_type: 事件类型
            file_path: 变更文件路径
            context: 变更上下文
        """
        self._logger.info(
            "VSCode 配置变更: event=%s, path=%s",
            event_type,
            file_path,
        )
