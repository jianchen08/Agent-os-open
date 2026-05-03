"""线程与消息相关 API 路由。

提供线程的 CRUD 操作和消息查询接口，所有接口需要 Bearer token 认证。
使用共享的 require_auth 依赖注入统一认证逻辑。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, status

from channels.api.deps import APIError, require_auth
from channels.api.models import (
    MessageResponse,
    ThreadCreate,
    ThreadResponse,
    ThreadUpdate,
    store,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/threads", tags=["线程"])


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


@router.get(
    "",
    response_model=list[ThreadResponse],
    summary="获取线程列表",
)
def list_threads(
    _user: dict = Depends(require_auth),
) -> list[ThreadResponse]:
    """获取当前用户的所有线程列表。

    Returns:
        ThreadResponse 列表
    """
    threads = store.get_user_threads(_user["sub"])
    return [_build_thread_response(t) for t in threads]


@router.post(
    "",
    response_model=ThreadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建线程",
)
def create_thread(
    body: ThreadCreate,
    _user: dict = Depends(require_auth),
) -> ThreadResponse:
    """创建新线程。

    Args:
        body: 线程创建请求，包含可选标题

    Returns:
        ThreadResponse 新创建的线程
    """
    thread = store.create_thread(
        user_id=_user["sub"],
        title=body.title,
    )
    return _build_thread_response(thread)


@router.get(
    "/{thread_id}",
    response_model=ThreadResponse,
    summary="获取线程详情",
)
def get_thread(
    thread_id: str,
    _user: dict = Depends(require_auth),
) -> ThreadResponse:
    """获取指定线程的详情。

    Args:
        thread_id: 线程 ID

    Returns:
        ThreadResponse 线程详情

    Raises:
        APIError: 线程不存在 (404)
    """
    thread = store.get_thread(thread_id)
    if thread is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="线程不存在",
        )
    return _build_thread_response(thread)


@router.patch(
    "/{thread_id}",
    response_model=ThreadResponse,
    summary="更新线程",
)
def update_thread(
    thread_id: str,
    body: ThreadUpdate,
    _user: dict = Depends(require_auth),
) -> ThreadResponse:
    """更新指定线程的标题。

    Args:
        thread_id: 线程 ID
        body: 线程更新请求

    Returns:
        ThreadResponse 更新后的线程

    Raises:
        APIError: 线程不存在 (404)
    """
    thread = store.update_thread(thread_id, title=body.title)
    if thread is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="线程不存在",
        )
    return _build_thread_response(thread)


@router.delete(
    "/{thread_id}",
    summary="删除线程",
)
def delete_thread(
    thread_id: str,
    _user: dict = Depends(require_auth),
) -> dict[str, str]:
    """删除指定线程及其所有消息。

    Args:
        thread_id: 线程 ID

    Returns:
        删除成功消息

    Raises:
        APIError: 线程不存在 (404)
    """
    deleted = store.delete_thread(thread_id)
    if not deleted:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="线程不存在",
        )
    return {"message": "线程已删除"}


@router.get(
    "/{thread_id}/messages",
    response_model=list[MessageResponse],
    summary="获取消息列表",
)
def list_messages(
    thread_id: str,
    _user: dict = Depends(require_auth),
) -> list[MessageResponse]:
    """获取指定线程的所有消息。

    Args:
        thread_id: 线程 ID

    Returns:
        MessageResponse 消息列表

    Raises:
        APIError: 线程不存在 (404)
    """
    thread = store.get_thread(thread_id)
    if thread is None:
        raise APIError(
            status_code=404,
            error_code="API_NOTF_2004",
            message="线程不存在",
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


@router.get(
    "/messages/search",
    response_model=list[MessageResponse],
    summary="搜索消息",
)
def search_messages(
    query: str = Query(..., description="搜索关键词"),
    limit: int = Query(default=20, ge=1, le=100, description="返回数量"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    _user: dict = Depends(require_auth),
) -> list[MessageResponse]:
    """在所有线程中搜索包含关键词的消息。

    Args:
        query: 搜索关键词
        limit: 返回数量
        offset: 偏移量

    Returns:
        MessageResponse 匹配的消息列表
    """
    results = store.search_messages(query=query, limit=limit, offset=offset)
    return [
        MessageResponse(
            id=m["id"],
            thread_id=m["thread_id"],
            role=m["role"],
            content=m["content"],
            timestamp=m["created_at"],
        )
        for m in results
    ]
