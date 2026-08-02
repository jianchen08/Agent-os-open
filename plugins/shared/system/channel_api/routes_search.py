"""搜索 API 路由（P2 搜索框合并-后端部分）。

提供 GET /api/v1/search 端点，统一搜索会话与消息内容：

- type=session: 按会话标题/意图做子串匹配（LIKE 语义，大小写不敏感）
- type=message: 按消息内容做子串匹配（读取 ExecutionRecordStorage 执行记录）
- type=all: 同时返回会话与消息

存储事实（与方案对齐）：
- 会话数据存于 MemoryStore（channels.api.memory_store，JSON 持久化）
- 消息存于 ExecutionRecordStorage（infrastructure，YAML 分片持久化）
搜索按真实存储层做子串匹配，不依赖 SQLite（当前库为空表）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from deps import APIError, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/search", tags=["搜索"])

# 允许的搜索类型
_SEARCH_TYPES = ("all", "session", "message")


def _get_storage() -> Any:
    """获取全局 ExecutionRecordStorage 实例，不可用时返回 None。"""
    try:
        from infrastructure.service_access import get_execution_record_storage  # noqa: PLC0415

        return get_execution_record_storage()
    except Exception:
        logger.warning("获取 ExecutionRecordStorage 失败，消息搜索将返回空", exc_info=True)
        return None


def _search_sessions(user_id: str, needle: str, limit: int) -> list[dict[str, Any]]:
    """按标题/意图搜索当前用户的会话（子串匹配，大小写不敏感）。"""
    from memory_store import store  # noqa: PLC0415

    hits: list[dict[str, Any]] = []
    for thread in store.threads.values():
        if thread.get("user_id") != user_id:
            continue
        title = (thread.get("title") or thread.get("intent") or "")
        if needle in title.lower():
            hits.append(
                {
                    "id": thread.get("id", ""),
                    "title": title,
                    "updated_at": thread.get("updated_at", ""),
                    "message_count": thread.get("message_count", 0),
                }
            )
            if len(hits) >= limit:
                break
    return hits


def _search_messages(needle: str, limit: int) -> list[dict[str, Any]]:
    """按内容搜索执行记录消息（子串匹配，大小写不敏感）。"""
    storage = _get_storage()
    if storage is None:
        return []
    hits: list[dict[str, Any]] = []
    for record in storage.search_records(needle, limit=limit):
        hits.append(
            {
                "id": record.record_id,
                "session_id": record.pipeline_run_id,
                "role": record.role or record.type or "user",
                "content": (record.content or "")[:200],
                "timestamp": record.created_at or "",
                "sequence": getattr(record, "sequence", 0),
            }
        )
    return hits


@router.get("", summary="统一搜索会话与消息")
def search(
    q: str = Query(default="", description="搜索关键词"),
    type: str = Query(default="all", description="搜索类型：all/session/message"),
    limit: int = Query(default=20, ge=1, le=100, description="每类结果数量上限"),
    _user: dict = Depends(require_auth),
) -> dict[str, Any]:
    """统一搜索入口。

    按 q 做子串匹配，type 控制搜索范围：
    - session: 会话标题/意图
    - message: 消息内容
    - all: 两者都搜

    q 为空或仅空白时返回空结果（不报错，符合搜索框清空场景）。
    """
    if type not in _SEARCH_TYPES:
        raise APIError(
            status_code=422,
            error_code="VAL_ENUM_7002",
            message=f"type 必须是 {'/'.join(_SEARCH_TYPES)} 之一",
        )

    needle = q.strip().lower()
    user_id = _user.get("sub", "")

    if not needle:
        return {"query": q, "type": type, "sessions": [], "messages": []}

    sessions: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []

    if type in ("all", "session"):
        sessions = _search_sessions(user_id, needle, limit)
    if type in ("all", "message"):
        messages = _search_messages(needle, limit)

    return {
        "query": q,
        "type": type,
        "sessions": sessions,
        "messages": messages,
    }
