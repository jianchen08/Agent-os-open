"""
创意生产连接器 - 游戏引擎

通过 HTTP/WebSocket API 连接游戏引擎（如 Unity、Unreal Engine），
实现场景预览、截屏审批、资产同步等功能。

暴露接口：
- GameEngineConnector: 游戏引擎连接器类
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from ..base import BaseConnector
from ..types import (
    ActionResult,
    ConnectorAction,
    ConnectorContext,
    ConnectorInfo,
    ConnectorState,
)

logger = logging.getLogger(__name__)


class GameEngineConnector(BaseConnector):
    """游戏引擎连接器。

    支持与 Unity/Unreal Engine 等 Game Engine 集成：
    - 获取场景截图用于审批
    - 同步资产（模型、贴图、动画）
    - 触发场景预览
    - 执行编辑器命令

    连接方式：通过引擎内嵌的 HTTP Server（插件提供）。
    """

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:9600",
        engine_type: str = "unity",
        api_timeout: float = 30.0,
    ) -> None:
        super().__init__()
        self._endpoint = endpoint.rstrip("/")
        self._engine_type = engine_type
        self._api_timeout = api_timeout
        self._session: aiohttp.ClientSession | None = None

    @property
    def connector_type(self) -> str:
        return f"game_engine_{self._engine_type}"

    def get_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            connector_type=self.connector_type,
            display_name=f"Game Engine ({self._engine_type})",
            capabilities=[
                "capture_screenshot",
                "get_scene_info",
                "list_assets",
                "import_asset",
                "execute_command",
                "get_selection",
                "navigate_to",
                "play_preview",
                "stop_preview",
            ],
            priority=8,
        )

    async def connect(self) -> None:
        """建立与游戏引擎的连接。"""
        self._set_state(ConnectorState.CONNECTING)
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._api_timeout),
            )
            async with self._session.get(f"{self._endpoint}/status") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._set_state(ConnectorState.CONNECTED)
                    self._logger.info(
                        "Game Engine 连接成功: %s (%s)",
                        self._engine_type,
                        data.get("version", "unknown"),
                    )
                else:
                    self._set_state(ConnectorState.ERROR)
        except Exception as e:
            self._set_state(ConnectorState.ERROR)
            self._logger.error("Game Engine 连接失败: %s", e)

    async def disconnect(self) -> None:
        """断开连接。"""
        if self._session:
            await self._session.close()
            self._session = None
        self._set_state(ConnectorState.DISCONNECTED)

    async def get_context(self) -> ConnectorContext:
        """获取引擎当前上下文。"""
        if not self._session or not self.is_connected:
            return ConnectorContext(metadata={"error": "未连接"})

        try:
            async with self._session.get(f"{self._endpoint}/context") as resp:
                data = await resp.json()
                return ConnectorContext(
                    active_file=data.get("active_scene"),
                    selected_text=data.get("selected_object"),
                    metadata={
                        "scene_name": data.get("scene_name"),
                        "engine_version": data.get("engine_version"),
                        "selected_objects": data.get("selected_objects", []),
                    },
                )
        except Exception as e:
            return ConnectorContext(metadata={"error": str(e)})

    async def execute_action(self, action: ConnectorAction) -> ActionResult:  # noqa: PLR0911
        """执行操作指令。"""
        if not self._session or not self.is_connected:
            return ActionResult(success=False, error="未连接到 Game Engine")

        try:
            if action.action_type == "capture_screenshot":
                return await self._capture_screenshot(action.parameters)
            if action.action_type == "get_scene_info":
                return await self._get_scene_info()
            if action.action_type == "list_assets":
                return await self._list_assets(action.parameters)
            if action.action_type == "import_asset":
                return await self._import_asset(action.parameters)
            if action.action_type == "get_selection":
                return await self._get_selection()
            if action.action_type == "execute_command":
                return await self._execute_command(action.parameters)
            if action.action_type == "play_preview":
                return await self._play_preview()
            if action.action_type == "stop_preview":
                return await self._stop_preview()
            return ActionResult(success=False, error=f"不支持的操作: {action.action_type}")
        except Exception as e:
            return ActionResult(success=False, error=str(e))

    async def _capture_screenshot(self, params: dict[str, Any]) -> ActionResult:
        """截取场景截图。"""
        capture_params = {
            "width": params.get("width", 1920),
            "height": params.get("height", 1080),
            "camera": params.get("camera"),
        }
        async with self._session.post(
            f"{self._endpoint}/screenshot",
            json=capture_params,
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return ActionResult(
                    success=True,
                    data={
                        "image_url": data.get("url", ""),
                        "image_path": data.get("path", ""),
                    },
                )
            return ActionResult(success=False, error="截图失败")

    async def _get_scene_info(self) -> ActionResult:
        """获取当前场景信息。"""
        async with self._session.get(f"{self._endpoint}/scene") as resp:
            data = await resp.json()
            return ActionResult(success=True, data=data)

    async def _list_assets(self, params: dict[str, Any]) -> ActionResult:
        """列出资产。"""
        asset_type = params.get("type", "all")
        async with self._session.get(
            f"{self._endpoint}/assets",
            params={"type": asset_type},
        ) as resp:
            data = await resp.json()
            return ActionResult(success=True, data=data)

    async def _import_asset(self, params: dict[str, Any]) -> ActionResult:
        """导入资产。"""
        async with self._session.post(
            f"{self._endpoint}/assets/import",
            json=params,
        ) as resp:
            data = await resp.json()
            return ActionResult(success=resp.status == 200, data=data)

    async def _get_selection(self) -> ActionResult:
        """获取当前选中的对象。"""
        async with self._session.get(f"{self._endpoint}/selection") as resp:
            data = await resp.json()
            return ActionResult(success=True, data=data)

    async def _execute_command(self, params: dict[str, Any]) -> ActionResult:
        """执行编辑器命令。"""
        command = params.get("command", "")
        async with self._session.post(
            f"{self._endpoint}/execute",
            json={"command": command, "args": params.get("args", {})},
        ) as resp:
            data = await resp.json()
            return ActionResult(success=resp.status == 200, data=data)

    async def _play_preview(self) -> ActionResult:
        """开始预览播放。"""
        async with self._session.post(f"{self._endpoint}/play") as resp:
            return ActionResult(success=resp.status == 200)

    async def _stop_preview(self) -> ActionResult:
        """停止预览。"""
        async with self._session.post(f"{self._endpoint}/stop") as resp:
            return ActionResult(success=resp.status == 200)
