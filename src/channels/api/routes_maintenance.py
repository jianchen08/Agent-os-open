"""维护管理 API 路由。

提供手动触发复盘的接口。
直接调用 ReviewEngine 处理 ExecutionRecordStorage 中所有 pending 的管道运行，
产出经验/改进建议并落到 KnowledgeService（source_type=review_experience）。

该模块为独立模块，触发链路：
  POST /api/v1/maintenance/review
    -> MaintenanceService.trigger_review_now()
    -> ReviewEngine.get_pending_pipelines() / run_review(run_id)
    -> ExecutionRecordStorage + KnowledgeService 真实落盘
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/maintenance", tags=["维护管理"])


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
async def trigger_review() -> dict[str, Any]:
    """手动触发复盘。

    直接调用 ReviewEngine 处理所有 status=completed && review_status=pending
    的管道运行，产出经验并写入 KnowledgeService。

    Returns:
        复盘执行结果
    """
    maintenance_service = _get_maintenance_service()
    if maintenance_service is None:
        return {"status": "error", "message": "MaintenanceService 不可用"}

    if getattr(maintenance_service, "_review_running", False):
        return {"status": "already_running", "message": "复盘正在执行中"}

    # 直接走同步触发，不依赖 LLM agent / pipeline_engine
    maintenance_service._review_running = True

    async def _run() -> None:
        """异步执行复盘。"""
        try:
            await maintenance_service.trigger_review_now()
        except Exception as exc:
            logger.error("[手动复盘] 复盘执行失败: %s", exc, exc_info=True)
        finally:
            maintenance_service._review_running = False

    asyncio.create_task(_run())

    return {"status": "submitted", "message": "复盘任务已提交（直接走 ReviewEngine）"}
