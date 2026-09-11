"""评测指标采集与聚合。

口径（与 docs/working/评测任务方案_20260908.md 对齐）：
- 轮次：state 字段 ``iteration``（每管道，LLM 轮数）；
- 重试：traces 中同 (工具名, 参数) 重复调用的多余次数（tool_retries）
  + 失败步数（tool_results success=false 与 patch raw_error，error_steps）；
- 超时步：错误文本含 CAPABILITY_TIMEOUT / timeout（工具超时 case 的判定证据）;
- token：Σ patch.llm_usage（按 model 分解，cached 单列）；
- 终态：管道 run_status ∈ {completed, failed, cancelled}，suspended/running 视为未收敛。
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

TERMINAL_RUN_STATUS = {"completed", "failed", "cancelled"}


def parse_state_value(raw: Any) -> Any:
    """pipeline_state.field_value 为 DB 原始字符串，JSON 形态解析为对象。"""
    if isinstance(raw, str) and raw[:1] in ("{", "["):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def state_fields_to_dict(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """fields 行数组 → {field_key: 解析后的值}。"""
    return {
        str(f.get("field_key") or ""): parse_state_value(f.get("field_value"))
        for f in fields
    }


def _tool_call_name(call: dict[str, Any]) -> str:
    name = call.get("name")
    if isinstance(name, str) and name:
        return name
    fn = call.get("function")
    return fn.get("name", "?") if isinstance(fn, dict) else "?"


def _error_text(trace: dict[str, Any]) -> str:
    patch = trace.get("patch_data")
    patch = patch if isinstance(patch, dict) else {}
    parts = [patch.get("raw_error") or "", patch.get("error") or ""]
    for result in patch.get("tool_results") or []:
        if isinstance(result, dict) and result.get("success") is False:
            metadata = result.get("metadata")
            msg = metadata.get("message") if isinstance(metadata, dict) else None
            parts.append(str(msg or result.get("data") or ""))
    return " ".join(str(p) for p in parts if p)


def aggregate_traces(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """单管道 trace 聚合：失败步/超时步/工具重试/按模型 token/eval_echo 证据。"""
    error_steps = 0
    timeout_steps = 0
    llm_rounds = 0
    signature_counts: Counter[tuple[str, str]] = Counter()
    by_model: dict[str, dict[str, int]] = {}
    echo_fail_seen = False
    echo_ok_seen = False

    for trace in traces:
        patch = trace.get("patch_data")
        patch = patch if isinstance(patch, dict) else {}

        # 轮次口径：每 LLM 轮产生一个 core 插件 patch（state.iteration 无持久化
        # 出口——活跃管道不落 pipeline_state 表且键未被 export_fields 声明）
        if trace.get("plugin_id") == "core":
            llm_rounds += 1

        error_text = _error_text(trace)
        if error_text.strip():
            error_steps += 1
            lowered = error_text.lower()
            if "capability_timeout" in lowered or "timeout" in lowered:
                timeout_steps += 1

        usage = patch.get("llm_usage")
        if isinstance(usage, dict) and usage.get("total_tokens"):
            model = str(usage.get("model") or "unknown")
            bucket = by_model.setdefault(
                model, {"input": 0, "output": 0, "cached": 0, "total": 0}
            )
            bucket["input"] += int(usage.get("input_tokens") or 0)
            bucket["output"] += int(usage.get("output_tokens") or 0)
            bucket["cached"] += int(usage.get("cached_tokens") or 0)
            bucket["total"] += int(usage.get("total_tokens") or 0)

        for call in patch.get("_executed_tool_calls") or []:
            if not isinstance(call, dict):
                continue
            args = call.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(call.get("args"), ensure_ascii=False, sort_keys=True, default=str)
            signature_counts[(_tool_call_name(call), args or "")] += 1
        for result in patch.get("tool_results") or []:
            if not isinstance(result, dict):
                continue
            if result.get("tool_name") != "eval_echo":
                continue
            data = result.get("data")
            valid = data.get("valid") if isinstance(data, dict) else None
            if valid is False:
                echo_fail_seen = True
            elif valid is True:
                echo_ok_seen = True

    return {
        "error_steps": error_steps,
        "timeout_steps": timeout_steps,
        "llm_rounds": llm_rounds,
        "tool_calls": sum(signature_counts.values()),
        "tool_retries": sum(c - 1 for c in signature_counts.values() if c > 1),
        "tool_names": sorted({name for (name, _args) in signature_counts}),
        "tool_call_counts": {
            name: sum(c for (n, _args), c in signature_counts.items() if n == name)
            for name in {name for (name, _args) in signature_counts}
        },
        "by_model": by_model,
        "echo_fail_seen": echo_fail_seen,
        "echo_ok_seen": echo_ok_seen,
        "echo_call_count": sum(c for (name, _), c in signature_counts.items() if name == "eval_echo"),
    }


def collect_pipeline_metrics(client: Any, pipeline_id: str) -> dict[str, Any]:
    """单管道指标：state 全字段 + trace 聚合。"""
    full = client.get_pipeline_state_full(pipeline_id)
    fields = state_fields_to_dict(full.get("fields") or [])
    summary = full.get("summary") or {}
    traces = (client.get_traces(pipeline_id) or {}).get("traces") or []
    agg = aggregate_traces(traces)
    runs = full.get("runs") or []
    run_statuses = [str(r.get("status") or "") for r in runs if isinstance(r, dict)]
    return {
        "pipeline_id": pipeline_id,
        "thread_id": summary.get("thread_id"),
        # 轮次：state.iteration 若有则优先（终态落库管道），否则按 core patch 数
        "iterations": int(fields.get("iteration") or 0) or agg["llm_rounds"],
        "task_status": summary.get("task.status") or fields.get("task.status"),
        "run_statuses": [str(r.get("status") or "") for r in runs if isinstance(r, dict)],
        "settled": all(s in TERMINAL_RUN_STATUS for s in run_statuses) and bool(run_statuses),
        **{k: agg[k] for k in ("error_steps", "timeout_steps", "llm_rounds", "tool_calls",
                               "tool_retries", "by_model", "echo_fail_seen",
                               "echo_ok_seen", "echo_call_count")},
    }


def merge_pipeline_metrics(per_pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    """case 全链（主管道 + 派生子任务管道）指标汇总。"""
    by_model: dict[str, dict[str, int]] = {}
    tool_names: set[str] = set()
    tool_call_counts: dict[str, int] = {}
    for p in per_pipeline:
        tool_names.update(p.get("tool_names") or [])
        for name, n in (p.get("tool_call_counts") or {}).items():
            tool_call_counts[name] = tool_call_counts.get(name, 0) + int(n or 0)
        for model, bucket in (p.get("by_model") or {}).items():
            target = by_model.setdefault(model, {"input": 0, "output": 0, "cached": 0, "total": 0})
            for key in target:
                target[key] += bucket.get(key, 0)
    return {
        "pipelines": len(per_pipeline),
        "iterations_total": sum(p.get("iterations", 0) for p in per_pipeline),
        "error_steps": sum(p.get("error_steps", 0) for p in per_pipeline),
        "timeout_steps": sum(p.get("timeout_steps", 0) for p in per_pipeline),
        "tool_calls": sum(p.get("tool_calls", 0) for p in per_pipeline),
        "tool_retries": sum(p.get("tool_retries", 0) for p in per_pipeline),
        "tool_names": sorted(tool_names),
        "tool_call_counts": tool_call_counts,
        "task_statuses": [p.get("task_status") for p in per_pipeline],
        "all_settled": all(p.get("settled") for p in per_pipeline) if per_pipeline else False,
        "any_failed": any(p.get("task_status") == "failed" for p in per_pipeline),
        "by_model": by_model,
        "tokens_total": sum(b.get("total", 0) for b in by_model.values()),
    }
