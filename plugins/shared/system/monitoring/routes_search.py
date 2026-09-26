"""search 域 handler：统一搜索会话与消息，数据源为内核库与内核能力桥——

- 会话（type=session）：sessions 表 title 子串匹配（大小写不敏感），按
  sessions.tenant_id 租户隔离，命中 id = thread_id（与前端会话列表同空间）；
- 消息（type=message）：最近 N 个去重管道（execution_records.recent_pipelines，
  控制能力调用次数）的 messages.list 读时重建全文（content_preview）子串匹配，
  管道集按租户可见集（runs 表 tenant_id 直查）过滤；
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

import execution_records as er  # 复用最近管道扫描边界（同插件内部模块）
import kernel_reads  # noqa: F401 —— 本插件内核只读能力桥

logger = logging.getLogger(__name__)

# 允许的搜索类型（前端 SearchType 三值全量支持）
_SEARCH_TYPES = ("all", "session", "message")

# 消息内容截断长度（对齐前端 MessageSearchHit.content 注释：截断到 200 字符）
_MESSAGE_CONTENT_MAX = 200

# 消息搜索的会话扫描边界（与 execution 域全会话模式一致）
_MESSAGE_SESSION_SCAN = er._ALL_SESSIONS_SCAN
# 每会话最多取的消息条数（消息搜索匹配上限）
_MESSAGE_PER_SESSION_LIMIT = er._PER_SESSION_MSG_LIMIT

# 租户可见管道集的扫描上界（runs 按 created_at 倒序取最近 N 条）：
# 与全会话扫描边界同量级，防止超大面积租户的全集合物化。
_TENANT_PIPELINE_SCAN = max(er._ALL_SESSIONS_SCAN, 200)


def _tenant_pipeline_ids(tenant_id: str, limit: int = _TENANT_PIPELINE_SCAN) -> set[str] | None:
    """租户可见管道集：直查内核 runs 表（信任锚 = 内核注入的租户身份头）。

    返回 None = 租户身份缺失或库不可用——调用方 fail-closed 返回空集，
    绝不全量查询兜底（否则搜索退化为跨租户消息/会话泄露）。
    """
    import os
    import sqlite3

    from kernel_db import kernel_db_path

    if not tenant_id:
        return None
    db_path = kernel_db_path()
    if not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path)
        try:
            # runs 表退役（ADR 2026-09-18）：执行过 = 有 run_status 簿记键
            rows = conn.execute(
                "SELECT pipeline_id FROM pipeline_state WHERE tenant_id = ? "
                "AND field_key = 'run_status' ORDER BY updated_at DESC LIMIT ?",
                (tenant_id, int(limit)),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return {r[0] for r in rows if r[0]}


def _search_sessions(
    needle: str, limit: int, tenant_id: str
) -> list[dict[str, Any]]:
    """按标题搜索会话——数据源是 sessions 表（会话标题唯一真值）。

    pipeline_state 摘要行不含标题字段（字段白名单仅 agent.id/workspace/track.*
    等执行态键），从 state 搜标题恒空。租户边界 = sessions.tenant_id 等值过滤
    （信任锚同 _tenant_pipeline_ids：内核注入的租户身份头），命中 id 为
    thread_id——与前端侧边栏会话列表的 id 同空间，可直接映射。needle 已在
    search() 入口 lower()；LIKE 通配符显式转义，用户输入不充当模式。
    """
    import os
    import sqlite3

    from kernel_db import kernel_db_path

    if not tenant_id:
        return []
    db_path = kernel_db_path()
    if not os.path.isfile(db_path):
        return []
    pattern = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT thread_id, title, COALESCE(updated_at, '') FROM sessions "
                "WHERE tenant_id = ? AND lower(COALESCE(title, '')) LIKE ? ESCAPE '\\' "
                "ORDER BY COALESCE(last_active_at, updated_at) DESC LIMIT ?",
                (tenant_id, pattern, int(limit)),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [
        {"id": r[0], "title": r[1] or "", "updated_at": r[2], "message_count": 0}
        for r in rows
    ]


async def _search_messages(
    needle: str, limit: int, allowed_pipelines: set[str] | None
) -> list[dict[str, Any]]:
    """按内容搜索消息（最近 N 个去重管道的 messages.list 全文子串匹配）。

    只扫描租户可见集内的管道（None = 不可见一切，fail-closed）。
    """
    if not allowed_pipelines:
        return []
    hits: list[dict[str, Any]] = []
    for run in await er.recent_pipelines(_MESSAGE_SESSION_SCAN):
        pid = run.get("pipeline_id") or ""
        if pid not in allowed_pipelines:
            continue
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


async def search(q: str = "", type: str = "all", limit: int = 20,
                 tenant_id: str = "") -> dict[str, Any]:
    """统一搜索入口：q 子串匹配，type 控制范围（all/session/message）。

    租户过滤（M3，与 tool-calls 域同纪律）：tenant_id 必须来自内核注入的
    X-AgentOS-Tenant 头（已认证租户）；缺失/为空一律空集（fail-closed）——
    搜索面含会话标题与消息正文，全量查询即跨租户泄露。
    """
    if type not in _SEARCH_TYPES:
        raise ValueError(f"type 必须是 {'/'.join(_SEARCH_TYPES)} 之一")

    needle = q.strip().lower()
    if not needle:
        return {"query": q, "type": type, "sessions": [], "messages": []}

    allowed = _tenant_pipeline_ids(tenant_id)
    if allowed is None:
        return {"query": q, "type": type, "sessions": [], "messages": []}

    n = max(1, min(int(limit), 100))
    sessions: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    if type in ("all", "session"):
        sessions = _search_sessions(needle, n, tenant_id)
    if type in ("all", "message"):
        messages = await _search_messages(needle, n, allowed)

    return {"query": q, "type": type, "sessions": sessions, "messages": messages}
