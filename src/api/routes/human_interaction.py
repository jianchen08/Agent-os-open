"""
人类交互 REST API 路由

提供交互请求查询和响应提交的 REST 接口
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from src.auth.dependencies import get_current_user
from src.core.human_interaction.models import (
    InteractionResponse,
    InteractionType,
    ResponseType,
)
from src.core.human_interaction.service import get_human_interaction_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/interaction", tags=["Human Interaction"])


class InteractionResponseRequest(BaseModel):
    """交互响应请求"""

    request_id: str = Field(..., description="请求 ID")
    response_type: str = Field(..., description="响应类型")
    selected_option_id: str | None = Field(None, description="选择的选项 ID")
    modified_data: dict[str, Any] | None = Field(None, description="修改后的数据")
    reason: str | None = Field(None, description="原因/备注")
    conversation_result: str | None = Field(None, description="对话结论")
    conversation_messages: list[dict[str, Any]] | None = Field(
        None, description="对话历史"
    )


class ConversationJumpRequest(BaseModel):
    """对话跳转请求"""

    request_id: str = Field(..., description="请求 ID")
    agent_id: str | None = Field(None, description="目标 Agent ID")
    thread_id: str = Field(..., description="线程 ID")


@router.get("/pending", summary="获取待处理交互请求")
async def get_pending_interactions(
    thread_id: str | None = None,
    agent_id: str | None = None,
    interaction_type: str | None = None,
    limit: int = 50,
    current_user=Depends(get_current_user),
) -> list[dict[str, Any]]:
    """
    获取待处理的交互请求列表

    Args:
        thread_id: 线程 ID 过滤
        agent_id: Agent ID 过滤
        interaction_type: 交互类型过滤
        limit: 返回数量限制
        current_user: 当前用户

    Returns:
        待处理请求列表
    """
    service = get_human_interaction_service()

    int_type = None
    if interaction_type:
        try:
            int_type = InteractionType(interaction_type)
        except ValueError:
            pass

    requests = await service.get_pending_requests(
        thread_id=thread_id,
        agent_id=agent_id,
        interaction_type=int_type,
        limit=limit,
    )

    return [req.to_dict() for req in requests]


@router.get("/{request_id}", summary="获取交互请求详情")
async def get_interaction_request(
    request_id: str,
    current_user=Depends(get_current_user),
) -> dict[str, Any]:
    """
    获取交互请求详情

    Args:
        request_id: 请求 ID
        current_user: 当前用户

    Returns:
        请求详情
    """
    service = get_human_interaction_service()

    request = await service.get_request(request_id)
    if not request:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"请求不存在: {request_id}",
        )

    return request.to_dict()


@router.post("/response", summary="提交交互响应")
async def submit_interaction_response(
    request: InteractionResponseRequest,
    current_user=Depends(get_current_user),
) -> dict[str, Any]:
    """
    提交交互响应

    Args:
        request: 响应请求
        current_user: 当前用户

    Returns:
        提交结果
    """
    service = get_human_interaction_service()

    try:
        response_type = ResponseType(request.response_type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"无效的响应类型: {request.response_type}",
        )

    existing_request = await service.get_request(request.request_id)
    if not existing_request:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"请求不存在: {request.request_id}",
        )

    response = InteractionResponse(
        request_id=request.request_id,
        response_type=response_type,
        selected_option_id=request.selected_option_id,
        modified_data=request.modified_data,
        reason=request.reason,
        conversation_result=request.conversation_result,
        conversation_messages=request.conversation_messages or [],
        user_id=str(current_user.id),
    )

    success = await service.submit_response(request.request_id, response)

    return {
        "request_id": request.request_id,
        "success": success,
        "response_type": response_type.value,
    }


@router.post("/{request_id}/approve", summary="批准请求")
async def approve_interaction(
    request_id: str,
    option_id: str | None = None,
    reason: str | None = None,
    current_user=Depends(get_current_user),
) -> dict[str, Any]:
    """
    批准交互请求

    Args:
        request_id: 请求 ID
        option_id: 选择的选项 ID
        reason: 批准原因
        current_user: 当前用户

    Returns:
        响应结果
    """
    service = get_human_interaction_service()

    response = await service.approve(
        request_id=request_id,
        option_id=option_id,
        reason=reason,
        user_id=str(current_user.id),
    )

    return response.to_dict()


@router.post("/{request_id}/deny", summary="拒绝请求")
async def deny_interaction(
    request_id: str,
    reason: str | None = None,
    current_user=Depends(get_current_user),
) -> dict[str, Any]:
    """
    拒绝交互请求

    Args:
        request_id: 请求 ID
        reason: 拒绝原因
        current_user: 当前用户

    Returns:
        响应结果
    """
    service = get_human_interaction_service()

    response = await service.deny(
        request_id=request_id,
        reason=reason,
        user_id=str(current_user.id),
    )

    return response.to_dict()


@router.post("/{request_id}/cancel", summary="取消请求")
async def cancel_interaction(
    request_id: str,
    reason: str | None = None,
    current_user=Depends(get_current_user),
) -> dict[str, Any]:
    """
    取消交互请求

    Args:
        request_id: 请求 ID
        reason: 取消原因
        current_user: 当前用户

    Returns:
        取消结果
    """
    service = get_human_interaction_service()

    success = await service.cancel_request(request_id, reason)

    return {
        "request_id": request_id,
        "success": success,
        "reason": reason,
    }


@router.post("/conversation/jump", summary="跳转到对话窗口")
async def jump_to_conversation(
    request: ConversationJumpRequest,
    current_user=Depends(get_current_user),
) -> dict[str, Any]:
    """
    跳转到对话窗口

    前端收到交互请求后，调用此接口获取跳转信息，
    然后跳转到对应的 Agent 子标签页。

    Args:
        request: 跳转请求
        current_user: 当前用户

    Returns:
        跳转信息
    """
    service = get_human_interaction_service()

    interaction_request = await service.get_request(request.request_id)
    if not interaction_request:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"请求不存在: {request.request_id}",
        )

    agent_id = request.agent_id or interaction_request.agent_id

    return {
        "request_id": request.request_id,
        "thread_id": request.thread_id,
        "agent_id": agent_id,
        "interaction_type": interaction_request.interaction_type.value,
        "mode": interaction_request.mode.value,
        "title": interaction_request.title,
        "description": interaction_request.description,
        "jump_url": f"/chat/{request.thread_id}?agent={agent_id}&interaction={request.request_id}",
        "conversation_context": (
            interaction_request.conversation_context.to_dict()
            if interaction_request.conversation_context
            else None
        ),
    }


@router.post("/{request_id}/conversation/end", summary="结束对话")
async def end_conversation(
    request_id: str,
    result: str,
    current_user=Depends(get_current_user),
) -> dict[str, Any]:
    """
    结束对话

    Args:
        request_id: 请求 ID
        result: 对话结论
        current_user: 当前用户

    Returns:
        响应结果
    """
    service = get_human_interaction_service()

    response = await service.end_conversation(
        request_id=request_id,
        result=result,
        user_id=str(current_user.id),
    )

    return response.to_dict()
