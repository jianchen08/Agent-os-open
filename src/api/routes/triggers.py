"""
触发器 API 路由

提供触发器的管理接口：创建、查询、更新、删除、手动触发等。
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.triggers.models import TriggerType
from src.triggers.registry import TriggerRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/triggers", tags=["triggers"])


# ============================================
# Pydantic 模型
# ============================================


class TriggerResponse(BaseModel):
    """触发器响应"""

    id: str
    name: str
    trigger_type: str
    enabled: bool
    execution_count: int = 0
    last_execution: str | None = None
    last_result: dict[str, Any] | None = None
    config: dict[str, Any]


class TriggerListResponse(BaseModel):
    """触发器列表响应"""

    total: int
    triggers: list[TriggerResponse]


class TriggerStatsResponse(BaseModel):
    """触发器统计响应"""

    total_triggers: int
    enabled_triggers: int
    disabled_triggers: int
    type_counts: dict[str, int]
    trigger_ids: list[str]


class TriggerCreateRequest(BaseModel):
    """创建触发器请求"""

    id: str = Field(..., description="触发器 ID")
    name: str = Field(..., description="触发器名称")
    trigger_type: str = Field(..., description="触发器类型: time/event/condition")
    enabled: bool = Field(True, description="是否启用")
    description: str | None = Field(None, description="触发器描述")
    actions: list[dict[str, Any]] = Field(default_factory=list, description="动作列表")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")
    schedule: dict[str, Any] | None = Field(None, description="时间调度配置")
    event: dict[str, Any] | None = Field(None, description="事件配置")
    condition: dict[str, Any] | None = Field(None, description="条件配置")


class TriggerUpdateRequest(BaseModel):
    """更新触发器请求"""

    name: str | None = None
    enabled: bool | None = None
    description: str | None = None
    actions: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    event: dict[str, Any] | None = None
    condition: dict[str, Any] | None = None


class ManualTriggerRequest(BaseModel):
    """手动触发请求"""

    context: dict[str, Any] = Field(default_factory=dict, description="触发上下文")


# ============================================
# 依赖注入
# ============================================


async def get_trigger_registry() -> TriggerRegistry:
    """获取触发器注册表（从应用状态）"""
    from src.api.main import app

    if not hasattr(app.state, "trigger_registry"):
        raise HTTPException(status_code=503, detail="触发器系统未初始化")
    return app.state.trigger_registry


# ============================================
# API 路由
# ============================================


@router.get("", response_model=TriggerListResponse)
async def list_triggers(
    enabled_only: bool = Query(False, description="只返回已启用的触发器"),
    trigger_type: str | None = Query(None, description="过滤触发器类型"),
    registry: TriggerRegistry = Depends(get_trigger_registry),
):
    """
    列出所有触发器

    Args:
        enabled_only: 是否只返回已启用的触发器
        trigger_type: 过滤触发器类型
        registry: 触发器注册表

    Returns:
        TriggerListResponse: 触发器列表
    """
    # 解析触发器类型
    trigger_type_enum = None
    if trigger_type:
        try:
            trigger_type_enum = TriggerType(trigger_type)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"无效的触发器类型: {trigger_type}"
            )

    triggers = await registry.list_triggers(
        enabled_only=enabled_only, trigger_type=trigger_type_enum
    )

    trigger_responses = [TriggerResponse(**trigger.to_dict()) for trigger in triggers]

    return TriggerListResponse(total=len(trigger_responses), triggers=trigger_responses)


@router.get("/stats", response_model=TriggerStatsResponse)
async def get_trigger_stats(registry: TriggerRegistry = Depends(get_trigger_registry)):
    """
    获取触发器统计信息

    Args:
        registry: 触发器注册表

    Returns:
        TriggerStatsResponse: 统计信息
    """
    stats = registry.get_stats()
    return TriggerStatsResponse(**stats)


@router.get("/{trigger_id}", response_model=TriggerResponse)
async def get_trigger(
    trigger_id: str, registry: TriggerRegistry = Depends(get_trigger_registry)
):
    """
    获取单个触发器详情

    Args:
        trigger_id: 触发器 ID
        registry: 触发器注册表

    Returns:
        TriggerResponse: 触发器详情
    """
    trigger = await registry.get_trigger(trigger_id)

    if not trigger:
        raise HTTPException(status_code=404, detail=f"触发器不存在: {trigger_id}")

    return TriggerResponse(**trigger.to_dict())


@router.post("", response_model=dict[str, str])
async def create_trigger(
    request: TriggerCreateRequest,
    registry: TriggerRegistry = Depends(get_trigger_registry),
):
    """
    创建触发器

    Args:
        request: 创建请求
        registry: 触发器注册表

    Returns:
        Dict[str, str]: 创建结果
    """
    # 构建触发器配置
    trigger_dict = request.dict()
    trigger_dict["metadata"] = {
        **trigger_dict.get("metadata", {}),
        "created_via": "api",
        "created_at": datetime.utcnow().isoformat(),
    }

    try:
        await registry.register_trigger(trigger_dict)
        logger.info(f"创建触发器成功: {request.id}")
        return {"status": "success", "id": request.id}
    except Exception as e:
        logger.error(f"创建触发器失败: {e}")
        raise HTTPException(status_code=400, detail=f"创建触发器失败: {str(e)}")


@router.put("/{trigger_id}", response_model=dict[str, str])
async def update_trigger(
    trigger_id: str,
    request: TriggerUpdateRequest,
    registry: TriggerRegistry = Depends(get_trigger_registry),
):
    """
    更新触发器

    Args:
        trigger_id: 触发器 ID
        request: 更新请求
        registry: 触发器注册表

    Returns:
        Dict[str, str]: 更新结果
    """
    # 获取现有触发器
    existing_trigger = await registry.get_trigger(trigger_id)
    if not existing_trigger:
        raise HTTPException(status_code=404, detail=f"触发器不存在: {trigger_id}")

    # 构建更新配置
    update_dict = {"id": trigger_id}
    request_dict = request.dict(exclude_unset=True)

    # 合并现有配置
    existing_trigger.config.to_dict()
    for key, value in request_dict.items():
        if value is not None:
            update_dict[key] = value

    # 必需字段
    if "trigger_type" not in update_dict:
        update_dict["trigger_type"] = existing_trigger.trigger_type.value
    if "actions" not in update_dict:
        update_dict["actions"] = [
            action.to_dict() for action in existing_trigger.config.actions
        ]

    try:
        await registry.update_trigger(trigger_id, update_dict)
        logger.info(f"更新触发器成功: {trigger_id}")
        return {"status": "success", "id": trigger_id}
    except Exception as e:
        logger.error(f"更新触发器失败: {e}")
        raise HTTPException(status_code=400, detail=f"更新触发器失败: {str(e)}")


@router.delete("/{trigger_id}", response_model=dict[str, str])
async def delete_trigger(
    trigger_id: str, registry: TriggerRegistry = Depends(get_trigger_registry)
):
    """
    删除触发器

    Args:
        trigger_id: 触发器 ID
        registry: 触发器注册表

    Returns:
        Dict[str, str]: 删除结果
    """
    trigger = await registry.get_trigger(trigger_id)
    if not trigger:
        raise HTTPException(status_code=404, detail=f"触发器不存在: {trigger_id}")

    await registry.unregister_trigger(trigger_id)
    logger.info(f"删除触发器成功: {trigger_id}")
    return {"status": "success", "id": trigger_id}


@router.post("/{trigger_id}/enable", response_model=dict[str, str])
async def enable_trigger(
    trigger_id: str, registry: TriggerRegistry = Depends(get_trigger_registry)
):
    """
    启用触发器

    Args:
        trigger_id: 触发器 ID
        registry: 触发器注册表

    Returns:
        Dict[str, str]: 启用结果
    """
    success = await registry.enable_trigger(trigger_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"触发器不存在: {trigger_id}")

    logger.info(f"启用触发器成功: {trigger_id}")
    return {"status": "success", "id": trigger_id}


@router.post("/{trigger_id}/disable", response_model=dict[str, str])
async def disable_trigger(
    trigger_id: str, registry: TriggerRegistry = Depends(get_trigger_registry)
):
    """
    禁用触发器

    Args:
        trigger_id: 触发器 ID
        registry: 触发器注册表

    Returns:
        Dict[str, str]: 禁用结果
    """
    success = await registry.disable_trigger(trigger_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"触发器不存在: {trigger_id}")

    logger.info(f"禁用触发器成功: {trigger_id}")
    return {"status": "success", "id": trigger_id}


@router.post("/{trigger_id}/trigger", response_model=dict[str, Any])
async def manual_trigger(
    trigger_id: str,
    request: ManualTriggerRequest = None,
    registry: TriggerRegistry = Depends(get_trigger_registry),
):
    """
    手动触发触发器

    Args:
        trigger_id: 触发器 ID
        request: 手动触发请求
        registry: 触发器注册表

    Returns:
        Dict[str, Any]: 触发结果
    """
    trigger = await registry.get_trigger(trigger_id)
    if not trigger:
        raise HTTPException(status_code=404, detail=f"触发器不存在: {trigger_id}")

    if not trigger.enabled:
        raise HTTPException(status_code=400, detail=f"触发器已禁用: {trigger_id}")

    context = request.context if request else {}

    try:
        result = await trigger.execute_actions(context=context)
        logger.info(f"手动触发触发器成功: {trigger_id}")
        return {
            "status": "success",
            "trigger_id": trigger_id,
            "result": result.to_dict(),
        }
    except Exception as e:
        logger.error(f"手动触发触发器失败: {e}")
        raise HTTPException(status_code=500, detail=f"触发执行失败: {str(e)}")
