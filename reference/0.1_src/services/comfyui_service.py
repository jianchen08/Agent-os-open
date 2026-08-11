"""
ComfyUI 服务层

编排 ComfyUIConnector 和 ComfyUIProvider 的能力，提供统一的业务接口：
- 连接管理（连接/断开）
- 工作流模板管理（列出/获取/保存/删除）
- 模型管理（列出可用模型）
- 生成任务（提交工作流、WebSocket 进度监听、获取结果）
- 生成历史（记录每次生成的参数、状态、结果）

暴露接口：
- ComfyUIService: ComfyUI 服务类
- get_comfyui_service: 获取全局单例
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from connectors.creative.comfyui import ComfyUIConnector
from services.comfyui_history import (
    GenerationHistory,
    GenerationRecord,
    GenerationStatus,
)
from tools.interfaces import ProgressCallback

logger = logging.getLogger(__name__)

_DEFAULT_WORKFLOW_DIR = Path(__file__).parent.parent.parent / "config" / "media_workflows"
_DEFAULT_HISTORY_PATH = "data/comfyui_history.json"


class ComfyUIService:
    """ComfyUI 业务服务。

    编排连接器和 Provider 的能力，提供面向 API 层的统一接口。
    通过模块级单例管理，避免多处实例化。

    Attributes:
        connector: ComfyUI 连接器实例
        history: 生成历史管理器
    """

    def __init__(
        self,
        workflow_dir: Path | str | None = None,
        history_path: str | Path | None = None,
    ) -> None:
        """初始化 ComfyUI 服务。

        Args:
            workflow_dir: 工作流模板目录，为 None 使用默认路径
            history_path: 历史记录文件路径，为 None 使用默认路径
        """
        self._connector: ComfyUIConnector | None = None
        self._history = GenerationHistory(history_path or _DEFAULT_HISTORY_PATH)
        self._workflow_dir = Path(workflow_dir) if workflow_dir else _DEFAULT_WORKFLOW_DIR
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_progress: dict[str, int] = {}

    @property
    def connector(self) -> ComfyUIConnector | None:
        """当前连接器实例。"""
        return self._connector

    @property
    def history(self) -> GenerationHistory:
        """生成历史管理器。"""
        return self._history

    # ================================================================
    # 连接管理
    # ================================================================

    async def connect(self, endpoint: str) -> dict[str, Any]:
        """连接到 ComfyUI 服务。

        创建新的连接器实例并建立连接。如果已有连接则先断开。

        Args:
            endpoint: ComfyUI 服务地址

        Returns:
            连接状态信息
        """
        if self._connector is not None:
            await self.disconnect()

        self._connector = ComfyUIConnector(
            endpoint=endpoint,
            workflow_dir=self._workflow_dir,
        )
        await self._connector.connect()

        if not self._connector.is_connected:
            error_msg = f"无法连接到 ComfyUI: {endpoint}"
            self._connector = None
            return {"connected": False, "error": error_msg}

        await self._connector.start_ws_listener()

        return {
            "connected": True,
            "endpoint": endpoint,
            "state": self._connector.state.value,
        }

    async def disconnect(self) -> dict[str, Any]:
        """断开与 ComfyUI 的连接。

        Returns:
            断开状态信息
        """
        if self._connector is None:
            return {"connected": False, "message": "未连接"}

        for _task_id, task in list(self._running_tasks.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._running_tasks.clear()
        self._task_progress.clear()

        await self._connector.disconnect()
        self._connector = None

        return {"connected": False, "message": "已断开"}

    def get_status(self) -> dict[str, Any]:
        """获取当前连接状态。

        Returns:
            状态信息字典
        """
        if self._connector is None:
            return {"connected": False, "endpoint": None, "state": "disconnected"}
        return {
            "connected": self._connector.is_connected,
            "endpoint": self._connector.endpoint,
            "state": self._connector.state.value,
        }

    def _require_connector(self) -> ComfyUIConnector:
        """获取连接器，未连接时抛出异常。"""
        if self._connector is None or not self._connector.is_connected:
            raise RuntimeError("未连接到 ComfyUI，请先调用 connect")
        return self._connector

    # ================================================================
    # 工作流模板管理
    # ================================================================

    def list_workflows(self) -> list[dict[str, Any]]:
        """列出所有工作流模板。

        Returns:
            模板信息列表
        """
        templates: list[dict[str, Any]] = []
        if not self._workflow_dir.exists():
            return templates
        for f in sorted(self._workflow_dir.glob("*.json")):
            templates.append({"name": f.stem, "file_path": str(f)})
        return templates

    def get_workflow(self, name: str) -> dict[str, Any]:
        """获取指定工作流模板详情。

        Args:
            name: 模板名称

        Returns:
            模板内容字典

        Raises:
            FileNotFoundError: 模板不存在
        """
        template_path = self._workflow_dir / f"{name}.json"
        if not template_path.exists():
            raise FileNotFoundError(f"工作流模板不存在: {name}")
        content = template_path.read_text(encoding="utf-8")
        return json.loads(content)

    def save_workflow(self, name: str, workflow: dict[str, Any]) -> dict[str, Any]:
        """保存自定义工作流模板。

        Args:
            name: 模板名称
            workflow: 工作流定义

        Returns:
            保存结果
        """
        self._workflow_dir.mkdir(parents=True, exist_ok=True)
        template_path = self._workflow_dir / f"{name}.json"
        template_path.write_text(
            json.dumps(workflow, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("保存工作流模板: %s", template_path)
        return {"name": name, "file_path": str(template_path)}

    def delete_workflow(self, name: str) -> bool:
        """删除自定义工作流模板。

        Args:
            name: 模板名称

        Returns:
            是否删除成功

        Raises:
            FileNotFoundError: 模板不存在
        """
        template_path = self._workflow_dir / f"{name}.json"
        if not template_path.exists():
            raise FileNotFoundError(f"工作流模板不存在: {name}")
        template_path.unlink()
        logger.info("删除工作流模板: %s", template_path)
        return True

    # ================================================================
    # 模型管理
    # ================================================================

    async def list_models(self) -> dict[str, list[str]]:
        """列出 ComfyUI 可用模型。

        Returns:
            按分类的模型列表
        """
        connector = self._require_connector()
        from connectors.types import ConnectorAction  # noqa: PLC0415

        action = ConnectorAction(action_type="list_models")
        result = await connector.execute_action(action)
        if not result.success:
            raise RuntimeError(f"获取模型列表失败: {result.error}")
        return result.output.get("models", {})

    # ================================================================
    # 生成任务
    # ================================================================

    async def generate(
        self,
        prompt: str,
        negative_prompt: str = "",
        template: str = "default_txt2img",
        **kwargs: Any,
    ) -> GenerationRecord:
        """提交图像生成任务。

        Args:
            prompt: 正向提示词
            negative_prompt: 负向提示词
            template: 工作流模板名称
            **kwargs: 额外参数

        Returns:
            生成记录
        """
        connector = self._require_connector()

        record = GenerationRecord(
            prompt=prompt,
            negative_prompt=negative_prompt,
            template_name=template,
            parameters=kwargs,
            status=GenerationStatus.PENDING,
        )
        self._history.add(record)

        workflow = self._build_workflow(template, prompt, negative_prompt, kwargs)

        from connectors.types import ConnectorAction  # noqa: PLC0415

        action = ConnectorAction(
            action_type="generate_image",
            parameters={"workflow": workflow},
        )
        result = await connector.execute_action(action)

        if not result.success:
            self._history.update(
                record.id,
                status=GenerationStatus.FAILED,
                error=result.error or "提交工作流失败",
            )
            record = self._history.get(record.id) or record
            return record

        prompt_id = result.output.get("prompt_id", "")
        self._history.update(
            record.id,
            status=GenerationStatus.RUNNING,
            parameters={**record.parameters, "prompt_id": prompt_id},
        )

        task = asyncio.create_task(
            self._monitor_generation(record.id, prompt_id),
            name=f"comfyui-gen-{record.id}",
        )
        self._running_tasks[record.id] = task

        record = self._history.get(record.id) or record
        return record

    async def _monitor_generation(self, record_id: str, prompt_id: str) -> None:
        """后台监控生成进度。"""
        connector = self._require_connector()
        try:
            while True:
                await asyncio.sleep(1.0)
                from connectors.types import ConnectorAction  # noqa: PLC0415

                action = ConnectorAction(
                    action_type="get_progress",
                    parameters={"prompt_id": prompt_id},
                )
                result = await connector.execute_action(action)
                if not result.success:
                    continue

                status = result.output.get("status", "unknown")
                completed = result.output.get("completed", False)

                if completed or status in ("success", "error"):
                    break

                self._task_progress[record_id] = result.output.get("progress", 0)

            from datetime import datetime, timezone  # noqa: PLC0415

            from connectors.types import ConnectorAction  # noqa: PLC0415

            action = ConnectorAction(
                action_type="get_result",
                parameters={"prompt_id": prompt_id},
            )
            result = await connector.execute_action(action)

            if result.success and result.output.get("images"):
                self._history.update(
                    record_id,
                    status=GenerationStatus.COMPLETED,
                    progress=100,
                    result_images=result.output["images"],
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                self._history.update(
                    record_id,
                    status=GenerationStatus.COMPLETED,
                    progress=100,
                    result_images=[],
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )

        except asyncio.CancelledError:
            self._history.update(record_id, status=GenerationStatus.FAILED, error="任务已取消")
        except Exception as e:
            logger.error("监控生成任务异常: record_id=%s, error=%s", record_id, e)
            self._history.update(record_id, status=GenerationStatus.FAILED, error=str(e))
        finally:
            self._running_tasks.pop(record_id, None)
            self._task_progress.pop(record_id, None)

    def _build_workflow(
        self,
        template_name: str,
        prompt: str,
        negative_prompt: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """构建工作流定义。"""
        template_path = self._workflow_dir / f"{template_name}.json"
        if not template_path.exists():
            raise FileNotFoundError(f"工作流模板不存在: {template_name}")

        template_str = template_path.read_text(encoding="utf-8")

        replace_params = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "width": params.get("width", 512),
            "height": params.get("height", 512),
            "steps": params.get("steps", 20),
            "cfg_scale": params.get("cfg_scale", 7.0),
            "seed": params.get("seed", -1),
            "checkpoint": params.get("checkpoint", "v1-5-pruned-emaonly.safetensors"),
        }

        result = template_str
        for key, value in replace_params.items():
            placeholder = "{{" + key + "}}"
            if isinstance(value, str):
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                result = result.replace(placeholder, escaped)
            else:
                result = result.replace(placeholder, str(value))

        return json.loads(result)

    # ================================================================
    # 任务进度与取消
    # ================================================================

    def get_task_progress(self, record_id: str) -> dict[str, Any] | None:
        """获取运行中任务的进度。"""
        record = self._history.get(record_id)
        if record is None:
            return None
        return {
            "id": record.id,
            "status": record.status,
            "progress": self._task_progress.get(record_id, record.progress),
            "prompt_id": record.parameters.get("prompt_id"),
        }

    async def cancel_task(self, record_id: str) -> bool:
        """取消运行中的任务。"""
        task = self._running_tasks.get(record_id)
        if task is None:
            return False

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        if self._connector and self._connector.is_connected:
            from connectors.types import ConnectorAction  # noqa: PLC0415

            action = ConnectorAction(action_type="interrupt_task")
            await self._connector.execute_action(action)

        return True

    # ================================================================
    # 进度回调注册
    # ================================================================

    def add_progress_callback(self, callback: ProgressCallback) -> None:
        """注册 WebSocket 进度回调。"""
        if self._connector is not None and hasattr(self._connector, "add_progress_callback"):
            self._connector.add_progress_callback(callback)

    def remove_progress_callback(self, callback: ProgressCallback) -> None:
        """移除 WebSocket 进度回调。"""
        if self._connector is not None and hasattr(self._connector, "remove_progress_callback"):
            self._connector.remove_progress_callback(callback)


# 全局单例
_global_service: ComfyUIService | None = None


def get_comfyui_service() -> ComfyUIService:
    """获取全局 ComfyUI 服务单例。"""
    global _global_service  # noqa: PLW0603
    if _global_service is None:
        _global_service = ComfyUIService()
    return _global_service
