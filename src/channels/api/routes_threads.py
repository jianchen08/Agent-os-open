"""线程与消息相关 API 路由。

提供线程的 CRUD 操作和消息查询接口，所有接口需要 Bearer token 认证。
支持 Authorization: Bearer <token> 请求头和 ?token= query 参数两种认证方式。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Query, status

from channels.api.auth import get_current_user
from channels.api.models import (
    MessageResponse,
    ThreadCreate,
    ThreadResponse,
    store,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/threads", tags=["线程"])


def _authenticate(authorization: str, token: str) -> dict:
    """验证 Bearer token 并返回用户信息。

    优先从 Authorization 请求头提取 token，其次使用 query 参数。

    Args:
        authorization: Authorization 请求头值
        token: query 参数中的 token

    Returns:
        用户信息字典

    Raises:
        HTTPException: token 缺失或无效
    """
    # 优先从 Authorization 头提取 Bearer token
    actual_token = ""
    if authorization and authorization.startswith("Bearer "):
        actual_token = authorization[7:]
    elif token:
        actual_token = token

    if not actual_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_info = get_current_user(actual_token)
    if user_info is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或过期的认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_info


def _build_thread_response(t: dict) -> ThreadResponse:
    """将存储层的线程字典转换为前端期望的 ThreadResponse 格式。

    字段映射：id -> thread_id, title -> intent,
    并添加 current_state 和 agent_id 默认值。

    Args:
        t: 存储层返回的线程字典

    Returns:
        ThreadResponse 与前端 mapThreadToSession 格式对齐
    """
    return ThreadResponse(
        thread_id=t["id"],
        intent=t.get("title") or None,
        created_at=t["created_at"],
        updated_at=t["updated_at"],
    )


@router.get("", response_model=list[ThreadResponse], summary="获取线程列表")
def list_threads(
    authorization: str = Header(default=""),
    token: str = Query(default="", description="Bearer token（备选）"),
) -> list[ThreadResponse]:
    """获取当前用户的所有线程列表。

    支持 Authorization: Bearer <token> 请求头和 ?token= query 参数。

    Args:
        authorization: Authorization 请求头
        token: query 参数中的 Bearer token

    Returns:
        ThreadResponse 列表
    """
    user_info = _authenticate(authorization, token)
    threads = store.get_user_threads(user_info["sub"])

    return [_build_thread_response(t) for t in threads]


@router.post("", response_model=ThreadResponse, status_code=status.HTTP_201_CREATED, summary="创建线程")
def create_thread(
    body: ThreadCreate,
    authorization: str = Header(default=""),
    token: str = Query(default="", description="Bearer token（备选）"),
) -> ThreadResponse:
    """创建新线程。

    支持 Authorization: Bearer <token> 请求头和 ?token= query 参数。

    Args:
        body: 线程创建请求，包含可选标题
        authorization: Authorization 请求头
        token: query 参数中的 Bearer token

    Returns:
        ThreadResponse 新创建的线程
    """
    user_info = _authenticate(authorization, token)
    thread = store.create_thread(
        user_id=user_info["sub"],
        title=body.title,
    )

    return _build_thread_response(thread)


@router.get("/{thread_id}", response_model=ThreadResponse, summary="获取线程详情")
def get_thread(
    thread_id: str,
    authorization: str = Header(default=""),
    token: str = Query(default="", description="Bearer token（备选）"),
) -> ThreadResponse:
    """获取指定线程的详情。

    支持 Authorization: Bearer <token> 请求头和 ?token= query 参数。

    Args:
        thread_id: 线程 ID
        authorization: Authorization 请求头
        token: query 参数中的 Bearer token

    Returns:
        ThreadResponse 线程详情

    Raises:
        HTTPException: 线程不存在或无权限
    """
    _authenticate(authorization, token)

    thread = store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="线程不存在",
        )

    return _build_thread_response(thread)


@router.delete("/{thread_id}", summary="删除线程")
def delete_thread(
    thread_id: str,
    authorization: str = Header(default=""),
    token: str = Query(default="", description="Bearer token（备选）"),
) -> dict[str, str]:
    """删除指定线程及其所有消息。

    支持 Authorization: Bearer <token> 请求头和 ?token= query 参数。

    Args:
        thread_id: 线程 ID
        authorization: Authorization 请求头
        token: query 参数中的 Bearer token

    Returns:
        删除成功消息

    Raises:
        HTTPException: 线程不存在
    """
    _authenticate(authorization, token)

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
    authorization: str = Header(default=""),
    token: str = Query(default="", description="Bearer token（备选）"),
) -> list[MessageResponse]:
    """获取指定线程的所有消息。

    支持 Authorization: Bearer <token> 请求头和 ?token= query 参数。

    Args:
        thread_id: 线程 ID
        authorization: Authorization 请求头
        token: query 参数中的 Bearer token

    Returns:
        MessageResponse 消息列表

    Raises:
        HTTPException: 线程不存在
    """
    _authenticate(authorization, token)

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
            thread_id=thread_id,
            role=m["role"],
            content=m["content"],
            timestamp=m["created_at"],
        )
        for m in messages
    ]
