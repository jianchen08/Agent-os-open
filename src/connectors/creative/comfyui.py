"""
创意生产连接器 - ComfyUI

通过 HTTP API 连接 ComfyUI，实现 AI 图像生成工作流的集成。
支持工作流提交、进度监控、结果获取。

暴露接口：
- ComfyUIConnector: ComfyUI 连接器类
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from connectors.base import BaseConnector
from connectors.types import (
    ActionResult,
    ConnectorAction,
    ConnectorContext,
    ConnectorInfo,
    ConnectorState,
)

logger = logging.getLogger(__name__)


class ComfyUIConnector(BaseConnector):
    """ComfyUI 连接器。

    通过 ComfyUI 的 HTTP API 实现：
    - 提交图像生成工作流（prompt）
    - 监控生成进度
    - 获取生成结果（图片）
    - 触发审批流程（生成完成后自动请求审批）

    使用场景：
    - AI 图像/插画生成
    - 概念图生成
    - 风格迁移
    """

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8188",
        api_timeout: float = 30.0,
    ) -> None:
        super().__init__()
        self._endpoint = endpoint.rstrip("/")
        self._api_timeout = api_timeout
        self._session: aiohttp.ClientSession | None = None
        self._pending_jobs: dict[str, dict[str, Any]] = {}

    @property
    def connector_type(self) -> str:
        return "comfyui"

    def get_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            connector_type="comfyui",
            display_name="ComfyUI",
            capabilities=[
                "generate_image",
                "submit_workflow",
                "get_progress",
                "get_result",
                "list_models",
                "list_workflows",
                "capture_screenshot",
            ],
            priority=10,
        )

    async def connect(self) -> None:
        """建立与 ComfyUI 的连接。"""
        self._set_state(ConnectorState.CONNECTING)
        try:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._api_timeout),
            )
            # 健康检查
            async with self._session.get(f"{self._endpoint}/system_stats") as resp:
                if resp.status == 200:
                    self._set_state(ConnectorState.CONNECTED)
                    self._logger.info("ComfyUI 连接成功: %s", self._endpoint)
                else:
                    self._set_state(ConnectorState.ERROR)
                    self._logger.error("ComfyUI 健康检查失败: status=%d", resp.status)
        except Exception as e:
            self._set_state(ConnectorState.ERROR)
            self._logger.error("ComfyUI 连接失败: %s", e)

    async def disconnect(self) -> None:
        """断开与 ComfyUI 的连接。"""
        if self._session:
            await self._session.close()
            self._session = None
        self._set_state(ConnectorState.DISCONNECTED)
        self._logger.info("ComfyUI 已断开")

    async def get_context(self) -> ConnectorContext:
        """获取 ComfyUI 当前状态。"""
        if not self._session or not self.is_connected:
            return ConnectorContext(metadata={"error": "未连接"})

        try:
            async with self._session.get(f"{self._endpoint}/queue") as resp:
                queue_info = await resp.json()
                return ConnectorContext(
                    metadata={
                        "queue_running": queue_info.get("queue_running", []),
                        "queue_pending": queue_info.get("queue_pending", []),
                    },
                )
        except Exception as e:
            self._logger.error("获取 ComfyUI 上下文失败: %s", e)
            return ConnectorContext(metadata={"error": str(e)})

    async def execute_action(self, action: ConnectorAction) -> ActionResult:
        """执行操作指令。"""
        if not self._session or not self.is_connected:
            return ActionResult(success=False, error="未连接到 ComfyUI")

        try:
            if action.action_type == "generate_image":
                return await self._submit_workflow(action.parameters)
            elif action.action_type == "get_progress":
                return await self._get_progress(action.parameters)
            elif action.action_type == "get_result":
                return await self._get_result(action.parameters)
            elif action.action_type == "list_models":
                return await self._list_models()
            elif action.action_type == "capture_screenshot":
                return await self._capture_screenshot()
            else:
                return ActionResult(success=False, error=f"不支持的操作: {action.action_type}")
        except Exception as e:
            self._logger.error("执行操作失败: %s | error: %s", action.action_type, e)
            return ActionResult(success=False, error=str(e))

    async def _submit_workflow(self, params: dict[str, Any]) -> ActionResult:
        """提交图像生成工作流。"""
        workflow = params.get("workflow", {})
        prompt_data = {"prompt": workflow}

        async with self._session.post(
            f"{self._endpoint}/prompt",
            json=prompt_data,
        ) as resp:
            result = await resp.json()
            if resp.status == 200:
                prompt_id = result.get("prompt_id", "")
                self._pending_jobs[prompt_id] = {
                    "status": "running",
                    "progress": 0,
                    "submitted_at": result.get("number", ""),
                }
                return ActionResult(
                    success=True,
                    data={"prompt_id": prompt_id, "status": "submitted"},
                )
            else:
                return ActionResult(
                    success=False,
                    error=result.get("error", {}).get("message", "提交失败"),
                )

    async def _get_progress(self, params: dict[str, Any]) -> ActionResult:
        """获取生成进度。"""
        prompt_id = params.get("prompt_id", "")
        async with self._session.get(f"{self._endpoint}/history/{prompt_id}") as resp:
            history = await resp.json()
            if prompt_id in history:
                outputs = history[prompt_id].get("outputs", {})
                status = history[prompt_id].get("status", {})
                return ActionResult(
                    success=True,
                    data={
                        "prompt_id": prompt_id,
                        "status": status.get("status_str", "unknown"),
                        "completed": status.get("completed", False),
                        "outputs": list(outputs.keys()),
                    },
                )
            return ActionResult(
                success=True,
                data={"prompt_id": prompt_id, "status": "pending", "completed": False},
            )

    async def _get_result(self, params: dict[str, Any]) -> ActionResult:
        """获取生成结果（图片 URL 列表）。"""
        prompt_id = params.get("prompt_id", "")
        async with self._session.get(f"{self._endpoint}/history/{prompt_id}") as resp:
            history = await resp.json()
            if prompt_id not in history:
                return ActionResult(success=False, error="未找到生成记录")

            outputs = history[prompt_id].get("outputs", {})
            images: list[str] = []
            for node_id, node_output in outputs.items():
                for img in node_output.get("images", []):
                    filename = img.get("filename", "")
                    subfolder = img.get("subfolder", "")
                    img_type = img.get("type", "output")
                    images.append(
                        f"{self._endpoint}/view?"
                        f"filename={filename}&subfolder={subfolder}&type={img_type}"
                    )

            return ActionResult(
                success=True,
                data={"prompt_id": prompt_id, "images": images},
            )

    async def _list_models(self) -> ActionResult:
        """列出可用模型。"""
        async with self._session.get(f"{self._endpoint}/object_info") as resp:
            object_info = await resp.json()
            models = {}
            for node_type, info in object_info.items():
                for input_name, input_info in info.get("input", {}).get("required", {}).items():
                    if isinstance(input_info, list) and len(input_info) > 0:
                        first = input_info[0]
                        if isinstance(first, list):
                            models[f"{node_type}.{input_name}"] = first
            return ActionResult(success=True, data={"models": models})

    async def _capture_screenshot(self) -> ActionResult:
        """截取当前画布截图（返回最后生成的图片）。"""
        async with self._session.get(f"{self._endpoint}/history?max_items=1") as resp:
            history = await resp.json()
            if not history:
                return ActionResult(success=False, error="无历史记录")
            last_id = list(history.keys())[0]
            return await self._get_result({"prompt_id": last_id})
