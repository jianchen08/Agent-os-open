"""维护管理 API 路由。

提供手动触发复盘的接口。复盘由 trigger_llm_review 编排：
启动 review_agent 管道做 LLM 深度分析 → 产出报告 → 持久化。

该模块为独立模块，触发链路：
  POST /api/v1/maintenance/review
    -> MemoryMaintenanceService.trigger_llm_review(parent_pipeline_id="")
    -> review_agent 管道（LLM 深度复盘）
    -> 报告写入 KnowledgeService + docs/working/review_report_*.md
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from channels.api.deps import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/maintenance",
    tags=["维护管理"],
    dependencies=[Depends(require_auth)],
)


def _get_maintenance_service() -> Any:
    """从 ServiceProvider 获取全局 MaintenanceService 实例。

    Returns:
        MemoryMaintenanceService 实例，服务不可用返回 None
    """
    try:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

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
    """手动触发复盘（LLM 深度复盘）。

    启动 review_agent 管道，对 status=已结束 且 review_status=pending 的管道
    做 LLM 深度复盘，产出结构化报告并写入 KnowledgeService + docs/working/。

    API 触发场景没有父管道，parent_pipeline_id 传空串
    （复盘完成后不回写通知，仅落盘报告）。

    Returns:
        复盘提交结果
    """
    maintenance_service = _get_maintenance_service()
    if maintenance_service is None:
        return {"status": "error", "message": "MaintenanceService 不可用"}

    # trigger_llm_review 内部已做 _review_running 互斥与防自循环检查，直接调用即可
    # 单批复盘多少个管道由 service 内部按 agent/status 分组 + 模型窗口预算反推决定
    result = await maintenance_service.trigger_llm_review(
        parent_pipeline_id="",
    )

    return {
        "status": result.get("status", "submitted"),
        "message": result.get("message", "复盘任务已提交，完成后会通知您结果。"),
    }
