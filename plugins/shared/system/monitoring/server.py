#!/usr/bin/env python3
"""Monitoring Service MCP 服务端——纯接口适配层。

老代码从 0.1 src/monitoring/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

核心能力：
- monitoring.get_metrics: 获取当前系统性能指标
- monitoring.get_health: 获取系统健康状态
- monitoring.record_llm_request: 记录 LLM API 调用
- monitoring.record_tool_execution: 记录工具执行
- monitoring.update_task_status: 更新任务状态指标

监控 M7（监控设计 §三 D 类 + §十一）：系统资源指标（CPU/内存/磁盘/网络）改走
record_metric 上报到内核聚合器，不再只自己存。本插件经 ctx.record_metric 把
psutil 采到的 D 类系统资源推给内核，进入统一聚合器（供 /api/v1/metrics 与
/metrics 查询）。

[来源: docs/working/module_migration_plan.md §六 P2 迁移]
[来源: docs/working/重要设计/插件监控与指标机制设计.md §三 D 类 + M7]
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import os
import sys
from typing import Any

import psutil

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("monitoring")

# 全局 PerformanceMonitor 实例
_monitor: Any = None
# 监控 M7：后台指标上报任务（把 D 类系统资源经 record_metric 推给内核聚合器）。
_reporter_task: asyncio.Task | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize performance monitor on load."""
    global _monitor, _reporter_task
    from performance_monitor import PerformanceMonitor

    _monitor = PerformanceMonitor()
    await _monitor.start()
    # M7：启动后台 record_metric 上报循环（系统资源 → 内核聚合器）
    _reporter_task = asyncio.create_task(_report_metrics_loop())
    logger.info("Monitoring service started (record_metric reporter enabled)")

    # 调试中心数据链（channel_api 退役批次 2/3 随迁）：execution/sessions/
    # agent-calls/search 域真实数据 = 内核只读能力（messages.list /
    # pipeline-runs.list / pipeline-state.list / db-admin.table_query），经
    # kernel_reads 桥接注入（逻辑随迁自 channel_api server._on_load，零改动）；
    # 能力未就绪时 handler 降级空载荷（前端契约不破坏）。
    try:
        import kernel_reads  # noqa: PLC0415

        async def _kr_list_pipeline_runs(status: str | None = None, limit: int = 100):
            # service-registry 约定：handle.call("<域>.<op>")——pipeline-runs 域挂在
            # capability_router 的 service-registry 分发下（直连式需登记两端
            # STANDARD_CAPABILITIES，未走该通道）。
            handle = plugin.get_capability("service-registry")
            return await handle.call("pipeline-runs.list", {"status": status or "", "limit": int(limit)})

        async def _kr_list_messages(pipeline_id: str, limit: int | None = None):
            params: dict[str, Any] = {"pipeline_id": pipeline_id}
            if limit is not None:
                params["limit"] = int(limit)
            handle = plugin.get_capability("service-registry")
            return await handle.call("messages.list", params)

        async def _kr_list_state_rows():
            handle = plugin.get_capability("pipeline-state")
            rows = await handle.call("list", {})
            return rows if isinstance(rows, list) else []

        async def _kr_query_table(
            table: str, limit: int = 50, offset: int = 0, authorization: str = ""
        ):
            params: dict[str, Any] = {"table": table, "limit": int(limit), "offset": int(offset)}
            if authorization:
                params["_authorization"] = authorization
            handle = plugin.get_capability("db-admin")
            return await handle.call("table_query", params)

        kernel_reads.set_provider("pipeline-runs", _kr_list_pipeline_runs)
        kernel_reads.set_provider("messages", _kr_list_messages)
        kernel_reads.set_provider("pipeline-state", _kr_list_state_rows)
        kernel_reads.set_provider("db-admin", _kr_query_table)
    except Exception as exc:  # noqa: BLE001 — 注入失败降级（handler 返回空结构）
        logger.warning("[monitoring] kernel_reads provider 注入失败: %s", exc)


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Stop monitor and cleanup on unload."""
    global _monitor, _reporter_task
    if _reporter_task is not None and not _reporter_task.done():
        _reporter_task.cancel()
        try:
            await _reporter_task
        except asyncio.CancelledError:
            pass
    _reporter_task = None
    if _monitor is not None:
        await _monitor.stop()
        _monitor = None
    logger.info("Monitoring service stopped")


def _ensure_monitor() -> Any:
    """获取 monitor 实例，如果未初始化则延迟创建。"""
    global _monitor
    if _monitor is None:
        from performance_monitor import PerformanceMonitor

        _monitor = PerformanceMonitor()
    return _monitor


async def _report_metrics_loop() -> None:
    """监控 M7：周期性把系统资源指标经 record_metric 上报内核聚合器。

    监控设计 §三 D 类 + §十一：monitoring 插件采完 D 类（CPU/内存/磁盘/网络），
    走通道2 record_metric 上报，进入同一聚合器。不再自己存全量历史。
    上报频率 5s/次（D 类系统资源不需 1s 粒度）。
    """
    # 上报间隔（5 秒，D 类系统资源粒度）
    interval = 5.0
    while True:
        try:
            await _report_system_metrics_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.debug("record_metric report failed: %s", e)
        await asyncio.sleep(interval)


async def _report_system_metrics_once() -> None:
    """采一次系统资源指标并通过 record_metric 上报。

    失败静默（record_metric 未注入 / 内核未启用聚合器时不阻断插件）。
    """
    monitor = _monitor
    if monitor is None:
        return
    try:
        system = await monitor.get_system_metrics()
    except Exception:  # noqa: BLE001
        return
    # psutil 采到的 D 类系统资源 → record_metric（gauge，覆盖当前值）
    # 指标名用监控设计 §九 后缀规范（_ratio=百分比、_bytes=字节、_kbytes=速率）
    metrics = [
        ("system.cpu_usage_ratio", system.cpu_usage / 100.0, {"unit": "ratio"}),
        ("system.memory_usage_ratio", system.memory_usage / 100.0, {"unit": "ratio"}),
        ("system.disk_usage_ratio", system.disk_usage / 100.0, {"unit": "ratio"}),
        ("system.network_sent_kbytes_per_sec", system.network_sent, {"unit": "kbytes/s"}),
        ("system.network_recv_kbytes_per_sec", system.network_recv, {"unit": "kbytes/s"}),
    ]
    for name, value, extra in metrics:
        labels = {"source": "psutil"}
        unit = extra.get("unit")
        try:
            await plugin.record_metric(name, value, "gauge", labels, unit=unit)
        except KeyError:
            # metrics capability 未注入（内核未启用聚合器）→ 静默跳过，不阻断
            return
        except Exception as e:  # noqa: BLE001
            logger.debug("record_metric %s failed: %s", name, e)


@plugin.tool(
    name="monitoring.get_metrics",
    schema={
        "type": "object",
        "properties": {},
    },
    description="Get current system performance metrics (CPU, memory, disk, network, LLM, tools, tasks)",
)
async def monitoring_get_metrics() -> dict[str, Any]:
    """Get current performance metrics snapshot.

    Returns:
        Dict with system, database, llm, tool, task metrics sections.
    """
    monitor = _ensure_monitor()
    return await monitor.get_current_metrics()


@plugin.tool(
    name="monitoring.get_health",
    schema={
        "type": "object",
        "properties": {},
    },
    description="Get system health status with bottleneck detection",
)
async def monitoring_get_health() -> dict[str, Any]:
    """Get system health status.

    Checks CPU/memory thresholds and returns status with issues list.

    Returns:
        Dict with status (healthy/warning/critical), issues list, and metrics.
    """
    monitor = _ensure_monitor()
    return monitor.get_health_status()


@plugin.tool(
    name="monitoring.record_llm_request_start",
    schema={"type": "object", "properties": {}, "required": []},
    description="Record that an LLM API call has started (increments active_requests; pair with record_llm_request on completion)",
)
async def monitoring_record_llm_request_start() -> dict[str, Any]:
    """Record an LLM request start (pairs with record_llm_request on completion)."""
    monitor = _ensure_monitor()
    monitor.record_llm_request_start()
    return {"recorded": True}


@plugin.tool(
    name="monitoring.record_llm_request",
    schema={
        "type": "object",
        "properties": {
            "response_time": {"type": "number", "description": "Response time in seconds"},
            "error": {"type": "boolean", "default": False, "description": "Whether the request errored"},
        },
        "required": ["response_time"],
    },
    description="Record an LLM API call for usage and latency tracking",
)
async def monitoring_record_llm_request(
    response_time: float,
    error: bool = False,
) -> dict[str, Any]:
    """Record an LLM request.

    Args:
        response_time: Response time in seconds.
        error: Whether the request resulted in an error.

    Returns:
        Dict with 'recorded': True.
    """
    monitor = _ensure_monitor()
    monitor.record_llm_request(response_time, error)
    return {"recorded": True}


@plugin.tool(
    name="monitoring.record_tool_execution",
    schema={
        "type": "object",
        "properties": {
            "execution_time": {"type": "number", "description": "Execution time in seconds"},
            "cache_hit": {"type": "boolean", "default": False},
            "error": {"type": "boolean", "default": False},
        },
        "required": ["execution_time"],
    },
    description="Record a tool execution for performance tracking",
)
async def monitoring_record_tool_execution(
    execution_time: float,
    cache_hit: bool = False,
    error: bool = False,
) -> dict[str, Any]:
    """Record a tool execution.

    Args:
        execution_time: Execution time in seconds.
        cache_hit: Whether the result was served from cache.
        error: Whether the execution resulted in an error.

    Returns:
        Dict with 'recorded': True.
    """
    monitor = _ensure_monitor()
    monitor.record_tool_execution(execution_time, cache_hit, error)
    return {"recorded": True}


@plugin.tool(
    name="monitoring.update_task_status",
    schema={
        "type": "object",
        "properties": {
            "pending": {"type": "integer", "description": "Number of pending tasks"},
            "running": {"type": "integer", "description": "Number of running tasks"},
            "completed": {"type": "integer", "description": "Number of completed tasks"},
            "task_time": {"type": "number", "default": 0, "description": "Task execution time in seconds"},
        },
        "required": ["pending", "running", "completed"],
    },
    description="Update task execution metrics (pending, running, completed counts)",
)
async def monitoring_update_task_status(
    pending: int,
    running: int,
    completed: int,
    task_time: float = 0,
) -> dict[str, Any]:
    """Update task execution metrics.

    Args:
        pending: Number of pending tasks.
        running: Number of running tasks.
        completed: Number of completed tasks.
        task_time: Task execution time in seconds.

    Returns:
        Dict with 'updated': True.
    """
    monitor = _ensure_monitor()
    monitor.update_task_status(pending, running, completed, task_time)
    return {"updated": True}


# ── HTTP 端点（http.handle）—— 前端 /ext/monitoring/** 入口 ─────────────────
# 参照 cost_control 已验证模式：dispatcher 把 HttpHandleRequest 整体作 arguments 传入，
# 本工具按 path 分发到 5 个子端点，返回 ToolExecutionResult{success,data}，body base64。
# 系统指标直接用 psutil 采集完整数据（含 memory.total/used/available、disk.total/used/free），
# 对齐前端 SystemMetrics 嵌套 TS 类型（monitoring.ts:10-40）。
# task/token/cache 统计读 PerformanceMonitor 本地累计（注：业务数据散在别处插件，
# 本地累计可能为 0，属已知限制，后续应接内核 metrics 聚合查询）。


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """包成内核期望的 HttpHandleResponse（body base64）。"""
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    body_b64 = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": body_b64,
        "body_encoding": "base64",
    }


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _error(message: str, status: int = 503) -> dict[str, Any]:
    return {"success": False, "error": message, "data": _json_response({"error": message}, status)}


def _collect_system_metrics() -> dict[str, Any]:
    """用 psutil 采集完整系统指标，对齐前端 SystemMetrics 嵌套结构。

    前端期望：{cpu_usage, memory:{total,used,available,usage_percent}, disk:{mount_point,total,used,free,usage_percent}, uptime, timestamp}
    """
    cpu = psutil.cpu_percent(interval=0.1)
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("/")
    boot_ts = psutil.boot_time()
    now = datetime.datetime.now(datetime.timezone.utc)
    uptime = max(0, int(now.timestamp() - boot_ts))
    return {
        "cpu_usage": cpu,
        "memory": {
            "total": vm.total,
            "used": vm.used,
            "available": vm.available,
            "usage_percent": vm.percent,
        },
        "disk": {
            "mount_point": "/",
            "total": du.total,
            "used": du.used,
            "free": du.free,
            "usage_percent": du.percent,
        },
        "uptime": uptime,
        "timestamp": now.isoformat(),
    }


def _collect_task_statistics() -> dict[str, Any]:
    """读 PerformanceMonitor._task_stats，对齐前端 TaskStatistics。"""
    monitor = _ensure_monitor()
    ts = monitor._task_stats
    total = ts.get("completed_tasks", 0) + ts.get("running_tasks", 0) + ts.get("pending_tasks", 0)
    succeeded = ts.get("completed_tasks", 0)
    return {
        "total": total,
        "succeeded": succeeded,
        "failed": 0,
        "running": ts.get("running_tasks", 0),
        "pending": ts.get("pending_tasks", 0),
        "success_rate": 100.0 if total > 0 and succeeded > 0 else 0.0,
    }


async def _collect_state_tasks(status: str | None = None) -> list[dict[str, Any]]:
    """从全部管道 state（内存热数据 + DB 冷兜底）派生任务列表。

    task = pipeline（单一真值）：每行一个条目，**不过滤状态**——status 过滤
    仅由调用方 query 决定（2026-08-19 调试中心修复：原实现硬编码空列表）。
    行字段对齐前端 TaskInfo 子集，并携带 state 关键数据
    （current_phase/ended/suspended/message_count/tokens）。
    """
    try:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
    except Exception as exc:  # noqa: BLE001 — 能力未就绪降级空列表（契约不破坏）
        logger.warning("[monitoring] pipeline-state.list 调用失败（任务列表降级空）: %s", exc)
        return []
    if not isinstance(rows, list):
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("pipeline_id"):
            continue
        st = row.get("task.status") or row.get("status") or "unknown"
        if status and st != status:
            continue
        items.append({
            "id": row.get("task.id") or row.get("pipeline_id"),
            "task_id": row.get("task.id"),
            "pipeline_id": row.get("pipeline_id"),
            "thread_id": row.get("thread_id"),
            "agent_id": row.get("agent_id"),
            "title": row.get("task.goal") or row.get("display_name")
            or row.get("name") or row["pipeline_id"][:12],
            "status": st,
            "current_phase": row.get("current_phase"),
            "ended": row.get("ended"),
            "suspended": row.get("suspended"),
            "message_count": row.get("message_count"),
            "total_tokens": row.get("track.total_tokens")
            or row.get("cost_control.total_tokens"),
            "run_status": row.get("router.stop_reason"),
            "source": "pipeline_state",
        })
    return items


def _collect_token_usage() -> dict[str, Any]:
    """读 PerformanceMonitor._llm_stats，对齐前端 TokenUsage。"""
    monitor = _ensure_monitor()
    ls = monitor._llm_stats
    return {
        "total_tokens": 0,  # 本地累计不含 token 计数（record_llm_request 未传 tokens）
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "request_count": ls.get("request_count", 0),
        "active_requests": ls.get("active_requests", 0),
        "error_count": ls.get("error_count", 0),
        "total_response_time": ls.get("total_response_time", 0.0),
    }


def _collect_cache_stats() -> dict[str, Any]:
    """读 PerformanceMonitor._tool_stats，对齐前端 CacheStats。"""
    monitor = _ensure_monitor()
    ts = monitor._tool_stats
    hits = ts.get("cache_hits", 0)
    misses = ts.get("cache_misses", 0)
    total = hits + misses
    return {
        "cache_hits": hits,
        "cache_misses": misses,
        "hit_rate": (hits / total * 100) if total > 0 else 0.0,
        "total_requests": total,
    }


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/monitoring/** (monitoring business REST)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到 5 个 monitoring 端点。

    前端 service 取字段时套了 envelope（data.metrics / data.statistics / data.token_usage /
    data.cache_stats / data.items+total），故返回 JSON 须包这些 key。
    """
    try:
        if path == "/ext/monitoring/system/metrics" and method == "GET":
            return _ok(_json_response({"metrics": _collect_system_metrics()}))

        if path == "/ext/monitoring/tasks/statistics" and method == "GET":
            return _ok(_json_response({"statistics": _collect_task_statistics()}))

        if path == "/ext/monitoring/tasks" and method == "GET":
            # task list：全部管道 state 派生（内存热 + DB 冷兜底，不过滤状态；
            # status 过滤由 query 决定）。对齐 TaskListResponse shape。
            q = query or {}

            def _qint(key: str, default: int) -> int:
                try:
                    return int(q.get(key, default))
                except (TypeError, ValueError):
                    return default

            page = max(1, _qint("page", 1))
            page_size = min(200, max(1, _qint("page_size", 20)))
            items = await _collect_state_tasks(status=q.get("status") or None)
            start = (page - 1) * page_size
            return _ok(_json_response({
                "items": items[start:start + page_size],
                "total": len(items),
                "page": page,
                "page_size": page_size,
            }))

        if path == "/ext/monitoring/token-usage" and method == "GET":
            return _ok(_json_response({"token_usage": _collect_token_usage()}))

        if path == "/ext/monitoring/cache-stats" and method == "GET":
            return _ok(_json_response({"cache_stats": _collect_cache_stats()}))

        # ── T4：Payload 诊断快照（列目录 + 读单文件）──
        # 数据源：logs/payload_diag/ 下 adapter.py 写的 {ts}_{model}_{hash}_{n}msg.json
        if path == "/ext/monitoring/payload-diag" and method == "GET":
            return _ok(_json_response({"items": _list_payload_diag(), "total": len(_list_payload_diag())}))

        if path == "/ext/monitoring/payload-diag/file" and method == "GET":
            q = query or {}
            name = q.get("name", "")
            return _ok(_json_response(_read_payload_diag(name)))

        # ── T5：工具调用记录（json_each 解包 traces.patch_data.tool_results）──
        if path == "/ext/monitoring/tool-calls" and method == "GET":
            q = query or {}
            return _ok(_json_response(_query_tool_calls(q)))

        # ── T4/T5 webview 页面 HTML ──
        if path == "/ext/monitoring/page/payload-diag" and method == "GET":
            return _ok(_html_response(_PAYLOAD_DIAG_HTML))

        if path == "/ext/monitoring/page/tool-calls" and method == "GET":
            return _ok(_html_response(_TOOL_CALLS_HTML))

        # ── execution/records 域（channel_api 退役批次 2 自持迁移）──
        # 数据 = 内核只读能力桥（kernel_reads：pipeline-runs.list/messages.list/
        # pipeline-state.list），能力不可用降级空结构（HTTP 200 空载荷）。
        if path.startswith("/ext/monitoring/execution"):
            return await _handle_execution_domain(path, method, raw_body, query or {})

        # ── sessions token-usage 域（批次 2 随迁，stub 接真）──
        if path.startswith("/ext/monitoring/sessions"):
            return await _handle_sessions_domain(path, method, raw_body, query or {})

        # ── agent-calls 域（批次 3 接真：pipeline-runs/messages 组装调用视图）──
        if path.startswith("/ext/monitoring/agent-calls"):
            return await _handle_agent_calls_domain(path, method, raw_body, query or {})

        # ── search 域（批次 3 接真：pipeline-state/messages 全局搜索）──
        if path.startswith("/ext/monitoring/search"):
            return await _handle_search_domain(path, method, raw_body, query or {})

        logger.warning("http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:
        logger.exception("monitoring http.handle failed: %s", exc)
        return _error(f"monitoring service error: {exc}", 500)


# ════════════════════════════════════════════════════════════════════════════
# channel_api 退役批次 2/3 自持域分发（execution/records + sessions +
# agent-calls + search）——路径语义与 /ext/channel_api/** 原值逐项对齐，
# 数据统一经 kernel_reads 能力桥（provider 由 _on_load 注入）。
# ════════════════════════════════════════════════════════════════════════════


def _qint(query: dict[str, str], key: str, default: int) -> int:
    """读取 query 整型参数；缺失/非法回退 default。"""
    try:
        return int(query[key]) if key in query else default
    except (TypeError, ValueError):
        return default


async def _handle_execution_domain(
    path: str, method: str, raw_body: str, query: dict[str, str]
) -> dict[str, Any]:
    """execution 域分发：/ext/monitoring/execution/** → execution_records 业务函数。

    仅迁前端实际消费的 /records* 子集（前端 executionRecords.ts / 调试中心），
    与 channel_api 原分发逐项同构（含 404/500 语义）。
    """
    import execution_records as er  # noqa: PLC0415

    prefix = "/ext/monitoring/execution"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not an execution path", "path": path}, 404))
    sub = path[len(prefix):]  # "/records" / "/records/sessions" / "/records/{id}" / ...

    try:
        # GET /records（list，query: session_id/parent_record_id/limit/offset）
        if sub == "/records" and method == "GET":
            return _ok(_json_response(await er.list_execution_records(
                session_id=query.get("session_id"),
                parent_record_id=query.get("parent_record_id"),
                limit=_qint(query, "limit", 50),
                offset=_qint(query, "offset", 0),
            )))
        # GET /records/sessions
        if sub == "/records/sessions" and method == "GET":
            return _ok(_json_response(await er.get_execution_record_sessions()))
        # GET /records/group-summary（query: session_id）
        if sub == "/records/group-summary" and method == "GET":
            return _ok(_json_response(await er.get_record_group_summary(
                session_id=query.get("session_id"),
            )))
        # GET /records/tree/{session_id}（query: max_depth）
        if sub.startswith("/records/tree/") and method == "GET":
            session_id = sub[len("/records/tree/"):]
            return _ok(_json_response(await er.get_execution_tree(
                session_id, max_depth=_qint(query, "max_depth", 5),
            )))
        # GET /records/{record_id}/children
        if sub.endswith("/children") and sub.startswith("/records/") and method == "GET":
            record_id = sub[len("/records/"):-len("/children")]
            return _ok(_json_response(await er.get_children_records(record_id)))
        # GET /records/{record_id}
        if sub.startswith("/records/") and method == "GET" and "/" not in sub[len("/records/"):]:
            record_id = sub[len("/records/"):]
            return _ok(_json_response(await er.get_execution_record(record_id)))
        # DELETE /records/{record_id}
        if sub.startswith("/records/") and method == "DELETE" and "/" not in sub[len("/records/"):]:
            record_id = sub[len("/records/"):]
            return _ok(_json_response(await er.delete_execution_record(record_id)))
        # DELETE /records/session/{session_id}
        if sub.startswith("/records/session/") and method == "DELETE":
            session_id = sub[len("/records/session/"):]
            return _ok(_json_response(await er.delete_execution_records_by_session(session_id)))
        # POST /records/clear-all
        if sub == "/records/clear-all" and method == "POST":
            return _ok(_json_response(await er.clear_all_records()))

        logger.warning("execution http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        logger.error("execution http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_sessions_domain(
    path: str, method: str, raw_body: str, query: dict[str, str]
) -> dict[str, Any]:
    """sessions 域分发：/ext/monitoring/sessions/** → token-usage 接真业务函数。"""
    import execution_records as er  # noqa: PLC0415

    prefix = "/ext/monitoring/sessions"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a sessions path", "path": path}, 404))
    sub = path[len(prefix):]  # "/{session_id}/total-token-usage" 等

    try:
        if sub.endswith("/total-token-usage") and method == "GET":
            session_id = sub[1:].rsplit("/total-token-usage", 1)[0]
            return _ok(_json_response(await er.get_session_total_token_usage(session_id)))
        if sub.endswith("/context-token-usage") and method == "GET":
            session_id = sub[1:].rsplit("/context-token-usage", 1)[0]
            parent = query.get("parent_execution_record_id")
            return _ok(_json_response(
                await er.get_session_context_token_usage(session_id, parent_execution_record_id=parent)
            ))

        logger.warning("sessions http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        logger.error("sessions http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_agent_calls_domain(
    path: str, method: str, raw_body: str, query: dict[str, str]
) -> dict[str, Any]:
    """agent-calls 域分发：/ext/monitoring/agent-calls/** → 管道运行聚合业务函数。"""
    import agent_calls as ac  # noqa: PLC0415

    prefix = "/ext/monitoring/agent-calls"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not an agent-calls path", "path": path}, 404))
    sub = path[len(prefix):]  # "" / "/statistics" / "/{execution_id}"

    try:
        if sub in ("", "/") and method == "GET":
            return _ok(_json_response(await ac.list_agent_calls(
                limit=_qint(query, "limit", 50),
                offset=_qint(query, "offset", 0),
            )))
        if sub == "/statistics" and method == "GET":
            return _ok(_json_response(await ac.get_agent_call_statistics()))
        if sub.startswith("/") and len(sub) > 1 and method == "GET":
            exec_id = sub[1:]
            return _ok(_json_response(await ac.get_agent_call(exec_id)))

        logger.warning("agent-calls http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        logger.error("agent-calls http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_search_domain(
    path: str, method: str, raw_body: str, query: dict[str, str]
) -> dict[str, Any]:
    """search 域分发：/ext/monitoring/search → 内核搜索业务函数（会话标题+消息内容）。

    GET /search?q=xxx&type=all|session|message&limit=20 统一搜索。
    响应形态对齐前端 services/api/search.ts（SearchResponse）。type 非法 → 422
    （语义同原 APIError VAL_ENUM_7002）。
    """
    import routes_search as rsearch  # noqa: PLC0415

    prefix = "/ext/monitoring/search"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a search path", "path": path}, 404))
    sub = path[len(prefix):]  # "" / "/"

    try:
        if sub in ("", "/") and method == "GET":
            return _ok(_json_response(await rsearch.search(
                q=query.get("q", ""),
                type=query.get("type", "all"),
                limit=_qint(query, "limit", 20),
            )))

        logger.warning("search http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except ValueError as exc:
        # 参数校验失败（type 枚举非法）：422，语义对齐原 search APIError
        return _ok(_json_response({"error": str(exc)}, 422))
    except Exception as exc:  # noqa: BLE001
        logger.error("search http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


# ════════════════════════════════════════════════════════════════════════════
# T4/T5：可观测性 webview 端点（Payload 诊断 + 工具调用记录）
# ════════════════════════════════════════════════════════════════════════════


def _html_response(html: str) -> dict[str, Any]:
    """包 HTML 成 HttpHandleResponse（body base64，Content-Type text/html）。"""
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": base64.b64encode(html.encode("utf-8")).decode("ascii"),
        "body_encoding": "base64",
    }


# ── T4：Payload 诊断 ──────────────────────────────────────────────────────


def _resolve_project_root() -> str:
    """向上探测项目根（含 config/models 的目录），sidecar cwd 会漂移到插件目录。

    与写入方 adapter.py 的锚定逻辑同源（2026-08-19 修复）：两端都用
    AGENTOS_LOG_DIR 优先 + 文件位置向上探测，保证读写落在同一目录。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cand = here
    while cand and cand != os.path.dirname(cand):
        if os.path.isdir(os.path.join(cand, "config", "models")):
            return cand
        cand = os.path.dirname(cand)
    return here


def _payload_diag_dir() -> str:
    """返回 payload_diag 目录路径（与 adapter.py 的写入端同源锚定）。"""
    return os.path.join(
        os.environ.get("AGENTOS_LOG_DIR") or _resolve_project_root(),
        "logs", "payload_diag",
    )


def _list_payload_diag() -> list[dict[str, Any]]:
    """列 logs/payload_diag/ 目录，解析文件名返回元数据列表。

    文件名格式：{ts}_{model}_{msgs_hash}_{msg_count}msg.json
    返回按时间倒序的元数据数组，供前端列表展示。
    """
    import glob

    diag_dir = _payload_diag_dir()
    items: list[dict[str, Any]] = []
    for fpath in glob.glob(os.path.join(diag_dir, "*.json")):
        fname = os.path.basename(fpath)
        meta = _parse_payload_diag_filename(fname)
        if meta is None:
            continue
        try:
            meta["size"] = os.path.getsize(fpath)
            meta["name"] = fname
            items.append(meta)
        except OSError:
            continue
    items.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return items


def _parse_payload_diag_filename(fname: str) -> dict[str, Any] | None:
    """解析文件名 {ts}__{model}__{msgs_hash}__{msg_count}msg.json。

    用双下划线 __ 作字段分隔，model 内部单下划线保留（如 deepseek-v4-flash）。
    """
    if not fname.endswith(".json"):
        return None
    stem = fname[:-5]  # 去 .json
    if not stem.endswith("msg"):
        return None
    stem = stem[:-3]  # 去 msg
    parts = stem.split("__")
    if len(parts) < 4:
        return None
    try:
        ts = int(parts[0])
        msg_count = int(parts[-1])
        msgs_hash = parts[-2]
        # model 是 parts[1] 到 parts[-2] 之间所有段的拼接（model 理论上不含 __，
        # 但防御性处理：万一 model 含特殊字符被 sanitize 转成 _ 后又碰巧相邻，join 回去）
        model = "__".join(parts[1:-2]) if len(parts) > 4 else parts[1]
    except ValueError:
        return None
    return {
        "ts": ts,
        "model": model,
        "msgs_hash": msgs_hash,
        "msg_count": msg_count,
    }


def _read_payload_diag(name: str) -> dict[str, Any]:
    """读单个 payload snapshot 文件，返回完整 body JSON。

    防路径穿越：只允许纯文件名（不含路径分隔符或 ..）。

    Args:
        name: 文件名（如 1723380000000_deepseek-v4-flash_a1b2c3d4_12msg.json）

    Returns:
        含 content 字段（原始 body JSON 字符串）的字典；文件不存在返回 error。
    """
    # 严格防路径穿越：不允许任何路径分隔符或 ..
    if not name or "/" in name or "\\" in name or ".." in name:
        return {"error": "invalid filename"}
    if not name.endswith(".json"):
        return {"error": "not a json file"}
    fpath = os.path.join(_payload_diag_dir(), name)
    if not os.path.isfile(fpath):
        return {"error": "file not found", "name": name}
    try:
        with open(fpath, encoding="utf-8") as fh:
            return {"name": name, "content": fh.read()}
    except OSError as exc:
        return {"error": str(exc)}


# ── T5：工具调用记录（json_each 解包 traces.patch_data.tool_results）─────


def _kernel_db_path() -> str:
    """返回 kernel SQLite 路径（与 agentos-kernel.rs 的 AGENTOS_DB_PATH 一致）。"""
    return os.environ.get("AGENTOS_DB_PATH", os.path.join(os.getcwd(), "agentos_kernel.db"))


def _query_tool_calls(q: dict[str, str]) -> dict[str, Any]:
    """从 traces 表查询工具调用记录。

    用 json_each 解包 patch_data.tool_results 数组，支持按 tool_name/status/
    min_duration 筛选。不改 schema，纯查询层。

    Args:
        q: 查询参数（tool_name / status / min_duration / limit）

    Returns:
        含 items + total 的字典
    """
    import sqlite3

    db_path = _kernel_db_path()
    if not os.path.isfile(db_path):
        return {"items": [], "total": 0, "error": "kernel db not found"}

    limit = min(int(q.get("limit", 50)), 200)
    tool_name_filter = q.get("tool_name", "").strip()
    status_filter = q.get("status", "").strip()  # success / error
    min_duration = q.get("min_duration", "").strip()

    sql = """
        SELECT t.trace_id, t.run_id, t.created_at,
               json_extract(item.value, '$.tool_name')   AS tool_name,
               json_extract(item.value, '$.success')     AS success,
               json_extract(item.value, '$.error')       AS error,
               json_extract(item.value, '$.duration_ms') AS duration_ms
        FROM traces t, json_each(t.patch_data, '$.tool_results') AS item
        WHERE t.plugin_id = 'pipeline_tool_core'
    """
    params: list[Any] = []
    if tool_name_filter:
        sql += " AND json_extract(item.value, '$.tool_name') = ?"
        params.append(tool_name_filter)
    if status_filter == "success":
        sql += " AND json_extract(item.value, '$.success') = 1"
    elif status_filter == "error":
        sql += " AND json_extract(item.value, '$.success') = 0"
    if min_duration:
        # 先解析再追加 SQL：非法 min_duration 只忽略过滤条件，
        # 不能让 SQL 已含占位符而 params 缺绑定（报 binding 数不匹配）
        try:
            min_duration_f = float(min_duration)
        except (TypeError, ValueError):
            min_duration_f = None
        if min_duration_f is not None:
            sql += " AND CAST(json_extract(item.value, '$.duration_ms') AS REAL) >= ?"
            params.append(min_duration_f)
    sql += " ORDER BY t.created_at DESC LIMIT ?"
    params.append(limit)

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return {"items": [], "total": 0, "error": str(exc)}

    items = [dict(r) for r in rows]
    return {"items": items, "total": len(items)}


# ── T4 webview HTML：Payload 诊断查看器 ──────────────────────────────────

_PAYLOAD_DIAG_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Payload 诊断</title>
<style>
  body { font-family: -apple-system, 'Segoe UI', sans-serif; margin: 0; padding: 12px; color: #1a1a1a; background: #f8fafc; font-size: 13px; }
  h2 { margin: 0 0 12px; font-size: 15px; }
  .layout { display: flex; gap: 12px; height: calc(100vh - 80px); }
  .list { width: 360px; overflow-y: auto; border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; }
  .item { padding: 8px 10px; border-bottom: 1px solid #e2e8f0; cursor: pointer; }
  .item:hover { background: #f1f5f9; }
  .item.active { background: #dbeafe; }
  .item .ts { color: #64748b; font-size: 11px; }
  .item .model { color: #0369a1; font-weight: 600; }
  .item .meta { color: #64748b; font-size: 11px; }
  .detail { flex: 1; overflow: auto; border: 1px solid #cbd5e1; border-radius: 6px; padding: 10px; background: #fff; }
  pre { white-space: pre-wrap; word-break: break-all; margin: 0; font-family: 'Consolas', monospace; font-size: 12px; }
  .toolbar { margin-bottom: 8px; display: flex; gap: 8px; align-items: center; }
  button { padding: 4px 10px; background: #e2e8f0; color: #1e293b; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; }
  button:hover { background: #cbd5e1; }
  input { padding: 4px 8px; background: #fff; color: #1e293b; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 12px; }
  .empty { color: #94a3b8; padding: 20px; text-align: center; }
  .msg-role { color: #7c3aed; font-weight: 600; }
  #status { color: #94a3b8; font-size: 11px; }
</style></head><body>
<h2>LLM Payload 诊断（最近 200 次调用快照）</h2>
<div class="toolbar">
  <input id="filter" placeholder="按 model 过滤..." oninput="renderList()">
  <button onclick="refresh()">刷新</button>
  <span id="count" style="color:#64748b"></span>
  <span id="status"></span>
</div>
<div class="layout">
  <div class="list" id="list"><div class="empty">加载中...</div></div>
  <div class="detail" id="detail"><div class="empty">选择左侧条目查看完整 payload</div></div>
</div>
<script>
// agentosFetch：通过 window.agentos.postMessage（宿主代 fetch 带 token）+ Promise 化
// webview iframe 是 sandbox（无 same-origin），直接 fetch 会失败（无 auth）。
function agentosFetch(path, params) {
  return new Promise(function(resolve, reject) {
    if (!window.agentos) { reject(new Error('window.agentos 不可用')); return; }
    var id = window.agentos.postMessage(path, params);
    function handler(e) {
      var d = e.data;
      if (!d || !d.__agentos_webview || d.id !== id) return;
      window.removeEventListener('message', handler);
      if (d.method && d.method.indexOf('.error') > -1) reject(new Error(JSON.stringify(d.params || d.error)));
      else resolve(d.params);
    }
    window.addEventListener('message', handler);
  });
}
// 从响应里提取业务数据（宿主返回的 axios res.data，内层 data.body 是 base64 JSON）
function unwrap(res) {
  // res 可能是 {data: {body: base64, ...}} 或直接 {body: base64}
  var data = (res && res.data) ? res.data : res;
  if (data && data.body) {
    try { return JSON.parse(atob(data.body)); } catch(e) { return data; }
  }
  return data;
}

let allItems = [];
let activeName = null;
function setStatus(s) { document.getElementById('status').textContent = s || ''; }

async function load() {
  setStatus('加载中...');
  try {
    var res = await agentosFetch('/ext/monitoring/payload-diag');
    var d = unwrap(res);
    allItems = d.items || [];
    document.getElementById('count').textContent = '(' + allItems.length + ')';
    setStatus('');
    renderList();
  } catch(e) {
    document.getElementById('list').innerHTML = '<div class="empty">加载失败: '+escapeHtml(String(e))+'</div>';
    setStatus('加载失败');
  }
}
function fmt(ts) {
  var d = new Date(ts);
  return d.toLocaleString('zh-CN', {hour12: false});
}
function renderList() {
  var f = document.getElementById('filter').value.toLowerCase();
  var items = allItems.filter(function(i) { return !f || (i.model||'').toLowerCase().indexOf(f) >= 0; });
  var html = items.map(function(i) {
    return '<div class="item'+(i.name===activeName?' active':'')+'" data-name="'+i.name+'">'+
      '<div class="ts">'+fmt(i.ts)+'</div>'+
      '<div class="model">'+escapeHtml(i.model||'?')+'</div>'+
      '<div class="meta">'+i.msg_count+'msg · '+(i.msgs_hash||'').slice(0,8)+' · '+(i.size?Math.round(i.size/1024)+'KB':'-')+'</div>'+
      '</div>';
  }).join('');
  var listEl = document.getElementById('list');
  listEl.innerHTML = html || '<div class="empty">无数据</div>';
  listEl.querySelectorAll('.item').forEach(function(el) {
    el.addEventListener('click', function() { show(el.getAttribute('data-name')); });
  });
}
async function show(name) {
  activeName = name;
  renderList();
  setStatus('读取中...');
  try {
    // GET 请求：params 必须 undefined（宿主约定 params!==undefined → POST）。
    // 查询参数拼进 method 路径。
    var res = await agentosFetch('/ext/monitoring/payload-diag/file?name=' + encodeURIComponent(name));
    var d = unwrap(res);
    if (d.error) { document.getElementById('detail').innerHTML = '<div class="empty">'+escapeHtml(d.error)+'</div>'; setStatus(''); return; }
    var body = JSON.parse(d.content);
    var html = '';
    body.messages.forEach(function(m, i) {
      var content = typeof m.content === 'string' ? m.content : JSON.stringify(m.content, null, 2);
      var preview = content.length > 2000 ? content.slice(0,2000)+'\\n... [截断]' : content;
      html += '<div style="margin-bottom:8px"><span class="msg-role">['+i+'] '+(m.role||'?')+(m.name?' / '+escapeHtml(m.name):'')+'</span><pre>'+escapeHtml(preview)+'</pre></div>';
    });
    document.getElementById('detail').innerHTML = '<div class="toolbar"><b>model:</b> '+escapeHtml(body.model||'?')+' &nbsp; <b>messages:</b> '+body.messages.length+' 条</div>'+html;
    setStatus('');
  } catch(e) {
    document.getElementById('detail').innerHTML = '<div class="empty">读取失败: '+escapeHtml(String(e))+'</div>';
    setStatus('读取失败');
  }
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
function refresh() { load(); }
load();
</script></body></html>"""


# ── T5 webview HTML：工具调用记录查看器 ──────────────────────────────────

_TOOL_CALLS_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>工具调用记录</title>
<style>
  body { font-family: -apple-system, 'Segoe UI', sans-serif; margin: 0; padding: 12px; color: #1a1a1a; background: #f8fafc; font-size: 13px; }
  h2 { margin: 0 0 12px; font-size: 15px; }
  .toolbar { margin-bottom: 12px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  input, select { padding: 4px 8px; background: #fff; color: #1e293b; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 12px; }
  button { padding: 4px 10px; background: #e2e8f0; color: #1e293b; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; }
  button:hover { background: #cbd5e1; }
  table { width: 100%; border-collapse: collapse; background: #fff; }
  th { text-align: left; padding: 8px; background: #f1f5f9; color: #475569; font-size: 11px; text-transform: uppercase; position: sticky; top: 0; }
  td { padding: 6px 8px; border-bottom: 1px solid #e2e8f0; }
  tr:hover { background: #f8fafc; }
  .ok { color: #16a34a; }
  .fail { color: #dc2626; }
  .slow { color: #d97706; font-weight: 600; }
  .empty { color: #94a3b8; text-align: center; padding: 40px; }
  #status { color: #94a3b8; font-size: 11px; }
</style></head><body>
<h2>工具调用记录（从 traces 表查询）</h2>
<div class="toolbar">
  <input id="f_tool" placeholder="工具名(精确)" style="width:140px">
  <select id="f_status">
    <option value="">全部状态</option>
    <option value="success">成功</option>
    <option value="error">失败</option>
  </select>
  <input id="f_dur" placeholder="最小耗时(ms)" style="width:120px" type="number">
  <button onclick="query()">查询</button>
  <span id="count" style="color:#64748b"></span>
  <span id="status"></span>
</div>
<div style="overflow:auto; max-height:calc(100vh - 120px)">
  <table>
    <thead><tr><th>时间</th><th>工具</th><th>状态</th><th>耗时</th><th>run_id</th><th>错误</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="empty" class="empty">点击查询加载数据</div>
</div>
<script>
// agentosFetch：通过 window.agentos.postMessage（宿主代 fetch 带 token）
function agentosFetch(path, params) {
  return new Promise(function(resolve, reject) {
    if (!window.agentos) { reject(new Error('window.agentos 不可用')); return; }
    var id = window.agentos.postMessage(path, params);
    function handler(e) {
      var d = e.data;
      if (!d || !d.__agentos_webview || d.id !== id) return;
      window.removeEventListener('message', handler);
      if (d.method && d.method.indexOf('.error') > -1) reject(new Error(JSON.stringify(d.params || d.error)));
      else resolve(d.params);
    }
    window.addEventListener('message', handler);
  });
}
function unwrap(res) {
  var data = (res && res.data) ? res.data : res;
  if (data && data.body) {
    try { return JSON.parse(atob(data.body)); } catch(e) { return data; }
  }
  return data;
}
function setStatus(s) { document.getElementById('status').textContent = s || ''; }

async function query() {
  // GET 请求通过 postMessage 时，params 传查询参数对象（宿主 GET 模式）
  // 但宿主逻辑：params !== undefined → POST。GET 端点要用 query string。
  // webview 协议：method 以 / 开头视为 REST。GET 无 params，POST 有 params。
  // 这里我们用 POST 风格：method = 完整路径（含 query string），params = undefined → GET
  var t = document.getElementById('f_tool').value.trim();
  var s = document.getElementById('f_status').value;
  var d = document.getElementById('f_dur').value.trim();
  var qs = [];
  if (t) qs.push('tool_name=' + encodeURIComponent(t));
  if (s) qs.push('status=' + s);
  if (d) qs.push('min_duration=' + d);
  var path = '/ext/monitoring/tool-calls' + (qs.length ? '?' + qs.join('&') : '');
  setStatus('查询中...');
  try {
    var res = await agentosFetch(path);
    var data = unwrap(res);
    var items = data.items || [];
    document.getElementById('count').textContent = '(' + items.length + ')';
    if (data.error) {
      document.getElementById('empty').textContent = '错误: ' + data.error;
      document.getElementById('empty').style.display = 'block';
      document.getElementById('rows').innerHTML = '';
      setStatus('查询错误');
      return;
    }
    if (!items.length) {
      document.getElementById('empty').style.display = 'block';
      document.getElementById('rows').innerHTML = '';
      setStatus('无数据');
      return;
    }
    document.getElementById('empty').style.display = 'none';
    document.getElementById('rows').innerHTML = items.map(function(i) {
      var ok = i.success === 1 || i.success === true;
      var dur = parseFloat(i.duration_ms || 0);
      var durCls = dur > 1000 ? 'slow' : '';
      return '<tr><td>' + fmt(i.created_at) + '</td><td>' + escapeHtml(i.tool_name || '?') + '</td>' +
        '<td class="' + (ok ? 'ok' : 'fail') + '">' + (ok ? '成功' : '失败') + '</td>' +
        '<td class="' + durCls + '">' + dur.toFixed(0) + 'ms</td>' +
        '<td style="color:#94a3b8;font-size:11px">' + escapeHtml((i.run_id || '').slice(0,8)) + '</td>' +
        '<td style="color:#dc2626;font-size:11px">' + escapeHtml((i.error || '').slice(0,80)) + '</td></tr>';
    }).join('');
    setStatus('');
  } catch(e) {
    document.getElementById('empty').textContent = '查询失败: ' + escapeHtml(String(e));
    document.getElementById('empty').style.display = 'block';
    setStatus('查询失败');
  }
}
function fmt(s) {
  if (!s) return '';
  try { return new Date(s).toLocaleString('zh-CN', {hour12: false}); } catch(e) { return s; }
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
</script></body></html>"""


if __name__ == "__main__":
    plugin.run()
