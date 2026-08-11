"""
创意生产连接器 - ComfyUI

通过 HTTP API 和 WebSocket 连接 ComfyUI，实现 AI 图像生成工作流的集成。
支持工作流提交、进度监控（WebSocket 实时推送）、结果获取、任务中断。

暴露接口：
- ComfyUIConnector: ComfyUI 连接器类
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
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

# 进度回调类型：接收消息类型和消息数据的可调用对象
ProgressCallback = Callable[[str, dict[str, Any]], None]

# 默认工作流模板目录（相对于项目根目录）
_DEFAULT_WORKFLOW_DIR = Path(__file__).parent.parent.parent.parent / "config" / "media_workflows"


class ComfyUIConnector(BaseConnector):
    """ComfyUI 连接器。

    通过 ComfyUI 的 HTTP API 和 WebSocket 实现：
    - 提交图像生成工作流（prompt）
    - WebSocket 实时监控生成进度
    - 获取生成结果（图片）
    - 中断任务、清空队列
    - 列出可用模型和工作流模板

    使用场景：
    - AI 图像/插画生成
    - 概念图生成
    - 风格迁移
    """

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8188",
        api_timeout: float = 30.0,
        workflow_dir: Path | str | None = None,
    ) -> None:
        """初始化 ComfyUI 连接器。

        Args:
            endpoint: ComfyUI 服务地址
            api_timeout: HTTP 请求超时时间（秒）
            workflow_dir: 工作流模板目录路径，为 None 则使用默认路径
        """
        super().__init__()
        self._endpoint = endpoint.rstrip("/")
        self._api_timeout = api_timeout
        self._session: aiohttp.ClientSession | None = None
        self._pending_jobs: dict[str, dict[str, Any]] = {}

        # WebSocket 相关
        self._ws_session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_client_id: str = uuid.uuid4().hex
        self._ws_listener_task: asyncio.Task[None] | None = None
        self._progress_callbacks: list[ProgressCallback] = []

        # 工作流模板目录
        if workflow_dir is not None:
            self._workflow_dir = Path(workflow_dir)
        else:
            self._workflow_dir = _DEFAULT_WORKFLOW_DIR

    @property
    def connector_type(self) -> str:
        """连接器类型标识。"""
        return "comfyui"

    @property
    def endpoint(self) -> str:
        """ComfyUI 服务地址。"""
        return self._endpoint

    def get_info(self) -> ConnectorInfo:
        """获取连接器描述信息。"""
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
                "interrupt_task",
                "clear_queue",
                "ws_progress",
            ],
            priority=10,
        )

    # ================================================================
    # 进度回调管理
    # ================================================================

    def add_progress_callback(self, callback: ProgressCallback) -> None:
        """注册进度回调函数。

        当 WebSocket 接收到进度消息时，会调用所有已注册的回调。

        Args:
            callback: 回调函数，签名为 (msg_type: str, data: dict) -> None
        """
        self._progress_callbacks.append(callback)

    def remove_progress_callback(self, callback: ProgressCallback) -> None:
        """移除进度回调函数。

        Args:
            callback: 要移除的回调函数
        """
        if callback in self._progress_callbacks:
            self._progress_callbacks.remove(callback)

    def _notify_progress(self, msg_type: str, data: dict[str, Any]) -> None:
        """通知所有已注册的进度回调。

        Args:
            msg_type: 消息类型（execution_start, progress, executing, executed 等）
            data: 消息数据
        """
        for callback in self._progress_callbacks:
            try:
                callback(msg_type, data)
            except Exception:
                self._logger.debug("进度回调执行异常", exc_info=True)

    # ================================================================
    # 连接管理
    # ================================================================

    async def connect(self) -> None:
        """建立与 ComfyUI 的 HTTP 连接。"""
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
        """断开与 ComfyUI 的连接（包括 WebSocket）。"""
        await self.stop_ws_listener()
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

    # ================================================================
    # WebSocket 进度监听
    # ================================================================

    @property
    def ws_url(self) -> str:
        """WebSocket 连接地址。"""
        host = self._endpoint.replace("http://", "").replace("https://", "")
        return f"ws://{host}/ws?clientId={self._ws_client_id}"

    async def start_ws_listener(self) -> None:
        """启动 WebSocket 进度监听。

        建立 WebSocket 连接并启动后台监听任务，
        接收 ComfyUI 推送的进度消息（execution_start, progress, executing, executed 等），
        并通过进度回调通知上层。
        """
        if self._ws_listener_task is not None and not self._ws_listener_task.done():
            self._logger.warning("WebSocket 监听已在运行")
            return

        self._ws_session = aiohttp.ClientSession()
        self._ws_listener_task = asyncio.create_task(self._ws_listen_loop(), name="comfyui-ws-listener")
        self._logger.info("WebSocket 监听已启动: %s", self.ws_url)

    async def stop_ws_listener(self) -> None:
        """停止 WebSocket 进度监听。"""
        if self._ws_listener_task is not None:
            self._ws_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_listener_task
            self._ws_listener_task = None

        if self._ws is not None:
            await self._ws.close()
            self._ws = None

        if self._ws_session is not None:
            await self._ws_session.close()
            self._ws_session = None

        self._logger.info("WebSocket 监听已停止")

    async def _ws_listen_loop(self) -> None:
        """WebSocket 监听循环，持续接收并分发消息。"""
        try:
            self._ws = await self._ws_session.ws_connect(self.ws_url)
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        msg_type = data.get("type", "")
                        msg_data = data.get("data", {})
                        self._notify_progress(msg_type, msg_data)
                    except json.JSONDecodeError:
                        self._logger.debug("WebSocket 消息 JSON 解析失败: %s", msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._logger.error("WebSocket 监听异常: %s", e)

    # ================================================================
    # 操作执行
    # ================================================================

    async def execute_action(self, action: ConnectorAction) -> ActionResult:  # noqa: PLR0911
        """执行操作指令。"""
        if not self._session or not self.is_connected:
            return ActionResult(success=False, error="未连接到 ComfyUI")

        try:
            if action.action_type == "generate_image":
                return await self._submit_workflow(action.parameters)
            if action.action_type == "get_progress":
                return await self._get_progress(action.parameters)
            if action.action_type == "get_result":
                return await self._get_result(action.parameters)
            if action.action_type == "list_models":
                return await self._list_models()
            if action.action_type == "capture_screenshot":
                return await self._capture_screenshot()
            if action.action_type == "interrupt_task":
                return await self._interrupt_task()
            if action.action_type == "clear_queue":
                return await self._clear_queue()
            if action.action_type == "list_workflows":
                return self._list_workflow_templates()
            return ActionResult(success=False, error=f"不支持的操作: {action.action_type}")
        except Exception as e:
            self._logger.error("执行操作失败: %s | error: %s", action.action_type, e)
            return ActionResult(success=False, error=str(e))

    # ================================================================
    # 工作流操作
    # ================================================================

    async def _submit_workflow(self, params: dict[str, Any]) -> ActionResult:
        """提交图像生成工作流。

        Args:
            params: 包含 workflow 字典的参数

        Returns:
            提交结果，包含 prompt_id
        """
        workflow = params.get("workflow", {})
        prompt_data = {"prompt": workflow, "client_id": self._ws_client_id}

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
            return ActionResult(
                success=False,
                error=result.get("error", {}).get("message", "提交失败"),
            )

    async def _get_progress(self, params: dict[str, Any]) -> ActionResult:
        """获取生成进度。

        Args:
            params: 包含 prompt_id 的参数

        Returns:
            进度信息
        """
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
        """获取生成结果（图片 URL 列表）。

        Args:
            params: 包含 prompt_id 的参数

        Returns:
            图片 URL 列表
        """
        prompt_id = params.get("prompt_id", "")
        async with self._session.get(f"{self._endpoint}/history/{prompt_id}") as resp:
            history = await resp.json()
            if prompt_id not in history:
                return ActionResult(success=False, error="未找到生成记录")

            outputs = history[prompt_id].get("outputs", {})
            images: list[str] = []
            for _node_id, node_output in outputs.items():
                for img in node_output.get("images", []):
                    filename = img.get("filename", "")
                    subfolder = img.get("subfolder", "")
                    img_type = img.get("type", "output")
                    images.append(f"{self._endpoint}/view?filename={filename}&subfolder={subfolder}&type={img_type}")

            return ActionResult(
                success=True,
                data={"prompt_id": prompt_id, "images": images},
            )

    async def _list_models(self) -> ActionResult:
        """列出可用模型。

        Returns:
            按节点类型和输入名分组的模型列表
        """
        async with self._session.get(f"{self._endpoint}/object_info") as resp:
            object_info = await resp.json()
            models: dict[str, list[str]] = {}
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

    async def _interrupt_task(self) -> ActionResult:
        """中断当前正在执行的任务。

        Returns:
            操作结果
        """
        async with self._session.post(f"{self._endpoint}/interrupt") as resp:
            if resp.status in (200, 204):
                return ActionResult(success=True, data={"message": "任务已中断"})
            text = await resp.text()
            return ActionResult(success=False, error=f"中断失败: {text}")

    async def _clear_queue(self) -> ActionResult:
        """清空任务队列。

        Returns:
            操作结果
        """
        async with self._session.post(
            f"{self._endpoint}/queue",
            json={"delete": []},
        ) as resp:
            if resp.status in (200, 204):
                return ActionResult(success=True, data={"message": "队列已清空"})
            text = await resp.text()
            return ActionResult(success=False, error=f"清空队列失败: {text}")

    def _list_workflow_templates(self) -> ActionResult:
        """列出工作流模板目录中的所有模板。

        Returns:
            模板名称列表
        """
        templates: list[str] = []
        if self._workflow_dir.exists():
            for f in self._workflow_dir.glob("*.json"):
                templates.append(f.stem)
        templates.sort()
        return ActionResult(success=True, data={"templates": templates})

    def load_workflow_template(self, name: str) -> dict[str, Any]:
        """加载指定名称的工作流模板。

        Args:
            name: 模板名称（不含 .json 扩展名）

        Returns:
            工作流字典

        Raises:
            FileNotFoundError: 模板不存在
        """
        template_path = self._workflow_dir / f"{name}.json"
        if not template_path.exists():
            raise FileNotFoundError(f"工作流模板不存在: {name}")
        content = template_path.read_text(encoding="utf-8")
        return json.loads(content)
