"""管道诊断读面：step 级 trace 时间线 + pipeline_state 全字段。

数据经内核只读能力桥 kernel_reads（traces.list_by_pipeline /
pipeline-runs.list_by_pipeline / pipeline-state.list / db-admin.table_query），
能力不可用时降级空结构（HTTP 200 空载荷，前端契约不破坏）。

两个端点（调试中心）：
- GET /ext/monitoring/traces?pipeline_id=&limit=
  step 级执行轨迹轻量投影（summary/llm_usage/error/tool_call_count），
  patch_data 原样随行（前端展开看原始 Patch）。
- GET /ext/monitoring/pipeline-state?pipeline_id=
  pipeline_state 表全字段（不经 export_fields 白名单裁剪，调试语义）+
  该管道全部 run + state 摘要行（若在）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import kernel_reads

logger = logging.getLogger(__name__)

# 单条摘要与人读字段的最大长度（超出截断，全文走 patch_data 展开）
_SUMMARY_MAX = 160

# pipeline_state 单管道字段行数上限（state 键数量级 ~百，500 足够）
_STATE_FIELD_LIMIT = 500


def _as_dict(value: Any) -> dict[str, Any]:
    """patch_data 形态收敛：str 尝试 JSON 解析，非 dict 归空。"""
    if isinstance(value, str) and value:
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _as_text(value: Any) -> str | None:
    """error 类字段收敛为字符串（dict/list 序列化，空值归 None）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _first_str(*candidates: Any) -> str | None:
    """取第一个非空字符串。"""
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c
    return None


def _trace_summary(patch: dict[str, Any]) -> str | None:
    """人读摘要，按信息密度取：router 回复 > raw_result 文本/消息 > 元数据消息。"""
    router = patch.get("router")
    if isinstance(router, dict):
        text = _first_str(router.get("last_response_text"))
        if text:
            return text[:_SUMMARY_MAX]
    raw_result = patch.get("raw_result")
    if isinstance(raw_result, str) and raw_result.strip():
        return raw_result[:_SUMMARY_MAX]
    if isinstance(raw_result, dict):
        metadata = raw_result.get("metadata")
        message = _first_str(
            metadata.get("message") if isinstance(metadata, dict) else None
        )
        if message:
            return message[:_SUMMARY_MAX]
    raw_error = patch.get("raw_error")
    if raw_error:
        return f"错误: {_as_text(raw_error)}"[:_SUMMARY_MAX]
    return None


def project_trace(entry: dict[str, Any]) -> dict[str, Any]:
    """TraceEntry → 前端时间线行（patch_data 保留原文供展开）。"""
    patch = _as_dict(entry.get("patch_data"))
    llm_usage = patch.get("llm_usage")
    tool_calls = patch.get("_executed_tool_calls")
    return {
        "trace_id": entry.get("trace_id") or "",
        "run_id": entry.get("run_id") or "",
        "seq": entry.get("seq_in_branch"),
        "plugin_id": entry.get("plugin_id") or "",
        "patch_type": entry.get("patch_type") or "",
        "created_at": entry.get("created_at") or "",
        "iteration": patch.get("iteration"),
        "summary": _trace_summary(patch),
        "llm_usage": llm_usage if isinstance(llm_usage, dict) else None,
        "error": _as_text(patch.get("raw_error") or patch.get("error")),
        "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
        "patch_data": patch,
    }


async def list_pipeline_traces(pipeline_id: str, limit: int = 200) -> dict[str, Any]:
    """单管道 step 级轨迹（seq 升序，尾部截取最近 limit 条）。"""
    if not pipeline_id:
        return {"traces": [], "total": 0, "pipeline_id": pipeline_id}
    entries = await kernel_reads.list_traces(pipeline_id)
    projected = [project_trace(e) for e in entries if isinstance(e, dict)]
    total = len(projected)
    return {
        "traces": projected[-limit:] if limit > 0 else projected,
        "total": total,
        "pipeline_id": pipeline_id,
    }


async def get_pipeline_state_full(pipeline_id: str) -> dict[str, Any]:
    """单管道 state 全字段 + runs 列表 + 摘要行（调试语义，不做白名单裁剪）。

    fields.field_value 为 DB 原始字符串（JSON 序列化由引擎写侧决定），
    前端展示时尝试 JSON pretty，解析失败按原文呈现。
    """
    if not pipeline_id:
        return {"pipeline_id": pipeline_id, "fields": [], "runs": [], "summary": None}
    table = await kernel_reads.query_table(
        "pipeline_state",
        filter=[f"pipeline_id:eq:{pipeline_id}"],
        sort="field_key:asc",
        limit=_STATE_FIELD_LIMIT,
    )
    raw_rows = table.get("rows")
    rows = raw_rows if isinstance(raw_rows, list) else []
    fields = [
        {
            "field_key": r.get("field_key"),
            "field_value": r.get("field_value"),
            "updated_at": r.get("updated_at"),
        }
        for r in rows
        if isinstance(r, dict)
    ]
    runs = await kernel_reads.list_runs_by_pipeline(pipeline_id)
    summary = next(
        (
            row
            for row in await kernel_reads.list_state_rows()
            if isinstance(row, dict) and row.get("pipeline_id") == pipeline_id
        ),
        None,
    )
    return {
        "pipeline_id": pipeline_id,
        "fields": fields,
        "runs": runs,
        "summary": summary,
    }
