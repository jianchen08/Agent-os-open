"""agent-calls 域 handler：以「管道运行快照 = 一次 agent 调用」为视角，
从内核能力桥组装调用记录——

- 一次 run（pipeline-runs.list）即一次 agent 调用：run_id/pipeline_id/thread_id/
  status/started_at/ended_at/total_tokens/total_seconds；
- 统计视图（statistics）：run 数 = 调用数，status 成功判定（completed/success/
  succeeded/ok）聚合成功率，total_seconds 求平均耗时；
- 详情（{execution_id}）：按 run_id 精确匹配（兼容旧前端以 pipeline_id 调用），
  附该管道最近消息快照（message_count + 前 20 条 messages 记录，复用
  execution_records._message_to_record 形态，前端可衔接执行记录视图）。

能力不可用时 kernel_reads 降级空结构（HTTP 200 空载荷）。
"""

from __future__ import annotations

import logging
from typing import Any

import kernel_reads  # noqa: F401 —— 本插件内核只读能力桥

import execution_records as er  # 复用消息快照/最近管道扫描 helper（同插件内部模块）

logger = logging.getLogger(__name__)

# run 状态 → 成功判定（内核 run status 取值集合，兼容大小写）
_SUCCESS_STATUSES = frozenset({"completed", "success", "succeeded", "ok"})

# 详情端点附带的最近消息条数（供前端上下文预览，控制载荷）
_DETAIL_MESSAGE_LIMIT = 20


def _run_to_call(run: dict[str, Any]) -> dict[str, Any]:
    """PipelineRunInfo 行 → agent-calls 列表项（一次 run = 一次 agent 调用）。"""
    return {
        "id": run.get("run_id") or "",
        "pipeline_id": run.get("pipeline_id"),
        "thread_id": run.get("thread_id"),
        "agent_id": run.get("agent_id"),
        "status": run.get("status") or "unknown",
        "started_at": run.get("started_at") or "",
        "ended_at": run.get("ended_at"),
        "total_tokens": run.get("total_tokens"),
        "total_seconds": run.get("total_seconds"),
    }


async def list_agent_calls(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """调用记录列表（最近最多 500 条 run 快照，时间倒序由内核保证）。"""
    runs = await kernel_reads.list_pipeline_runs(status=None, limit=500)
    items = [_run_to_call(r) for r in runs]
    return {"items": items[offset:offset + limit], "total": len(items)}


async def get_agent_call_statistics() -> dict[str, Any]:
    """调用统计（agent 调用视角）：调用数/成功率/平均耗时（含失败数）。"""
    runs = await kernel_reads.list_pipeline_runs(status=None, limit=500)
    total = len(runs)
    succeeded = sum(
        1 for r in runs if (str(r.get("status") or "").lower() in _SUCCESS_STATUSES)
    )
    durations = [
        d for r in runs
        if isinstance((d := r.get("total_seconds")), (int, float))
    ]
    avg_duration_ms = (sum(durations) / len(durations) * 1000.0) if durations else 0.0
    return {
        "total_calls": total,
        "success_rate": round(succeeded / total * 100, 2) if total else 0.0,
        "avg_duration_ms": round(avg_duration_ms, 1),
        "failed_calls": total - succeeded,
    }


async def get_agent_call(execution_id: str) -> dict[str, Any]:
    """调用详情：按 run_id 匹配（兜底 pipeline_id），附最近消息快照。

    未命中返回 ``{"id": execution_id, "status": "not_found"}``（与原 stub 形态一致）。
    """
    runs = await kernel_reads.list_pipeline_runs(status=None, limit=500)
    run = next(
        (r for r in runs if r.get("run_id") == execution_id),
        next((r for r in runs if r.get("pipeline_id") == execution_id), None),
    )
    if run is None:
        return {"id": execution_id, "status": "not_found"}
    pid = run.get("pipeline_id") or ""
    msgs = await kernel_reads.list_messages(pid, limit=er._SESSION_MSG_FETCH_LIMIT)
    detail = _run_to_call(run)
    detail.update({
        "id": execution_id,
        "message_count": len(msgs),
        "messages": [er._message_to_record(m, pid) for m in msgs[:_DETAIL_MESSAGE_LIMIT]],
    })
    return detail
