"""
ComfyUI API 路由

提供 ComfyUI 相关的 RESTful API 和 WebSocket 端点，前缀 /api/v1/comfyui。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from channels.api.deps import APIError, require_auth
from services.comfyui_service import get_comfyui_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/comfyui",
    tags=["ComfyUI"],
    dependencies=[Depends(require_auth)],
)


class ConnectRequest(BaseModel):
    """连接 ComfyUI 请求。"""

    endpoint: str = Field(..., description="ComfyUI 服务地址")


class GenerateRequest(BaseModel):
    """图像生成请求。"""

    prompt: str = Field(..., description="正向提示词")
    negative_prompt: str = Field(default="", description="负面提示词")
    template: str = Field(default="default_txt2img", description="工作流模板名称")
    width: int = Field(default=512, ge=64, le=2048, description="图像宽度")
    height: int = Field(default=512, ge=64, le=2048, description="图像高度")
    steps: int = Field(default=20, ge=1, le=150, description="采样步数")
    cfg_scale: float = Field(default=7.0, ge=1.0, le=30.0, description="CFG 引导系数")
    seed: int = Field(default=-1, description="随机种子，-1 为随机")
    checkpoint: str | None = Field(default=None, description="模型检查点名称")


class SaveWorkflowRequest(BaseModel):
    """保存工作流模板请求。"""

    name: str = Field(..., min_length=1, max_length=100, description="模板名称")
    workflow: dict[str, Any] = Field(..., description="工作流定义")


@router.get("/status", summary="获取 ComfyUI 连接状态")
async def get_status() -> dict[str, Any]:
    """获取当前 ComfyUI 连接状态。"""
    service = get_comfyui_service()
    return service.get_status()


@router.post("/connect", summary="连接 ComfyUI 服务")
async def connect_comfyui(body: ConnectRequest) -> dict[str, Any]:
    """连接到指定的 ComfyUI 服务。"""
    service = get_comfyui_service()
    result = await service.connect(body.endpoint)
    if not result.get("connected", False):
        raise APIError(status_code=503, error_code="COMFYUI_001", message=result.get("error", "连接失败"))
    return result


@router.post("/disconnect", summary="断开 ComfyUI 连接")
async def disconnect_comfyui() -> dict[str, Any]:
    """断开与 ComfyUI 的连接。"""
    service = get_comfyui_service()
    return await service.disconnect()


@router.get("/models", summary="列出可用模型")
async def list_models() -> dict[str, Any]:
    """列出 ComfyUI 中可用的模型。"""
    service = get_comfyui_service()
    try:
        models = await service.list_models()
        return {"models": models}
    except RuntimeError as e:
        raise APIError(status_code=503, error_code="COMFYUI_002", message=str(e))  # noqa: B904
    except Exception as e:
        raise APIError(status_code=500, error_code="COMFYUI_099", message=f"获取模型列表失败: {e}")  # noqa: B904


@router.get("/workflows", summary="列出工作流模板")
async def list_workflows() -> dict[str, Any]:
    """列出所有可用的工作流模板。"""
    service = get_comfyui_service()
    templates = service.list_workflows()
    return {"templates": templates, "total": len(templates)}


@router.get("/workflows/{name}", summary="获取工作流模板详情")
async def get_workflow(name: str) -> dict[str, Any]:
    """获取指定工作流模板的完整定义。"""
    service = get_comfyui_service()
    try:
        workflow = service.get_workflow(name)
        return {"name": name, "workflow": workflow}
    except FileNotFoundError:
        raise APIError(status_code=404, error_code="COMFYUI_003", message=f"工作流模板不存在: {name}")  # noqa: B904


@router.post("/workflows", summary="保存自定义工作流模板")
async def save_workflow(body: SaveWorkflowRequest) -> dict[str, Any]:
    """保存自定义工作流模板到模板目录。"""
    service = get_comfyui_service()
    result = service.save_workflow(body.name, body.workflow)
    return {"success": True, **result}


@router.delete("/workflows/{name}", summary="删除自定义工作流模板")
async def delete_workflow(name: str) -> dict[str, Any]:
    """删除指定的工作流模板。"""
    service = get_comfyui_service()
    try:
        service.delete_workflow(name)
        return {"success": True, "message": f"模板 {name} 已删除"}
    except FileNotFoundError:
        raise APIError(status_code=404, error_code="COMFYUI_003", message=f"工作流模板不存在: {name}")  # noqa: B904


@router.post("/generate", summary="提交图像生成任务")
async def generate_image(body: GenerateRequest) -> dict[str, Any]:
    """提交图像生成任务。"""
    service = get_comfyui_service()
    try:
        kwargs: dict[str, Any] = {
            "width": body.width,
            "height": body.height,
            "steps": body.steps,
            "cfg_scale": body.cfg_scale,
            "seed": body.seed,
        }
        if body.checkpoint:
            kwargs["checkpoint"] = body.checkpoint

        record = await service.generate(
            prompt=body.prompt,
            negative_prompt=body.negative_prompt,
            template=body.template,
            **kwargs,
        )
        return {"success": True, "record": record.to_dict()}
    except RuntimeError as e:
        raise APIError(status_code=503, error_code="COMFYUI_002", message=str(e))  # noqa: B904
    except FileNotFoundError as e:
        raise APIError(status_code=404, error_code="COMFYUI_003", message=str(e))  # noqa: B904
    except Exception as e:
        raise APIError(status_code=500, error_code="COMFYUI_099", message=f"生成任务提交失败: {e}")  # noqa: B904


@router.get("/history", summary="获取生成历史")
async def get_history(
    limit: int = Query(default=20, ge=1, le=100, description="每页数量"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    status: str | None = Query(default=None, description="按状态过滤"),
) -> dict[str, Any]:
    """获取生成历史记录（分页）。"""
    service = get_comfyui_service()
    records, total = service.history.list_records(limit=limit, offset=offset, status=status)
    return {
        "records": [r.to_dict() for r in records],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/history/{record_id}", summary="获取单条生成记录")
async def get_history_record(record_id: str) -> dict[str, Any]:
    """获取指定 ID 的生成记录。"""
    service = get_comfyui_service()
    record = service.history.get(record_id)
    if record is None:
        raise APIError(status_code=404, error_code="COMFYUI_004", message=f"生成记录不存在: {record_id}")
    return {"record": record.to_dict()}


@router.delete("/history/{record_id}", summary="删除生成记录")
async def delete_history_record(record_id: str) -> dict[str, Any]:
    """删除指定的生成记录。"""
    service = get_comfyui_service()
    deleted = service.history.delete(record_id)
    if not deleted:
        raise APIError(status_code=404, error_code="COMFYUI_004", message=f"生成记录不存在: {record_id}")
    return {"success": True, "message": "记录已删除"}


@router.get("/tasks/{record_id}/progress", summary="获取运行中任务的进度")
async def get_task_progress(record_id: str) -> dict[str, Any]:
    """获取运行中生成任务的进度。"""
    service = get_comfyui_service()
    progress = service.get_task_progress(record_id)
    if progress is None:
        raise APIError(status_code=404, error_code="COMFYUI_004", message=f"生成记录不存在: {record_id}")
    return progress


@router.post("/tasks/{record_id}/cancel", summary="取消运行中的任务")
async def cancel_task(record_id: str) -> dict[str, Any]:
    """取消运行中的生成任务。"""
    service = get_comfyui_service()
    cancelled = await service.cancel_task(record_id)
    if not cancelled:
        raise APIError(status_code=400, error_code="COMFYUI_005", message=f"任务不存在或不在运行中: {record_id}")
    return {"success": True, "message": "任务已取消"}


@router.websocket("/ws")
async def comfyui_ws(websocket: WebSocket) -> None:
    """WebSocket 端点，向客户端实时推送 ComfyUI 生成进度。"""
    await websocket.accept()
    service = get_comfyui_service()

    def on_progress(msg_type: str, data: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(websocket.send_text(json.dumps({"type": msg_type, "data": data}, ensure_ascii=False)))
        except Exception:
            pass

    service.add_progress_callback(on_progress)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        logger.debug("ComfyUI WebSocket 客户端断开")
    except Exception as e:
        logger.error("ComfyUI WebSocket 异常: %s", e)
    finally:
        service.remove_progress_callback(on_progress)
