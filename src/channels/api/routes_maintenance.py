"""维护管理 API 路由。

提供手动触发复盘的接口。
通过管道引擎注册复盘管道，再用消息注入触发执行。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/maintenance", tags=["维护管理"])

REVIEW_AGENT_ID = "review_agent"


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


async def _start_review_pipeline(force: bool) -> dict[str, Any]:
    """注册复盘管道并通过消息注入触发执行。

    流程与 TaskExecutor 一致：
    1. 从 AgentRegistry 获取 review_agent 配置
    2. 通过 EngineRegistry 注册管道
    3. 用 send_pipeline_message 注入复盘上下文

    Args:
        force: 是否强制触发

    Returns:
        执行结果
    """
    from pipeline.message_bus import send_pipeline_message
    from pipeline.registry import get_engine_registry
    from agents.agent_registry import get_agent_registry
    from config.agent_loader import load_agent_config

    agent_config = load_agent_config(REVIEW_AGENT_ID)
    if agent_config is None:
        agent_config = get_agent_registry().get(REVIEW_AGENT_ID)
    if agent_config is None:
        return {"status": "error", "message": f"Agent '{REVIEW_AGENT_ID}' 配置不存在"}

    registry = get_engine_registry()
    entry = registry.register_pipeline(
        tags={"source": "manual_review", "force": str(force)},
    )
    pipeline_id = entry.engine.pipeline_id

    result = await send_pipeline_message(
        pipeline_id,
        "[手动触发复盘] 请分析最近的管道执行记录，产出经验和改进建议。",
        agent_config=agent_config,
        metadata={"source": "manual_review", "force": force},
    )

    if result.success:
        return {
            "status": "submitted",
            "message": "复盘任务已通过管道提交",
            "pipeline_id": pipeline_id,
            "method": result.method,
        }
    return {"status": "error", "message": f"管道注入失败: {result.error}"}


@router.post("/review", summary="手动触发复盘")
async def trigger_review(
    body: ReviewTriggerRequest | None = None,
) -> dict[str, Any]:
    """手动触发复盘。

    注册复盘 Agent 管道并通过消息注入触发执行。
    完成后自动通过管道输出通知用户。

    Args:
        body: 复盘触发请求

    Returns:
        复盘任务提交结果
    """
    force = body.force if body else False

    maintenance_service = _get_maintenance_service()
    if maintenance_service is None:
        return {"status": "error", "message": "MaintenanceService 不可用"}

    if getattr(maintenance_service, "_review_running", False):
        return {"status": "already_running", "message": "复盘正在执行中"}

    if not force and not maintenance_service.should_trigger_review():
        pending = maintenance_service._get_review_engine()._count_pending_records()
        return {
            "status": "skipped",
            "message": f"未满足复盘触发条件，当前待复盘记录 {pending} 条",
        }

    maintenance_service._review_running = True

    async def _run() -> None:
        """异步执行复盘管道。"""
        try:
            await _start_review_pipeline(force)
        except Exception as exc:
            logger.error("[手动复盘] 复盘管道执行失败: %s", exc)
        finally:
            maintenance_service._review_running = False

    asyncio.create_task(_run())

    return {"status": "submitted", "message": "复盘任务已通过管道提交"}
