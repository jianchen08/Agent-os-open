"""search 域 handler：统一搜索会话与消息，数据源为内核能力桥——

- 会话（type=session）：pipeline-state 摘要行标题（display_name/name/task.goal/
  thread_id）子串匹配（大小写不敏感）；
- 消息（type=message）：最近 N 个去重管道（execution_records.recent_pipelines，
  控制能力调用次数）的 messages.list 读时重建全文（content_preview）子串匹配；
- type=all：两者都搜（前端 Sidebar 搜索框默认）。

响应形态对齐前端 services/api/search.ts（SearchResponse/SessionSearchHit/
MessageSearchHit：sessions[{id,title,updated_at,message_count}]、
messages[{id,session_id,role,content,timestamp,sequence}]）。

q 为空/仅空白 → 空结果（不报错，符合搜索框清空场景）；type 非法 → ValueError
（dispatch 层转 422，语义同原 APIError VAL_ENUM_7002）。
"""

from __future__ import annotations

import logging
from typing import Any

import kernel_reads  # noqa: F401 —— 本插件内核只读能力桥

import execution_records as er  # 复用最近管道扫描边界（同插件内部模块）

logger = logging.getLogger(__name__)

# 允许的搜索类型（前端 SearchType 三值全量支持）
_SEARCH_TYPES = ("all", "session", "message")

# 消息内容截断长度（对齐前端 MessageSearchHit.content 注释：截断到 200 字符）
_MESSAGE_CONTENT_MAX = 200

# 消息搜索的会话扫描边界（与 execution 域全会话模式一致）
_MESSAGE_SESSION_SCAN = er._ALL_SESSIONS_SCAN
# 每会话最多取的消息条数（消息搜索匹配上限）
_MESSAGE_PER_SESSION_LIMIT = er._PER_SESSION_MSG_LIMIT


async def _search_sessions(needle: str, limit: int) -> list[dict[str, Any]]:
    """按标题/意图搜索会话（state 摘要行子串匹配，大小写不敏感）。"""
    hits: list[dict[str, Any]] = []
    for row in await kernel_reads.list_state_rows():
        title = (row.get("display_name") or row.get("name")
                 or row.get("task.goal") or row.get("thread_id") or "")
        if not title or needle not in str(title).lower():
            continue
        hits.append({
            "id": row.get("pipeline_id") or "",
            "title": title,
            "updated_at": row.get("ended") or row.get("started_at") or "",
            "message_count": row.get("message_count") or 0,
        })
        if len(hits) >= limit:
            break
    return hits


async def _search_messages(needle: str, limit: int) -> list[dict[str, Any]]:
    """按内容搜索消息（最近 N 个去重管道的 messages.list 全文子串匹配）。"""
    hits: list[dict[str, Any]] = []
    for run in await er.recent_pipelines(_MESSAGE_SESSION_SCAN):
        pid = run.get("pipeline_id") or ""
        msgs = await kernel_reads.list_messages(pid, limit=_MESSAGE_PER_SESSION_LIMIT)
        for msg in msgs:
            content = msg.get("content_preview") or ""
            if not content or needle not in str(content).lower():
                continue
            hits.append({
                "id": msg.get("message_id") or "",
                "session_id": pid,
                "role": msg.get("role") or "unknown",
                "content": str(content)[:_MESSAGE_CONTENT_MAX],
                "timestamp": msg.get("created_at") or "",
                "sequence": msg.get("seq_in_branch"),
            })
            if len(hits) >= limit:
                return hits
    return hits


async def search(q: str = "", type: str = "all", limit: int = 20) -> dict[str, Any]:
    """统一搜索入口：q 子串匹配，type 控制范围（all/session/message）。"""
    if type not in _SEARCH_TYPES:
        raise ValueError(f"type 必须是 {'/'.join(_SEARCH_TYPES)} 之一")

    needle = q.strip().lower()
    if not needle:
        return {"query": q, "type": type, "sessions": [], "messages": []}

    n = max(1, min(int(limit), 100))
    sessions: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    if type in ("all", "session"):
        sessions = await _search_sessions(needle, n)
    if type in ("all", "message"):
        messages = await _search_messages(needle, n)

    return {"query": q, "type": type, "sessions": sessions, "messages": messages}