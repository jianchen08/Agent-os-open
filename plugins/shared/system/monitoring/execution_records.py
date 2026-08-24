"""execution/records + sessions token-usage 域 handler（channel_api 退役批次 2 随迁）。

[来源] 本模块由 channel_api/routes_missing.py 的 execution_router（约 822-954 行）
与 sessions_router（约 1027-1037 行）搬迁而来：剥离 FastAPI 装饰器与 require_auth
依赖，纯 dict 返回；数据经内核只读能力桥 kernel_reads 组装（2026-08-19 调试中心
数据链修复的同一消费链），能力不可用时降级空结构（HTTP 200 空载荷，前端契约
不破坏）。

领域语义与原实现逐项对齐（前端 executionRecords.ts / sessions.ts 直接消费：
records/sessions/group-summary/tree/children/get/delete 形态不变）；
sessions token-usage 从 stub 接真：总 Token 数取 pipeline-state 摘要行的
track.total_tokens / cost_control.total_tokens，request_count 取该管道 run 次数
（内核 MessageRecord 不携带 token 计数字段，prompt/completion 无独立计数源）。

[交互] 读面仅消费内核能力（service-registry→pipeline-runs.list/messages.list、
pipeline-state→list）；clear-all 已做实（2026-08-24）——经 db-admin.
clear_execution_data 真删 9 表 + 内存 registry（users 保留），内核信封
非 200 时以 ClearExecutionDataError 原状态码透传；单条/按会话删除仍为
stub（内核消息模型无单条删除面，维持成功形态由前端消费）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import kernel_reads  # noqa: F401 —— 本插件内核只读能力桥（provider 由 server._on_load 注入）

logger = logging.getLogger(__name__)

# 全会话模式扫描的最近会话数（每会话一次内核能力调用，控制页面加载时延）
_ALL_SESSIONS_SCAN = 30
# 全会话模式每会话最多取的消息条数
_PER_SESSION_MSG_LIMIT = 100
# 单会话模式最多拉取的消息条数（超过此长度的会话只呈现最近这段）
_SESSION_MSG_FETCH_LIMIT = 500


async def recent_pipelines(limit: int = _ALL_SESSIONS_SCAN) -> list[dict[str, Any]]:
    """最近 N 个去重管道（runs 按 started_at 倒序，同管道多 run 只取最新）。

    agent-calls / search 域复用同一个"最近会话扫描"边界，控制内核能力调用次数。
    """
    runs = await kernel_reads.list_pipeline_runs(status=None, limit=500)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for run in runs:
        pid = run.get("pipeline_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append(run)
        if len(out) >= limit:
            break
    return out


async def find_state_row(session_id: str) -> dict[str, Any]:
    """按 pipeline_id 找管道 state 摘要行；不存在返回空 dict。"""
    for row in await kernel_reads.list_state_rows():
        if row.get("pipeline_id") == session_id:
            return row
    return {}


def _message_to_record(msg: dict[str, Any], session_id: str) -> dict[str, Any]:
    """内核 MessageRecord → 前端 ExecutionRecord（message_data = 拼装的消息快照）。

    content_preview 为读时重建的全文（内核注释：字段名保留 preview 以稳接口形状）；
    tool_calls_json/tool_result_json 在此解析为结构化对象。
    """
    role = msg.get("role") or "unknown"
    tool_calls: Any = None
    for key in ("tool_calls_json",):
        raw = msg.get(key)
        if raw:
            try:
                tool_calls = json.loads(raw)
            except (TypeError, ValueError):
                tool_calls = None
    tool_result: Any = None
    if msg.get("tool_result_json"):
        try:
            tool_result = json.loads(msg["tool_result_json"])
        except (TypeError, ValueError):
            tool_result = None
    message_data: dict[str, Any] = {
        "role": role,
        "content": msg.get("content_preview") or "",
        "tool_calls": tool_calls,
        "tool_call_id": msg.get("tool_call_id"),
        "reasoning_content": msg.get("reasoning_content"),
        "tool_result": tool_result,
        "error": msg.get("error"),
    }
    return {
        "id": msg.get("message_id") or "",
        "session_id": session_id,
        "sequence": msg.get("seq_in_branch"),
        "record_type": role,
        "status": msg.get("status") or ("completed" if role in ("user", "assistant", "system") else None),
        "depth": 0,
        "message_data": message_data,
        "created_at": msg.get("created_at") or "",
        "run_id": msg.get("run_id"),
    }


# ── execution/records 域（9 端点）───────────────────────────────────────


async def list_execution_records(
    session_id: str | None = None,
    parent_record_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """执行记录列表：单会话模式或全会话模式（最近 N 个会话的消息快照）。"""
    del parent_record_id  # 内核消息模型为扁平槽位，无父子记录
    if session_id:
        msgs = await kernel_reads.list_messages(session_id, limit=_SESSION_MSG_FETCH_LIMIT)
        records = [_message_to_record(m, session_id) for m in msgs]
        records.reverse()  # seq 升序读入 → 最新在前呈现
        return {
            "records": records[offset:offset + limit],
            "total": len(records),
            "session_id": session_id,
        }
    # 全会话模式：最近 N 个会话的消息拼装为全局时间倒序列表
    runs = await kernel_reads.list_pipeline_runs(limit=_ALL_SESSIONS_SCAN)
    all_records: list[dict[str, Any]] = []
    seen_pipelines: set[str] = set()
    for run in runs:
        pid = run.get("pipeline_id")
        # 同管道多 run（重试/续跑）去重，只取一次消息快照
        if not pid or pid in seen_pipelines:
            continue
        seen_pipelines.add(pid)
        msgs = await kernel_reads.list_messages(pid, limit=_PER_SESSION_MSG_LIMIT)
        all_records.extend(_message_to_record(m, pid) for m in msgs)
    all_records.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {
        "records": all_records[offset:offset + limit],
        "total": len(all_records),
        "session_id": None,
    }


async def get_execution_record_sessions() -> dict[str, Any]:
    """有记录的会话列表（runs 倒序去重 + state 摘要补充标题/消息数）。"""
    runs = await kernel_reads.list_pipeline_runs(limit=500)
    # 上游 pipeline-runs.list 约定倒序，此处显式排序兜底（对约定幂等、对乱序防御）
    runs = sorted(runs, key=lambda r: r.get("started_at") or "", reverse=True)
    states = {
        row.get("pipeline_id"): row
        for row in await kernel_reads.list_state_rows()
        if row.get("pipeline_id")
    }
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run in runs:  # runs 按 started_at 倒序，同管道多 run 去重取最新
        pid = run.get("pipeline_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        state = states.get(pid, {})
        sessions.append({
            "id": pid,
            "title": state.get("display_name") or state.get("name")
            or run.get("thread_id") or pid[:12],
            "created_at": run.get("started_at") or "",
            "updated_at": run.get("ended_at") or run.get("started_at") or "",
            # state 摘要缺失时置 None（前端隐藏"0 条"误导显示）而非 0
            "record_count": state.get("message_count"),
            "thread_id": run.get("thread_id"),
            "run_status": run.get("status"),
        })
    return {"sessions": sessions, "total": len(sessions)}


async def get_record_group_summary(session_id: str | None = None) -> dict[str, Any]:
    """记录分组概要（内核消息模型无 parent 层级，保持空结构）。"""
    del session_id
    return {"groups": [], "total_groups": 0}


async def get_execution_tree(session_id: str, max_depth: int = 5) -> dict[str, Any]:
    """执行记录树（内核消息模型为扁平槽位，无层级树，保持空结构）。"""
    return {"tree": [], "total": 0, "session_id": session_id, "max_depth": max_depth}


async def get_children_records(record_id: str) -> list[dict[str, Any]]:
    """子执行记录（存储层无 parent_record_id 概念，无子记录可返回）。"""
    del record_id
    return []


async def get_execution_record(record_id: str) -> dict[str, Any]:
    """单条执行记录：message_id 全局无索引，在最近 N 个会话的消息快照中线性查找。"""
    runs = await kernel_reads.list_pipeline_runs(limit=_ALL_SESSIONS_SCAN)
    seen_pipelines: set[str] = set()
    for run in runs:
        pid = run.get("pipeline_id")
        if not pid or pid in seen_pipelines:
            continue
        seen_pipelines.add(pid)
        msgs = await kernel_reads.list_messages(pid, limit=_PER_SESSION_MSG_LIMIT)
        for msg in msgs:
            if msg.get("message_id") == record_id:
                return _message_to_record(msg, pid)
    return {"id": record_id, "session_id": "", "message_data": {}, "created_at": ""}


async def clear_all_records(authorization: str = "") -> dict[str, Any]:
    """清理所有执行记录与轨迹（stub 做实 2026-08-24）。

    经 db-admin.clear_execution_data 能力真删：内核 9 表（runs/traces/blobs/
    branches/sessions/pipeline_sessions/pipeline_state/pipeline_checkpoints/
    message_slots，users 保留）+ 内存常驻 registry；清理前自动产出数据库
    快照备份（backup_path 回传）。有运行中管道时内核返回 409（detail 透传）。

    Raises:
        kernel_reads.ClearExecutionDataError: 原状态码透传（403/409/502/503/500），
        写面不降级假成功。
    """
    result = await kernel_reads.clear_execution_data(authorization=authorization)
    return {
        "success": True,
        "message": "所有执行记录与轨迹已清理（用户账号保留）",
        "cleared_count": result.get("cleared_count", 0),
        "tables": result.get("cleared"),
        "backup_path": result.get("backup_path"),
    }


# ── sessions token-usage 域（2 端点，stub 接真）─────────────────────────


def _state_total_tokens(state: dict[str, Any]) -> int:
    """从 state 摘要行取 Token 总量（track/cost_control 两个累计位，前者优先）。"""
    value = state.get("track.total_tokens") or state.get("cost_control.total_tokens") or 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def get_session_total_token_usage(session_id: str) -> dict[str, Any]:
    """会话总 Token 用量：state 行 token 累计 + 该管道 run 次数（请求数）。

    内核 MessageRecord 不携带 token 计数字段（usage 聚合在 pipeline-state 摘要行），
    prompt/completion 无独立计数源（置 0，字段保留对齐前端 SessionTokenUsageResponse）。
    """
    state = await find_state_row(session_id)
    runs = await kernel_reads.list_pipeline_runs(status=None, limit=500)
    request_count = sum(1 for r in runs if r.get("pipeline_id") == session_id)
    return {
        "session_id": session_id,
        "total_tokens": _state_total_tokens(state),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "request_count": request_count,
    }


async def get_session_context_token_usage(
    session_id: str,
    parent_execution_record_id: str | None = None,
) -> dict[str, Any]:
    """上下文 Token 用量：state 行 token 累计视为当前上下文占用（估算）。

    内核无逐消息 token 计数，无法精确切分上下文窗口占比，保持 is_estimated=True
    （前端 ContextTokenUsageResponse 按估算模式展示）。
    """
    del parent_execution_record_id  # 读面无逐记录 token 快照，忽略该参数
    state = await find_state_row(session_id)
    total = _state_total_tokens(state)
    return {
        "current_context_tokens": total,
        "is_estimated": True,
        "model": "default",
        "total_tokens": total,
    }