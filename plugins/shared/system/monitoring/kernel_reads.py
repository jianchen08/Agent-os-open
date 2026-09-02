"""内核只读能力桥：execution/sessions/agent-calls/search 域 handler 的真实数据入口。

数据源 = 内核能力（messages.list / pipeline-runs.list / db-admin.table_query），
不消费 stub 数据。provider 闭包由 monitoring server.py 的 _on_load 注入
（get_capability 写法）；未注入（单测环境/内核握手未完成）时各读函数
warn 一次并返回空结构，handler 侧保持 HTTP 200 空载荷的前端契约不破坏。

信封解包（_unwrap）：capability 返回形态随提供方而异——db-admin 返回
``{status, body}`` 信封、tool-executor 归一 ``{success, data}``、
service-registry 域（messages/pipeline-runs）直接返回数组。统一在此收敛，
调用方只拿业务数据。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# provider 注册表：name → 异步闭包。server._on_load 注入。
_PROVIDERS: dict[str, Callable[..., Any]] = {}
# 能力不可用的告警去重（每种只 warn 一次，避免轮询刷屏）
_warned: set[str] = set()


def set_provider(name: str, fn: Callable[..., Any]) -> None:
    """注册一个内核读 provider（幂等，重复注入以后者为准）。"""
    _PROVIDERS[name] = fn
    _warned.discard(name)


def reset_providers() -> None:
    """清空全部 provider（单测隔离用）。"""
    _PROVIDERS.clear()
    _warned.clear()


def _unwrap(raw: Any) -> Any:
    """收敛 capability 返回的三种信封形态为业务数据。

    - ``{status, body}``（db-admin）→ body；
    - ``{success, data}``（归一信封）→ success 时 data，否则空列表；
    - 其余（含裸数组/对象）→ 原样返回。
    """
    if isinstance(raw, dict):
        if "body" in raw and ("status" in raw or "error" in raw):
            return raw.get("body")
        if "data" in raw and "success" in raw:
            return raw.get("data") if raw.get("success") else []
    return raw


def _rows(raw: Any) -> list[dict[str, Any]]:
    """把 _unwrap 结果收形为 list[dict]（非 dict 元素剔除，非 list 归空）。"""
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


async def _call(name: str, *args: Any, **kwargs: Any) -> Any:
    """调用已注入 provider；未注入/调用失败时 warn 一次并返回空。"""
    fn = _PROVIDERS.get(name)
    if fn is None:
        if name not in _warned:
            _warned.add(name)
            logger.warning("[kernel_reads] provider %s 未注入（内核能力不可用），降级空数据", name)
        return []
    try:
        return await fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 —— 读面降级：能力失败不崩 handler
        if name not in _warned:
            _warned.add(name)
            logger.warning("[kernel_reads] provider %s 调用失败（降级空数据）: %s", name, exc)
        return []


async def list_pipeline_runs(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """管道运行快照列表（pipeline-runs.list，按 started_at 倒序）。

    行字段：run_id/pipeline_id/thread_id/status/started_at/ended_at/
    total_tokens/total_seconds（PipelineRunInfo 序列化形态）。
    """
    return _rows(await _call("pipeline-runs", status=status, limit=limit))


async def list_messages(pipeline_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    """按 pipeline_id 查消息记录（messages.list，seq 升序）。

    行字段（MessageRecord）：message_id/run_id/seq_in_branch/role/content_preview
    （读时重建的全文）/tool_calls_json/tool_call_id/reasoning_content/status/
    error/created_at/pipeline_id。
    """
    return _rows(await _call("messages", pipeline_id=pipeline_id, limit=limit))


async def list_state_rows() -> list[dict[str, Any]]:
    """管道 state 摘要行（pipeline-state.list，内存热数据 + DB 冷兜底）。

    行字段：白名单摘要（display_name/message_count/task.*/current_phase…）+
    pipeline_id/thread_id/agent_id/source。
    """
    return _rows(await _call("pipeline-state"))


class ClearExecutionDataError(Exception):
    """全量执行数据清理失败（写面专用，绝不降级假成功）。

    携带应透传给前端的 HTTP 状态码：内核信封原状态（403/409/500）、
    能力缺失 503、能力通道异常 502。与读面"降级空数据"策略刻意相反——
    清理是破坏性操作，静默失败比报错危险。
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def clear_execution_data(authorization: str = "") -> dict[str, Any]:
    """db-admin.clear_execution_data：全量执行数据清理（9 表 + 内存 registry）。

    Returns:
        内核 body：``{cleared: {表: 行数}, cleared_count, backup_path}``。

    Raises:
        ClearExecutionDataError: 能力未注入（503）/通道异常（502）/内核
            信封非 200（透传原状态码与 message）。
    """
    fn = _PROVIDERS.get("db-admin-clear")
    if fn is None:
        if "db-admin-clear" not in _warned:
            _warned.add("db-admin-clear")
            logger.warning("[kernel_reads] provider db-admin-clear 未注入（清理能力不可用）")
        raise ClearExecutionDataError(503, "执行数据清理能力不可用（db-admin 能力未注入）")
    params: dict[str, Any] = {}
    if authorization:
        params["_authorization"] = authorization
    try:
        envelope = await fn(authorization=authorization)
    except Exception as exc:  # noqa: BLE001 —— 写面：通道异常上抛（不吞成假成功）
        raise ClearExecutionDataError(502, f"清理能力调用失败: {exc}") from exc
    if isinstance(envelope, dict) and envelope.get("status") == 200:
        body = envelope.get("body")
        return body if isinstance(body, dict) else {}
    if isinstance(envelope, dict) and "error" in envelope:
        err = envelope["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        try:
            status = int(envelope.get("status") or 500)
        except (TypeError, ValueError):
            status = 500
        raise ClearExecutionDataError(status, str(message or "清理失败"))
    raise ClearExecutionDataError(502, f"清理能力返回异常信封: {envelope!r}")


# process.* gauge → 行字段（metrics-admin.list 读面过滤集；监控设计 §三 通道3 C 类）
_PROCESS_FIELDS: dict[str, str] = {
    "process.alive": "alive",
    "process.pid": "pid",
    "process.memory_rss_bytes": "memory_rss_bytes",
    "process.uptime_seconds": "uptime_seconds",
    "process.last_crash_ts": "last_crash_ts",
}


async def plugin_runtime(authorization: str = "") -> dict[str, Any]:
    """插件运行态表 + lifecycle 计数（metrics-admin list/query 只读桥）。

    - rows：list 的 process.* gauge 按 plugin_id 组行（alive/pid/memory_rss_bytes/
      uptime_seconds/last_crash_ts + 派生 status/memory_rss_mb）。仅被内核周期轮询
      采到进程态的插件会出现（lazy 未 spawn 的插件无 series，不占行）。
    - lifecycle：query(plugin=kernel) 的 lifecycle.plugin_load_total /
      plugin_error_total 计数器样本求和——聚合器留存窗口内的累计（滚动约 2 小时，
      非进程全生命周期总量，Prometheus counter 模型的诚实口径）。

    Returns:
        {columns, rows, total, lifecycle}（columns 声明中文表头，前端表格零改动渲染）。

    能力未注入/调用失败降级空结构（读面契约同 list_pipeline_runs 等）。
    """
    rows_map: dict[str, dict[str, Any]] = {}
    list_body = _unwrap(await _call("metrics-admin-list", authorization=authorization))
    series = list_body.get("series") if isinstance(list_body, dict) else list_body
    for s in _rows(series):
        field = _PROCESS_FIELDS.get(str(s.get("name", "")))
        plugin_id = s.get("plugin_id")
        if field is None or not isinstance(plugin_id, str):
            continue
        row = rows_map.setdefault(plugin_id, {"plugin_id": plugin_id})
        row[field] = s.get("latest")

    rows: list[dict[str, Any]] = []
    for plugin_id in sorted(rows_map):
        row = rows_map[plugin_id]
        alive = row.get("alive")
        row["alive"] = int(alive) if isinstance(alive, (int, float)) else 0
        row["status"] = "running" if row["alive"] == 1 else "dead"
        rss = row.get("memory_rss_bytes")
        row["memory_rss_mb"] = (
            round(float(rss) / (1024 * 1024), 1) if isinstance(rss, (int, float)) else None
        )
        # gauge series 缺失（未崩过或留存窗外）按 0 = 未知/未崩
        row["last_crash_ts"] = row.get("last_crash_ts") or 0
        rows.append(row)

    load_total = 0.0
    error_total = 0.0
    query_body = _unwrap(
        await _call("metrics-admin-query", authorization=authorization, plugin="kernel")
    )
    metrics = query_body.get("metrics") if isinstance(query_body, dict) else query_body
    for s in _rows(metrics):
        name = str(s.get("name", ""))
        if name not in ("lifecycle.plugin_load_total", "lifecycle.plugin_error_total"):
            continue
        total = sum(
            float(sample.get("value") or 0)
            for sample in (s.get("samples") or [])
            if isinstance(sample, dict)
        )
        if name == "lifecycle.plugin_load_total":
            load_total += total
        else:
            error_total += total

    return {
        "columns": [
            {"key": "plugin_id", "label": "插件"},
            {"key": "status", "label": "状态"},
            {"key": "pid", "label": "PID"},
            {"key": "memory_rss_mb", "label": "内存 (MB)"},
            {"key": "uptime_seconds", "label": "运行时长 (秒)"},
            {"key": "last_crash_ts", "label": "上次崩溃时间戳"},
        ],
        "rows": rows,
        "total": len(rows),
        "lifecycle": {
            "plugin_load_total": int(load_total),
            "plugin_error_total": int(error_total),
        },
    }
