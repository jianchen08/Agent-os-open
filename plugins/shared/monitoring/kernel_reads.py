"""内核只读能力桥：execution/sessions/search 域 handler 的真实数据入口。

数据源 = 内核能力（messages.list / pipeline-runs.list / db-admin.table_query），
不消费 stub 数据。provider 闭包由 monitoring server.py 的 _on_load 注入
（get_capability 写法）；未注入（单测环境/内核握手未完成）时各读函数
warn 一次并返回空结构，handler 侧保持 HTTP 200 空载荷的前端契约不破坏。

信封解包（_unwrap）：capability 返回形态随提供方而异——db-admin 返回
``{status, body}`` 信封、tool-executor 归一 ``{success, data}``、
service-registry 域（messages/pipeline-runs）直接返回数组。统一在此收敛，
调用方只拿业务数据。

db-read（内核库读契约服务，C2 刀2）：provider 闭包委托共享桥
``agentos_plugin_sdk.db_read.db_read_query``（T6d 自共享根上收 SDK），本模块
保留 provider 装配缝（与其他读面同款 set_provider 注入），失败以
:class:`DbReadQueryError` 上抛——降级（返 None/空集）归调用面自持，不走
其他读面的"降级空数据"（搜索/统计面需要区分"查空"与"查失败"）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from agentos_plugin_sdk.db_read import DbReadQueryError, unwrap_db_read_envelope

__all__ = [
    "ClearExecutionDataError",
    "DbReadQueryError",
    "db_read_query",
    "kernel_plugin_counters",
    "list_messages",
    "list_pipeline_runs",
    "list_runs_by_pipeline",
    "list_state_rows",
    "list_traces",
    "plugin_crash_samples",
    "plugin_runtime",
    "query_table",
    "reset_providers",
    "set_provider",
]

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


async def list_traces(pipeline_id: str) -> list[dict[str, Any]]:
    """按 pipeline_id 直查单管道 step 级轨迹（traces.list_by_pipeline，seq 升序）。

    行字段（TraceEntry）：trace_id/pipeline_id/seq/plugin_id/patch_type/
    patch_data（状态窗口 JSON）/created_at。绑真会话的任务管道旧读面
    （按 thread_id）查不到，此读面绕开会话映射直查。
    """
    return _rows(await _call("traces", pipeline_id=pipeline_id))


async def list_runs_by_pipeline(pipeline_id: str) -> list[dict[str, Any]]:
    """按 pipeline_id 列该管道全部 run（pipeline-runs.list_by_pipeline）。

    行字段（PipelineRunInfo）：run_id/pipeline_id/thread_id/status/started_at/
    ended_at。
    """
    return _rows(await _call("runs-by-pipeline", pipeline_id=pipeline_id))


async def query_table(
    table: str,
    filter: list[str] | None = None,
    sort: str = "",
    limit: int = 500,
) -> dict[str, Any]:
    """db-admin.table_query 只读桥（filter 语法 col:op:value，op ∈ eq/ne/gt/lt/contains）。

    Returns:
        内核 body：``{table, total, limit, offset, rows}``；能力不可用降级空 dict。
    """
    params: dict[str, Any] = {"table": table, "limit": int(limit)}
    if filter:
        params["filter"] = [str(f) for f in filter]
    if sort:
        params["sort"] = sort
    body = _unwrap(await _call("db-admin-query", **params))
    return body if isinstance(body, dict) else {}


async def db_read_query(
    face: str,
    sql: str,
    params: dict[str, Any] | None = None,
    authorization: str = "",
) -> list[list[Any]]:
    """内核库读契约查询（db-read.query，C2 刀2）——失败上抛不降级。

    provider 由 server._on_load 注入（原始 capability 调用，信封原样回传，
    解包单点在共享 helper）；未注入（单测环境/内核握手未完成）即抛
    :class:`DbReadQueryError`，由调用面按各自降级契约处理（search 返空集、
    统计返全 0、审批返 None）。身份经 ``authorization``（入站 Authorization
    原头值）由内核复权，租户收窄在契约层内建（spec §4）。

    Returns:
        行列表（行 = 列值数组，位置对齐 SELECT 投影序）。
    """
    fn = _PROVIDERS.get("db-read")
    if fn is None:
        raise DbReadQueryError("db-read provider 未注入（内核能力不可用）")
    try:
        envelope = await fn(face=face, sql=sql, params=params, authorization=authorization)
    except DbReadQueryError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 通道异常归一为契约错误，降级归调用面
        raise DbReadQueryError(f"db-read provider 调用失败: {exc}") from exc
    return unwrap_db_read_envelope(envelope)


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
    "process.memory_commit_bytes": "memory_commit_bytes",
    "process.uptime_seconds": "uptime_seconds",
    "process.last_crash_ts": "last_crash_ts",
}


async def plugin_runtime(authorization: str = "") -> dict[str, Any]:
    """插件运行态表 + lifecycle 计数（metrics-admin list/query 只读桥）。

    - rows：list 的 process.* gauge 按 plugin_id 组行（alive/pid/memory_commit_bytes/
      uptime_seconds/last_crash_ts + 派生 status/memory_commit_mb）。仅被内核周期轮询
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
        commit = row.get("memory_commit_bytes")
        row["memory_commit_mb"] = (
            round(float(commit) / (1024 * 1024), 1) if isinstance(commit, (int, float)) else None
        )
        # gauge series 缺失（未崩过或留存窗外）按 0 = 未知/未崩
        row["last_crash_ts"] = row.get("last_crash_ts") or 0
        rows.append(row)

    load_total = 0.0
    error_total = 0.0
    query_body = _unwrap(
        await _call("metrics-admin-query", authorization=authorization, plugin_id="kernel")
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
            {"key": "memory_commit_mb", "label": "提交内存 (MB)"},
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


async def plugin_crash_samples(window: str = "5m", authorization: str = "") -> dict[str, list[dict[str, Any]]]:
    """各插件 ``process.last_crash_ts`` gauge 样本（metrics-admin.query 读面桥）。

    插件组健康告警的数据源（卡5）：last_crash_ts 唯一写方 = invoker 恢复失败/
    判死回调（notify_crash），窗口内样本严格递增跳变数 = respawn 恢复失败次数。
    ``metric`` 过滤在内核侧收窄（只回该名 series，免拉全量），身份经
    ``authorization`` 原头转发内核复权（读面强制 admin/viewer，无凭证 401）。

    Returns:
        {plugin_id: [{ts, value}, ...]}；能力未注入/调用失败降级空 dict
        （读面契约同 list_pipeline_runs 等）。
    """
    params: dict[str, Any] = {
        "window": window,
        "metric": "process.last_crash_ts",
        # provider 闭包形参 authorization（闭包内自行换算 capability 层
        # _authorization）——直接注 _authorization 会 TypeError 静默降级
        # 空数据（混沌演练 2026-10-18 实证：带凭证 HTTP 面全盲）
        "authorization": authorization,
    }
    body = _unwrap(await _call("metrics-admin-query", **params))
    if not isinstance(body, dict):
        return {}
    samples_by_plugin: dict[str, list[dict[str, Any]]] = {}
    for series in _rows(body.get("metrics") if isinstance(body.get("metrics"), list) else body.get("series")):
        if str(series.get("name", "")) != "process.last_crash_ts":
            continue
        plugin_id = series.get("plugin_id")
        if not isinstance(plugin_id, str) or not plugin_id:
            continue
        samples = [s for s in (series.get("samples") or []) if isinstance(s, dict)]
        if samples:
            samples_by_plugin[plugin_id] = samples
    return samples_by_plugin


# 卡9 内核自采 counter 名（plugin="kernel" 命名空间；插件组健康规则的
# EOF 速率 / tools-list 非法两条规则的数据源）
_KERNEL_COUNTER_NAMES = (
    "mcp.sidecar_stdout_eof_total",
    "mcp.tools_list_observe_fail_total",
    "mcp.tools_list_malformed_total",
)


async def kernel_plugin_counters(window: str = "5m", authorization: str = "") -> dict[str, list[dict[str, Any]]]:
    """内核自采 counter 样本（metrics-admin.query 读面桥，卡9 规则数据源）。

    一次 query(plugin="kernel") 取内核命名空间全量（内核指标面小，免三次往返），
    侧滤卡9 三条计数器名。``authorization`` 缺省 = 插件后台轮询形态：params 不带
    ``_authorization``，经内核 metrics-admin 的插件语境系统读放行（invoker 注入
    ``_plugin_id`` 信任锚点，ADR 2026-10-18-metrics-admin-plugin-context-read）；
    传入凭证时内核侧照常 admin/viewer 复权（HTTP 面形态不变）。

    Returns:
        {metric 名: [{ts, value}, ...]}（只含有样本的计数器）；能力未注入/
        调用失败降级空 dict（读面契约同 plugin_crash_samples）。
    """
    params: dict[str, Any] = {
        "window": window,
        "plugin_id": "kernel",
        # 同上：authorization 走 provider 闭包形参，勿手工注 _authorization
        "authorization": authorization,
    }
    body = _unwrap(await _call("metrics-admin-query", **params))
    if not isinstance(body, dict):
        return {}
    counters: dict[str, list[dict[str, Any]]] = {}
    metrics = body.get("metrics") if isinstance(body.get("metrics"), list) else body.get("series")
    for series in _rows(metrics):
        name = str(series.get("name", ""))
        if name not in _KERNEL_COUNTER_NAMES:
            continue
        samples = [s for s in (series.get("samples") or []) if isinstance(s, dict)]
        if samples:
            counters[name] = samples
    return counters
