"""线程与消息相关 API 路由。

提供线程的 CRUD 操作和消息查询接口，所有接口需要 Bearer token 认证。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, status

from channels.api.auth import get_current_user
from channels.api.models import (
    MessageResponse,
    ThreadCreate,
    ThreadResponse,
    store,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/threads", tags=["线程"])


def _authenticate(token: str) -> dict:
    """验证 Bearer token 并返回用户信息。

    Args:
        token: Bearer token 字符串

    Returns:
        用户信息字典

    Raises:
        HTTPException: token 无效
    """
    user_info = get_current_user(token)
    if user_info is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或过期的认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_info


@router.get("", response_model=list[ThreadResponse], summary="获取线程列表")
def list_threads(token: str = Query(..., description="Bearer token")) -> list[ThreadResponse]:
    """获取当前用户的所有线程列表。

    Args:
        token: Bearer token

    Returns:
        ThreadResponse 列表
    """
    user_info = _authenticate(token)
    threads = store.get_user_threads(user_info["sub"])

    return [
        ThreadResponse(
            id=t["id"],
            title=t["title"],
            created_at=t["created_at"],
            updated_at=t["updated_at"],
            message_count=t["message_count"],
        )
        for t in threads
    ]


@router.post("", response_model=ThreadResponse, status_code=status.HTTP_201_CREATED, summary="创建线程")
def create_thread(
    body: ThreadCreate,
    token: str = Query(..., description="Bearer token"),
) -> ThreadResponse:
    """创建新线程。

    Args:
        body: 线程创建请求，包含可选标题
        token: Bearer token

    Returns:
        ThreadResponse 新创建的线程
    """
    user_info = _authenticate(token)
    thread = store.create_thread(
        user_id=user_info["sub"],
        title=body.title,
    )

    return ThreadResponse(
        id=thread["id"],
        title=thread["title"],
        created_at=thread["created_at"],
        updated_at=thread["updated_at"],
        message_count=0,
    )


@router.get("/{thread_id}", response_model=ThreadResponse, summary="获取线程详情")
def get_thread(
    thread_id: str,
    token: str = Query(..., description="Bearer token"),
) -> ThreadResponse:
    """获取指定线程的详情。

    Args:
        thread_id: 线程 ID
        token: Bearer token

    Returns:
        ThreadResponse 线程详情

    Raises:
        HTTPException: 线程不存在或无权限
    """
    _authenticate(token)

    thread = store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="线程不存在",
        )

    return ThreadResponse(
        id=thread["id"],
        title=thread["title"],
        created_at=thread["created_at"],
        updated_at=thread["updated_at"],
        message_count=thread["message_count"],
    )


@router.delete("/{thread_id}", summary="删除线程")
def delete_thread(
    thread_id: str,
    token: str = Query(..., description="Bearer token"),
) -> dict[str, str]:
    """删除指定线程及其所有消息。

    Args:
        thread_id: 线程 ID
        token: Bearer token

    Returns:
        删除成功消息

    Raises:
        HTTPException: 线程不存在
    """
    _authenticate(token)

    deleted = store.delete_thread(thread_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="线程不存在",
        )

    return {"message": "线程已删除"}


@router.get("/{thread_id}/messages", response_model=list[MessageResponse], summary="获取消息列表")
def list_messages(
    thread_id: str,
    token: str = Query(..., description="Bearer token"),
) -> list[MessageResponse]:
    """获取指定线程的所有消息。

    Args:
        thread_id: 线程 ID
        token: Bearer token

    Returns:
        MessageResponse 消息列表

    Raises:
        HTTPException: 线程不存在
    """
    _authenticate(token)

    thread = store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="线程不存在",
        )

    messages = store.get_messages(thread_id)
    return [
        MessageResponse(
            id=m["id"],
            role=m["role"],
            content=m["content"],
            created_at=m["created_at"],
        )
        for m in messages
    ]
