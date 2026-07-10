"""
创意生产连接器 - 通用连接器

通用的 HTTP/WebSocket 连接器，可连接任何提供 REST API 的外部创作软件。
适用于视频剪辑软件、音频编辑器、3D 建模工具等。

暴露接口：
- GenericCreativeConnector: 通用创意软件连接器类
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from ..base import BaseConnector
from ..config_mixin import ConfigSubscriberMixin
from ..types import (
    ActionResult,
    ConnectorAction,
    ConnectorContext,
    ConnectorInfo,
    ConnectorState,
)

logger = logging.getLogger(__name__)


class GenericCreativeConnector(BaseConnector, ConfigSubscriberMixin):
    """通用创意软件连接器。

    可配置的通用连接器，通过 HTTP REST API 连接外部创作软件：
    - 视频剪辑软件（DaVinci Resolve、Premiere Pro）
    - 音频编辑器（Audition、Reaper）
    - 3D 建模工具（Blender）
    - 其他支持 HTTP API 的工具

    配置示例：
        connector = GenericCreativeConnector(
            name="Blender",
            endpoint="http://127.0.0.1:8080",
            capabilities=["capture_screenshot", "get_scene_info", "execute_command"],
        )
    """

    def __init__(
        self,
        name: str = "Generic",
        connector_id: str = "generic",
        endpoint: str = "http://127.0.0.1:8080",
        capabilities: list[str] | None = None,
        api_timeout: float = 30.0,
    ) -> None:
        super().__init__()
        self._name = name
        self._connector_id = connector_id
        self._endpoint = endpoint.rstrip("/")
        self._capabilities = capabilities or [
            "capture_screenshot",
            "get_context",
            "execute_command",
        ]
        self._api_timeout = api_timeout
        self._session: aiohttp.ClientSession | None = None

    @property
    def connector_type(self) -> str:
        return f"creative_{self._connector_id}"

    def get_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            connector_type=self.connector_type,
            display_name=self._name,
            capabilities=self._capabilities,
            priority=5,
        )

    async def connect(self) -> None:
        """建立连接。"""
        self._set_state(ConnectorState.CONNECTING)
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._api_timeout),
            )
            async with self._session.get(f"{self._endpoint}/health") as resp:
                if resp.status == 200:
                    self._set_state(ConnectorState.CONNECTED)
                    self._logger.info("%s 连接成功: %s", self._name, self._endpoint)
                else:
                    self._set_state(ConnectorState.ERROR)
        except Exception as e:
            self._set_state(ConnectorState.ERROR)
            self._logger.error("%s 连接失败: %s", self._name, e)

    async def disconnect(self) -> None:
        """断开连接。"""
        self.unsubscribe_config()
        if self._session:
            await self._session.close()
            self._session = None
        self._set_state(ConnectorState.DISCONNECTED)

    async def get_context(self) -> ConnectorContext:
        """获取软件当前上下文。"""
        if not self._session or not self.is_connected:
            return ConnectorContext(metadata={"error": "未连接"})

        try:
            async with self._session.get(f"{self._endpoint}/context") as resp:
                data = await resp.json()
                return ConnectorContext(
                    active_file=data.get("active_file"),
                    selected_text=data.get("selected_text"),
                    metadata=data.get("metadata", {}),
                )
        except Exception as e:
            return ConnectorContext(metadata={"error": str(e)})

    async def execute_action(self, action: ConnectorAction) -> ActionResult:
        """执行操作指令。"""
        if not self._session or not self.is_connected:
            return ActionResult(success=False, error=f"未连接到 {self._name}")

        try:
            if action.action_type == "capture_screenshot":
                return await self._capture_screenshot(action.parameters)
            if action.action_type == "execute_command":
                return await self._execute_command(action.parameters)
            # 通用操作：直接转发到 API
            return await self._forward_action(action)
        except Exception as e:
            return ActionResult(success=False, error=str(e))

    async def _capture_screenshot(self, params: dict[str, Any]) -> ActionResult:
        """截取屏幕截图。"""
        async with self._session.post(
            f"{self._endpoint}/screenshot",
            json=params,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return ActionResult(
                    success=True,
                    data={"image_url": data.get("url", ""), "image_path": data.get("path", "")},
                )
            return ActionResult(success=False, error="截图失败")

    async def _execute_command(self, params: dict[str, Any]) -> ActionResult:
        """执行自定义命令。"""
        async with self._session.post(
            f"{self._endpoint}/command",
            json=params,
        ) as resp:
            data = await resp.json()
            return ActionResult(success=resp.status == 200, data=data)

    async def _forward_action(self, action: ConnectorAction) -> ActionResult:
        """通用操作转发。"""
        async with self._session.post(
            f"{self._endpoint}/action",
            json={
                "action_type": action.action_type,
                "parameters": action.parameters,
                "action_id": action.action_id,
            },
        ) as resp:
            data = await resp.json()
            return ActionResult(success=resp.status == 200, data=data)

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
            "%s 配置变更: event=%s, path=%s",
            self._name,
            event_type,
            file_path,
        )
