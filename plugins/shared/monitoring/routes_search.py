"""search 域 handler：统一搜索会话与消息，数据源为 db-read 契约与内核能力桥——

- 会话（type=session）：db-read sessions 面（tenant 档）title 子串匹配
  （大小写不敏感），租户收窄由契约内建（身份经 Authorization 原头内核复权），
  命中 id = thread_id（与前端会话列表同空间）；
- 消息（type=message）：最近 N 个去重管道（execution_records.recent_pipelines，
  控制能力调用次数）的 messages.list 读时重建全文（content_preview）子串匹配，
  管道集按租户可见集（pipeline_state run_status 簿记键，经 pipeline_state
  tenant 档面契约收窄）过滤；
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
_MESSAGE_SESSION_SCAN = er.ALL_SESSIONS_SCAN
# 每会话最多取的消息条数（消息搜索匹配上限）
_MESSAGE_PER_SESSION_LIMIT = er.PER_SESSION_MSG_LIMIT

# 租户可见管道集的扫描上界（state 簿记行按 updated_at 倒序取最近 N 条）：
# 与全会话扫描边界同量级，防止超大面积租户的全集合物化。
_TENANT_PIPELINE_SCAN = max(er.ALL_SESSIONS_SCAN, 200)


async def _tenant_pipeline_ids(
    tenant_id: str, authorization: str = "", limit: int = _TENANT_PIPELINE_SCAN
) -> set[str] | None:
    """租户可见管道集：db-read 契约查 pipeline_state 运行簿记键（runs 表退役
    ADR 2026-09-18；管道存在性锚 = run_status 簿记行）。

    数据面 = pipeline_state tenant 档（db-read 契约服务，身份经 authorization
    原头由内核复权，租户收窄契约层内建——调用方传不了收窄值）；内层 :tenant
    谓词 = 收窄后限量（LIMIT 语义与收窄前一致）。

    返回 None = 租户身份缺失或契约查询失败——调用方 fail-closed 返回空集，
    绝不全量查询兜底（否则搜索退化为跨租户消息/会话泄露）。
    """
    if not tenant_id:
        return None
    try:
        rows = await kernel_reads.db_read_query(
            face="pipeline_state",
            sql="SELECT tenant_id, pipeline_id FROM pipeline_state"
            " WHERE tenant_id = :tenant AND field_key = 'run_status'"
            " ORDER BY updated_at DESC LIMIT :limit",
            params={"tenant": tenant_id, "limit": int(limit)},
            authorization=authorization,
        )
    except kernel_reads.DbReadQueryError:
        return None
    return {r[1] for r in rows if len(r) > 1 and r[1]}


async def _search_sessions(
    needle: str, limit: int, tenant_id: str, authorization: str = ""
) -> list[dict[str, Any]]:
    """按标题搜索会话——数据源是 sessions 表（会话标题唯一真值）。

    pipeline_state 摘要行不含标题字段（字段白名单仅 agent.id/workspace/track.*
    等执行态键），从 state 搜标题恒空。数据面 = sessions tenant 档（db-read
    契约服务，租户边界契约层内建，信任锚同 _tenant_pipeline_ids：内核复权的
    请求方身份），内层 :tenant 谓词保证收窄后限量；命中 id 为 thread_id——
    与前端侧边栏会话列表的 id 同空间，可直接映射。
    needle 已在 search() 入口 lower()；LIKE 通配符显式转义后作绑定参数传入，
    用户输入不充当模式。
    """
    if not tenant_id:
        return []
    pattern = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    try:
        rows = await kernel_reads.db_read_query(
            face="sessions",
            sql="SELECT tenant_id, thread_id, title, COALESCE(updated_at, '') FROM sessions"
            " WHERE tenant_id = :tenant"
            " AND lower(COALESCE(title, '')) LIKE :pattern ESCAPE '\\'"
            " ORDER BY COALESCE(last_active_at, updated_at) DESC LIMIT :limit",
            params={"tenant": tenant_id, "pattern": pattern, "limit": int(limit)},
            authorization=authorization,
        )
    except kernel_reads.DbReadQueryError:
        return []
    return [
        {"id": r[1], "title": r[2] or "", "updated_at": r[3], "message_count": 0}
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
                 tenant_id: str = "", authorization: str = "") -> dict[str, Any]:
    """统一搜索入口：q 子串匹配，type 控制范围（all/session/message）。

    租户过滤（M3，与 tool-calls 域同纪律）：tenant_id 必须来自内核注入的
    X-AgentOS-Tenant 头（已认证租户）；缺失/为空一律空集（fail-closed）——
    搜索面含会话标题与消息正文，全量查询即跨租户泄露。authorization =
    入站 Authorization 原头值（db-read 契约身份链，内核复权后内建收窄）。
    """
    if type not in _SEARCH_TYPES:
        raise ValueError(f"type 必须是 {'/'.join(_SEARCH_TYPES)} 之一")

    needle = q.strip().lower()
    if not needle:
        return {"query": q, "type": type, "sessions": [], "messages": []}

    allowed = await _tenant_pipeline_ids(tenant_id, authorization)
    if allowed is None:
        return {"query": q, "type": type, "sessions": [], "messages": []}

    n = max(1, min(int(limit), 100))
    sessions: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    if type in ("all", "session"):
        sessions = await _search_sessions(needle, n, tenant_id, authorization)
    if type in ("all", "message"):
        messages = await _search_messages(needle, n, allowed)

    return {"query": q, "type": type, "sessions": sessions, "messages": messages}
