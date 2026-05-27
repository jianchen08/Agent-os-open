"""维护管理 API 路由。

提供手动触发复盘的接口。
通过管道消息注入机制触发复盘，与定时触发使用相同的管道执行路径。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/maintenance", tags=["维护管理"])


class ReviewTriggerRequest(BaseModel):
    """手动触发复盘请求。"""

    force: bool = Field(default=False, description="是否强制触发")
    pipeline_id: str = Field(default="", description="目标管道 ID，为空则自动查找")
    model_config = {"extra": "ignore"}


def _find_active_pipeline() -> str:
    """从引擎注册表中查找一个活跃的管道 ID。

    Returns:
        管道 ID，未找到返回空字符串
    """
    try:
        from pipeline.registry import get_engine_registry
        registry = get_engine_registry()
        entries = registry.list_all() if hasattr(registry, "list_all") else []
        if entries:
            return entries[0] if isinstance(entries[0], str) else entries[0].get("id", "")
    except Exception:
        pass
    return ""


def _get_maintenance_service() -> Any:
    """从 ServiceProvider 获取全局 MaintenanceService 实例。

    Returns:
        MemoryMaintenanceService 实例，服务不可用返回 None
    """
    try:
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
        return provider.get("maintenance_service")
    except Exception as exc:
        logger.warning(
            "_get_maintenance_service: MaintenanceService 获取失败 | error=%s",
            exc,
        )
        return None


@router.post("/review", summary="手动触发复盘")
async def trigger_review(
    body: ReviewTriggerRequest | None = None,
) -> dict[str, Any]:
    """手动触发复盘。

    通过 send_pipeline_message 向管道注入复盘指令，
    由管道中的 Agent 执行复盘任务。完成后自动通知结果。

    Args:
        body: 复盘触发请求

    Returns:
        复盘任务提交结果
    """
    force = body.force if body else False
    pipeline_id = body.pipeline_id if body else ""

    maintenance_service = _get_maintenance_service()
    if maintenance_service is None:
        return {
            "status": "error",
            "message": "MaintenanceService 不可用",
        }

    if getattr(maintenance_service, "_review_running", False):
        return {
            "status": "already_running",
            "message": "复盘正在执行中",
        }

    if not force and not maintenance_service.should_trigger_review():
        pending = maintenance_service._get_review_engine()._count_pending_records()
        return {
            "status": "skipped",
            "message": f"未满足复盘触发条件，当前待复盘记录 {pending} 条",
        }

    if not pipeline_id:
        pipeline_id = _find_active_pipeline()

    if pipeline_id:
        try:
            from pipeline.message_bus import send_pipeline_message
            result = await send_pipeline_message(
                pipeline_id,
                "[手动触发] 请执行复盘任务，分析最近的管道执行记录，产出经验和改进建议。",
                metadata={"source": "manual_review", "force": force},
            )
            if result.success:
                return {
                    "status": "submitted",
                    "message": "复盘任务已通过管道提交，完成后将通过消息通知",
                    "pipeline_id": pipeline_id,
                    "method": result.method,
                }
            logger.warning("管道消息注入失败: %s", result.error)
        except Exception as exc:
            logger.warning("管道消息注入异常: %s", exc)

    async def _run_review_direct() -> None:
        """管道不可用时降级直接执行复盘。"""
        try:
            await maintenance_service.trigger_review(force=force)
            logger.info("手动触发的复盘任务已完成（直接执行）")
        except Exception as exc:
            logger.error("手动触发的复盘任务执行失败: %s", exc)

    asyncio.create_task(_run_review_direct())

    return {
        "status": "submitted",
        "message": "复盘任务已提交（直接执行），完成后将通过消息通知",
    }
