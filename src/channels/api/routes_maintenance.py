"""维护管理 API 路由。

提供手动触发复盘的接口。
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
    model_config = {"extra": "ignore"}


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

    提交复盘任务，后台执行，完成后通过消息注入通知用户。
    如果复盘已在运行中，返回 already_running 状态。

    Args:
        body: 复盘触发请求，包含 force 字段

    Returns:
        复盘任务提交结果
    """
    force = body.force if body else False

    maintenance_service = _get_maintenance_service()
    if maintenance_service is None:
        return {
            "status": "error",
            "message": "MaintenanceService 不可用",
        }

    # 检查是否已在运行
    if not force and getattr(maintenance_service, "_review_running", False):
        return {
            "status": "already_running",
            "message": "复盘正在执行中",
        }

    # 启动后台任务执行复盘
    async def _run_review() -> None:
        """后台执行复盘任务。"""
        try:
            await maintenance_service.trigger_review(force=force)
            logger.info("手动触发的复盘任务已完成")
        except Exception as exc:
            logger.error("手动触发的复盘任务执行失败: %s", exc)

    asyncio.create_task(_run_review())

    return {
        "status": "submitted",
        "message": "复盘任务已提交，完成后将通过消息通知",
    }
