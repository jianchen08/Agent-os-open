#!/usr/bin/env python3
"""Monitoring Service MCP 服务端——纯接口适配层。

老代码从 0.1 src/monitoring/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

核心能力：
- HTTP 面 /ext/monitoring/**：系统指标/健康/执行记录读面（前端与运维消费）
- LLM 工具面已退役（工具面审查 2026-10-19：记录/诊断件归 HTTP 面，不占 LLM 声明）

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
import time
from collections.abc import Awaitable, Callable
from typing import Any

import psutil

from agentos_plugin_sdk.bootstrap import bootstrap_plugin
from agentos_plugin_sdk.diagnostics_settings import payload_diag_dir

_paths = bootstrap_plugin(__file__)  # 插件目录（kernel_reads 等平铺模块）+ plugins/shared 根入 sys.path

import kernel_reads  # noqa: E402 — 平铺同目录模块（bootstrap 已插插件目录）
from _root_api import (  # noqa: E402 — HTTP 响应封装公共实现，调用点零改名
    decode_body as _decode_body,
    error as _error,
    json_response as _json_response,
    ok as _ok,
    protocol_error as _protocol_error,
)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("monitoring")

# 全局 PerformanceMonitor 实例
_monitor: Any = None
# 监控 M7：后台指标上报任务（把 D 类系统资源经 record_metric 推给内核聚合器）。
_reporter_task: asyncio.Task | None = None
# 细粒度内存观测（10s 级 per-process RSS 运维日志，mem_watch.py）。
_mem_watch: Any = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize performance monitor on load."""
    global _monitor, _reporter_task, _mem_watch, _plugin_health_task
    from performance_monitor import PerformanceMonitor

    _monitor = PerformanceMonitor()
    await _monitor.start()
    # M7：启动后台 record_metric 上报循环（系统资源 → 内核聚合器）
    _reporter_task = asyncio.create_task(_report_metrics_loop())
    # 卡9：启动插件组健康后台轮询（插件语境系统读，无用户凭证自动评估告警）
    _plugin_health_task = asyncio.create_task(_plugin_health_loop())
    from mem_watch import MemWatch

    _mem_watch = MemWatch()
    await _mem_watch.start()
    logger.info("Monitoring service started (record_metric reporter + mem_watch enabled)")

    # 调试中心数据链：execution/sessions/search 域真实数据 =
    # 内核只读能力（messages.list / pipeline-runs.list / pipeline-state.list /
    # db-admin.table_query），经 kernel_reads 桥接注入；能力未就绪时
    # handler 降级空载荷（前端契约不破坏）。
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

        async def _kr_list_traces(pipeline_id: str):
            handle = plugin.get_capability("service-registry")
            return await handle.call("traces.list_by_pipeline", {"pipeline_id": pipeline_id})

        async def _kr_list_runs_by_pipeline(pipeline_id: str):
            handle = plugin.get_capability("service-registry")
            return await handle.call("pipeline-runs.list_by_pipeline", {"pipeline_id": pipeline_id})

        async def _kr_query_table(table: str, filter: list[str] | None = None, sort: str = "", limit: int = 500):
            params: dict[str, Any] = {"table": table, "limit": int(limit)}
            if filter:
                params["filter"] = filter
            if sort:
                params["sort"] = sort
            handle = plugin.get_capability("db-admin")
            return await handle.call("table_query", params)

        async def _kr_clear_execution_data(authorization: str = ""):
            params: dict[str, Any] = {}
            if authorization:
                params["_authorization"] = authorization
            handle = plugin.get_capability("db-admin")
            return await handle.call("clear_execution_data", params)

        # metrics-admin 读面（插件运行态：process.* gauge + lifecycle 计数）；
        # 鉴权凭证原样透传，内核 handler 侧 resolve_request_user 校验 admin/viewer。
        async def _kr_metrics_admin_list(authorization: str = ""):
            params: dict[str, Any] = {}
            if authorization:
                params["_authorization"] = authorization
            handle = plugin.get_capability("metrics-admin")
            return await handle.call("list", params)

        async def _kr_metrics_admin_query(
            authorization: str = "", plugin_id: str = "", window: str = "24h", metric: str = ""
        ):
            params: dict[str, Any] = {"window": window}
            if authorization:
                params["_authorization"] = authorization
            if plugin_id:
                params["plugin"] = plugin_id
            if metric:
                params["metric"] = metric
            handle = plugin.get_capability("metrics-admin")
            return await handle.call("query", params)

        # db-read 内核库读契约（C2 刀2）：provider 只做原始 capability 调用
        #（信封原样回传），解包单点在共享 helper unwrap_db_read_envelope
        #（kernel_reads.db_read_query 消费）；身份经 _authorization 原头转发
        # 内核复权。
        async def _kr_db_read(
            face: str,
            sql: str,
            params: dict[str, Any] | None = None,
            authorization: str = "",
        ):
            call_params: dict[str, Any] = {"face": face, "sql": sql}
            if params:
                call_params["params"] = params
            if authorization:
                call_params["_authorization"] = authorization
            handle = plugin.get_capability("db-read")
            return await handle.call("query", call_params)

        kernel_reads.set_provider("pipeline-runs", _kr_list_pipeline_runs)
        kernel_reads.set_provider("messages", _kr_list_messages)
        kernel_reads.set_provider("pipeline-state", _kr_list_state_rows)
        kernel_reads.set_provider("traces", _kr_list_traces)
        kernel_reads.set_provider("runs-by-pipeline", _kr_list_runs_by_pipeline)
        kernel_reads.set_provider("db-admin-query", _kr_query_table)
        kernel_reads.set_provider("db-admin-clear", _kr_clear_execution_data)
        kernel_reads.set_provider("metrics-admin-list", _kr_metrics_admin_list)
        kernel_reads.set_provider("metrics-admin-query", _kr_metrics_admin_query)
        kernel_reads.set_provider("db-read", _kr_db_read)
    except Exception as exc:  # noqa: BLE001 — 注入失败降级（handler 返回空结构）
        logger.warning("[monitoring] kernel_reads provider 注入失败: %s", exc)


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Stop monitor and cleanup on unload."""
    global _monitor, _reporter_task, _mem_watch, _plugin_health_task
    if _reporter_task is not None and not _reporter_task.done():
        _reporter_task.cancel()
        try:
            await _reporter_task
        except asyncio.CancelledError:
            pass
    _reporter_task = None
    if _plugin_health_task is not None and not _plugin_health_task.done():
        _plugin_health_task.cancel()
        try:
            await _plugin_health_task
        except asyncio.CancelledError:
            pass
    _plugin_health_task = None
    if _mem_watch is not None:
        await _mem_watch.stop()
        _mem_watch = None
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
        # get_system_metrics 签名 async 但内部 psutil.cpu_percent(interval=0.1)
        # 同步阻塞 ~100ms（X26-F1）：经 to_thread + asyncio.run 桥落工作线程，
        # 不阻塞本事件循环（该 coroutine 内无 await 依赖，临时 loop 桥安全）
        system = await asyncio.to_thread(asyncio.run, monitor.get_system_metrics())
    except Exception as exc:  # noqa: BLE001 — 采样失败本轮放弃（5s 后重试），留痕可排查
        logger.debug("system metrics 采样失败（本轮跳过）: %s", exc)
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


# 插件组健康告警器（卡5/卡9：四条阈值规则，规则引擎 plugin_health_monitor）。
# 惰性创建；配置（config_files[plugin_health]）每次评估前刷新——设置页改阈值
# 即热生效，无需 respawn。
_plugin_health_monitor: Any = None
# 卡9：后台自动轮询任务（插件语境系统读，无用户凭证也可评估告警）。
_plugin_health_task: asyncio.Task | None = None


def _ensure_plugin_health_monitor() -> Any:
    """获取插件组健康告警器实例，未初始化则延迟创建。"""
    global _plugin_health_monitor
    if _plugin_health_monitor is None:
        from plugin_health_monitor import PluginHealthMonitor

        _plugin_health_monitor = PluginHealthMonitor()
    return _plugin_health_monitor


def _effective_health_config() -> dict[str, Any]:
    """取插件组健康生效配置（config_files[plugin_health] 命名空间，容错收敛）。"""
    from plugin_health_monitor import coerce_config

    injected = plugin.get_config() if hasattr(plugin, "get_config") else {}
    namespace = injected.get("plugin_health") if isinstance(injected, dict) else None
    return coerce_config(namespace)


async def _collect_health_inputs(window: str, authorization: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """取插件组健康两族数据源（崩溃跳变样本 + 卡9 内核计数器样本）。

    读面通道两轨：带 authorization = HTTP 面（内核 admin/viewer 复权）；
    缺省 = 插件后台轮询（插件语境系统读，内核按 `_plugin_id` 信任锚放行）。
    """
    samples = await kernel_reads.plugin_crash_samples(window=window, authorization=authorization)
    counters = await kernel_reads.kernel_plugin_counters(window=window, authorization=authorization)
    return samples, counters


async def _collect_plugin_health(authorization: str = "") -> dict[str, Any]:
    """评估插件组健康规则并返回命中告警（卡5/卡9 四条规则全量）。

    数据经 metrics-admin 读面（authorization 透传复权；能力未注入/调用失败
    降级空告警，kernel_reads 读面契约）。命中告警按冷却闸打日志
    （「插件组健康告警 [LEVEL]: …」，形态同性能告警先例）。
    """
    monitor = _ensure_plugin_health_monitor()
    effective = _effective_health_config()
    monitor.set_config(effective)

    samples, counters = await _collect_health_inputs(str(effective["window"]), authorization)
    alerts = await monitor.evaluate_and_alert(samples, kernel_counters=counters)
    return {
        "alerts": [alert.model_dump() for alert in alerts],
        "evaluated_at": time.time(),
    }


async def _plugin_health_loop() -> None:
    """卡9：后台自动轮询插件组健康（插件语境系统读，免用户凭证）。

    间隔走配置 poll_interval_secs（默认 60s）；单轮失败不退出循环（告警面
    故障不得拖垮插件），CancelledError 正常上抛随 unload 取消。
    """
    interval = 60.0  # 兜底间隔：首轮配置读取失败时 sleep 行仍有界可等
    while True:
        try:
            effective = _effective_health_config()
            interval = float(effective.get("poll_interval_secs", 60))
            monitor = _ensure_plugin_health_monitor()
            monitor.set_config(effective)
            samples, counters = await _collect_health_inputs(str(effective["window"]))
            await monitor.evaluate_and_alert(samples, kernel_counters=counters)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 单轮失败留痕即续（同 _report_metrics_loop）
            logger.warning("插件组健康后台轮询单轮失败: %s", e)
        await asyncio.sleep(max(1.0, interval))


# ── HTTP 端点（http.handle）—— 前端 /ext/monitoring/** 入口 ─────────────────
# 参照 cost_control 已验证模式：dispatcher 把 HttpHandleRequest 整体作 arguments 传入，
# 本工具按 path 分发到 5 个子端点，返回 ToolExecutionResult{success,data}，body base64。
# 系统指标直接用 psutil 采集完整数据（含 memory.total/used/available、disk.total/used/free），
# 对齐前端 SystemMetrics 嵌套 TS 类型（monitoring.ts:10-40）。
# task/token/cache 统计读 PerformanceMonitor 本地累计（注：业务数据散在别处插件，
# 本地累计可能为 0，属已知限制，后续应接内核 metrics 聚合查询）。


def _disk_sample_anchor() -> str:
    """磁盘卡采样锚点：本插件目录（server.py 所在）的绝对路径。

    X28-F1：旧实现固定 psutil.disk_usage("/")——Windows 下 psutil 按当前进程
    工作目录解析出某个盘的数据，标签却写 "/" 常量，语义失真且多盘机器只见
    一盘。锚定插件真实目录后，psutil 解析该路径所在盘（POSIX 解析其挂载
    点），数值与标签同为真值。取目录而非文件路径：Windows 的 disk_usage
    只收目录（文件路径报 WinError 267）。多盘枚举需前端卡片改造，另立项
    （P-155）。
    """
    return os.path.abspath(os.path.dirname(__file__))


def _disk_mount_label(anchor: str) -> str:
    """采样锚点的挂载标签：Windows 取盘符（如 "D:\\"），POSIX 无盘符回退 "/"。"""
    drive = os.path.splitdrive(anchor)[0]
    return drive + os.sep if drive else "/"


def _collect_system_metrics() -> dict[str, Any]:
    """用 psutil 采集完整系统指标，对齐前端 SystemMetrics 嵌套结构。

    前端期望：{cpu_usage, memory:{total,used,available,usage_percent}, disk:{mount_point,total,used,free,usage_percent}, uptime, timestamp}
    """
    cpu = psutil.cpu_percent(interval=0.1)
    vm = psutil.virtual_memory()
    anchor = _disk_sample_anchor()
    du = psutil.disk_usage(anchor)
    boot_ts = psutil.boot_time()
    now = datetime.datetime.now(datetime.UTC)
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
            "mount_point": _disk_mount_label(anchor),
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
    仅由调用方 query 决定。行字段对齐前端 TaskInfo 子集，并携带 state 关键数据
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
        # 状态链：task 域键优先（任务管道），缺席回退内核 run_status（引擎写的
        # 运行状态单一真值，state 基线出口）——聊天会话等非任务管道否则恒显
        # unknown（GUI 黑盒测试 2026-09-11）。双双缺席保持 unknown（不猜）。
        st = row.get("task.status") or row.get("status") or row.get("run_status") or "unknown"
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
            "total_tokens": row.get("track.total_tokens"),
            "run_status": row.get("run_status"),
            "source": "pipeline_state",
        })
    return items


# provider / 模型名前缀 → 展示图标（token 按模型表；未命中回退 🔤）
_MODEL_ICON_RULES: list[tuple[str, str]] = [
    ("deepseek", "🐋"),
    ("zhipu", "🤖"),
    ("glm", "🤖"),
    ("minimax", "🐚"),
    ("siliconflow", "⚙️"),
    ("openai", "🟢"),
    ("anthropic", "🎭"),
    ("mock", "🧪"),
]


def _model_icon(model: str, provider: str) -> str:
    """按 provider 优先、模型名前缀兜底映射图标。"""
    model_l = (model or "").lower()
    provider_l = (provider or "").lower()
    for prefix, icon in _MODEL_ICON_RULES:
        if provider_l.startswith(prefix) or model_l.startswith(prefix):
            return icon
    return "🔤"


def _empty_model_row(model: str, label: str) -> dict[str, Any]:
    """按模型聚合行模板：token 面由 state 累计填充，requests 由 traces COUNT 填充。"""
    return {
        "icon": _model_icon(model, ""),
        "model": label,
        "requests": 0,
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "total_tokens": 0.0,
        "cached_tokens": 0.0,
        "missed_tokens": 0.0,
    }


# traces/state 聚合读 patch_data 全表 JSON（库 1.4GB），单次耗时随库增长（实测
# 索引前 1.7-2.1s / 索引后 0.2ms）。查询经 db-read 契约（内核侧 spawn_blocking
# 执行，sidecar 事件循环只负责 await，不再自卸载线程）——但并发 miss 仍不被
# 天然串行化，显式 single-flight 保留：同键的并发请求共享同一次计算（否则
# N 个 widget 同刻到达就重复 N 次全表聚合）。同键结果另按 TTL 合并：TTL 15s
# ≤ 最短轮询间隔（30s），可见滞后不跨轮询周期。缓存键含租户：聚合结果按
# 调用方身份租户收窄（db-read 契约内建），跨租户共享同键即串数据。
_AGG_CACHE_TTL_SECS = 15.0
_aggregation_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_aggregation_inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
# by-time 时间窗上限（天）：查询参数防护栏，防全表范围外的大窗口滥用
_BY_TIME_MAX_DAYS = 90


async def _cached_aggregation(
    key: str, collect: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    now = time.monotonic()
    hit = _aggregation_cache.get(key)
    if hit is not None and now - hit[0] < _AGG_CACHE_TTL_SECS:
        return hit[1]

    existing = _aggregation_inflight.get(key)
    if existing is not None:
        return await asyncio.shield(existing)

    async def _compute() -> dict[str, Any]:
        try:
            return await collect()
        finally:
            _aggregation_inflight.pop(key, None)

    task = asyncio.ensure_future(_compute())
    _aggregation_inflight[key] = task
    payload = await asyncio.shield(task)
    _aggregation_cache[key] = (time.monotonic(), payload)
    return payload


async def _collect_token_usage(tenant_id: str = "", authorization: str = "") -> dict[str, Any]:
    """从 pipeline_state 聚合 LLM token 用量（累计真值源，db-read 契约面）。

    累计口径的唯一真值 = state 的 track.llm_usage（track 插件每轮 LLM 调用
    累加，逐管道自持）：跨运行累计、跨成员热重载保留。traces 的 llm_usage
    是逐轮明细（供按时间维度聚合与诊断），累计值以 state 为准——两源在今天
    新产生的管道上逐值一致，历史差异来自打包测试期的数据 churn。

    缓存命中口径：cached/missed 读 track.llm_usage 的 total_cached_tokens
    （历史管道缺该字段按 0 兜底，与「未记录模型」同一容错口径）；展示口径用
    百分数字段 cache_hit_pct（0–100，1 位小数），与 metrics 观测口径 gauge
    cache_ratio（0–1）、cost_update 的 cache_hit_ratio（0–1）面不同不混用。

    按模型归属取 state 的 llm_model 键（llm_core 写出，管道级单一模型）。
    调用次数真值 = traces llm_usage 逐轮明细按模型 COUNT（照 by-time 查询
    模式走 idx_traces_llm_usage 覆盖索引）——state 累计键不含次数，曾以
    PerformanceMonitor 本地计数顶数恒 0 假数据（P-147）；顶层 request_count
    = 各模型行之和，active_requests/error_count/total_response_time 无真值
    来源（进程内计数器无生产写入口）已退役（P-149 同刀）。数据面 =
    pipeline_state/traces tenant 档（db-read 契约，身份经 authorization 由
    内核复权后内建收窄——用量页为本租户口径，与 cost_control 用量页同纪律）；
    契约失败降级空行 + 次数 0（与 _query_tool_calls 同策略）。
    """
    rows: list[dict[str, Any]] = []
    if tenant_id:
        try:
            by_pipe: dict[str, dict[str, Any]] = {}
            # state 值域标量化（ADR 2026-09-18）：标量按 value_kind 解码，
            # value_kind='json' 的过渡键按 JSON 解析（非标量声明键域迁移消灭前）
            db_rows = await kernel_reads.db_read_query(
                face="pipeline_state",
                sql="SELECT pipeline_id, field_key, value, value_kind, tenant_id"
                " FROM pipeline_state"
                " WHERE field_key IN ('track.llm_usage', 'llm_model')",
                authorization=authorization,
            )
            for pid, key, val, kind, _row_tenant in db_rows:
                slot = by_pipe.setdefault(str(pid), {})
                if kind == "json":
                    try:
                        slot[str(key)] = json.loads(val) if isinstance(val, str) else None
                    except (TypeError, ValueError):
                        slot[str(key)] = None
                else:
                    slot[str(key)] = val
            # 同模型的多个管道汇总为一行（state 按管道自持累计，跨管道同一
            # 模型需相加才是「按模型」的全局口径）。
            merged: dict[str, dict[str, Any]] = {}
            for pid, slot in by_pipe.items():
                usage = slot.get("track.llm_usage")
                if not isinstance(usage, dict):
                    usage = {}
                model_raw = slot.get("llm_model")
                model = model_raw if isinstance(model_raw, str) else ""
                label = model or "（未记录模型）"
                acc = merged.setdefault(label, _empty_model_row(model, label))
                row_input = float(usage.get("total_input_tokens", 0) or 0)
                row_cached = float(usage.get("total_cached_tokens", 0) or 0)
                acc["input_tokens"] += row_input
                acc["output_tokens"] += float(usage.get("total_output_tokens", 0) or 0)
                acc["total_tokens"] += float(usage.get("total_tokens", 0) or 0)
                acc["cached_tokens"] += row_cached
                # 未命中按该管道自己的 input−cached 夹非负（cached>input 的脏行不产生负 missed）
                acc["missed_tokens"] += max(row_input - row_cached, 0.0)
            # 「调用次数」真值（P-147）：traces llm_usage 逐轮明细按模型 COUNT
            # （by-time 同款查询模式，idx_traces_llm_usage 覆盖索引支撑；GROUP BY
            # tenant_id 后交契约包装收窄，聚合形状纪律见 _collect_token_usage_by_time）。
            # 次数面独立降级（失败降 0，不牵连 token 行）；查出的新模型行 setdefault
            # 补入（token 面 0——累计 token 真值源仍是 state）。
            try:
                for raw_model, cnt, _row_tenant in (
                    await kernel_reads.db_read_query(
                        face="traces",
                        sql=(
                            "SELECT COALESCE(json_extract(patch_data,"
                            " '$.llm_usage.model'), ''), COUNT(*), tenant_id"
                            " FROM traces"
                            " WHERE json_extract(patch_data, '$.llm_usage') IS NOT NULL"
                            " GROUP BY tenant_id, 1"
                        ),
                        authorization=authorization,
                    )
                ):
                    model_s = raw_model if isinstance(raw_model, str) else ""
                    label_s = model_s or "（未记录模型）"
                    req_acc = merged.setdefault(label_s, _empty_model_row(model_s, label_s))
                    req_acc["requests"] += int(cnt or 0)
            except kernel_reads.DbReadQueryError as exc:  # noqa: BLE001 — 次数降级 0
                logger.warning("[monitoring] traces 调用次数聚合失败（降级 0）: %s", exc)
            rows = sorted(merged.values(), key=lambda r: -r["total_tokens"])
            # 命中率是行级派生值（cached/input），聚合后按行累计值一次算，不作累加字段
            for row in rows:
                row["cache_hit_pct"] = (
                    round(row["cached_tokens"] / row["input_tokens"] * 100, 1)
                    if row["input_tokens"] > 0
                    else 0.0
                )
        except kernel_reads.DbReadQueryError as exc:  # noqa: BLE001 — 契约失败降级本地计数
            logger.warning("[monitoring] state token 聚合失败（降级本地计数）: %s", exc)
            rows = []

    prompt = sum(r["input_tokens"] for r in rows)
    completion = sum(r["output_tokens"] for r in rows)
    total = sum(r["total_tokens"] for r in rows)
    cached = sum(r["cached_tokens"] for r in rows)
    missed = sum(r["missed_tokens"] for r in rows)
    # 顶层调用次数 = 按模型行之和（与表格同源同值，不另立第二查询口径）
    requests_total = sum(r["requests"] for r in rows)

    return {
        "total_tokens": total,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "missed_tokens": missed,
        # 顶层命中率用总 cached/总 input 派生（非各行 pct 相加）
        "cache_hit_pct": round(cached / prompt * 100, 1) if prompt > 0 else 0.0,
        # P-149：active_requests/error_count/total_response_time 已退役（无真值
        # 来源的进程内死计数器）；request_count 即 traces 调用次数真值
        "request_count": requests_total,
        # 按模型统计（表格 widget 消费 {columns, rows}，与 /plugins 端点同约定）
        "columns": [
            {"key": "icon", "label": "图标"},
            {"key": "model", "label": "模型"},
            {"key": "requests", "label": "调用次数"},
            {"key": "input_tokens", "label": "输入 Tokens"},
            {"key": "output_tokens", "label": "输出 Tokens"},
            {"key": "total_tokens", "label": "合计 Tokens"},
            {"key": "cached_tokens", "label": "缓存命中 Tokens"},
            {"key": "cache_hit_pct", "label": "命中率 (%)"},
        ],
        "rows": rows,
        # 图表形状（chart widget series 消费 {labels, datasets}；与 rows 同源同值）
        "labels": [r["model"] for r in rows],
        "datasets": [
            {"label": "输入 Tokens", "data": [r["input_tokens"] for r in rows]},
            {"label": "输出 Tokens", "data": [r["output_tokens"] for r in rows]},
            {"label": "缓存命中 Tokens", "data": [r["cached_tokens"] for r in rows]},
        ],
    }


async def _collect_token_usage_by_time(
    tenant_id: str = "",
    authorization: str = "",
    days: int = 0,
    since: str = "",
    until: str = "",
) -> dict[str, Any]:
    """按日（UTC）聚合 traces 的 llm_usage token 用量（时间维度，db-read 契约面）。

    时间维度只有 traces 有（state 的累加器是管道级单值，不含按日拆分）——
    这也是 traces 与 state 的分工：累计口径读 state，时间序列读 traces。
    created_at 为 ISO8601 UTC 文本，`substr(created_at, 1, 10)` 即日期——
    不经 datetime() 解析（纳秒精度 + 时区后缀解析行为版本相关）。

    按需加载：`days` 限定最近 N 天（0=全部）；`since`/`until` 为显式日期区间
    （ISO `YYYY-MM-DD`，闭区间，优先于 days）。限定区间可利用
    idx_traces_llm_usage 的日期前缀列做范围裁剪，库再涨也不拖慢首屏。
    缓存命中 SUM llm_usage.cached_tokens（早期轨迹无该字段时 json_extract
    为 NULL → COALESCE 按 0 计）；命中率同累计口径 cache_hit_pct（0–100，
    1 位小数，input≤0 时 0.0）。表格 widget 消费 {columns, rows}；chart
    widget 消费 {labels, datasets}（同源同值）；契约失败降级空行。

    数据面 = traces tenant 档（db-read 契约，身份经 authorization 内核复权
    后内建收窄）。聚合形状纪律：无 GROUP BY 租户列的裸聚合会把全租户聚成
    一行（租户列成任意值），包装无法正确收窄——按日聚合必须 GROUP BY
    tenant_id + 日桶后交契约包装（71dca0fff 实证教训）。
    """
    rows: list[dict[str, Any]] = []
    if tenant_id:
        try:
            where = ["json_extract(patch_data, '$.llm_usage') IS NOT NULL"]
            params: dict[str, Any] = {}
            if since:
                where.append("substr(created_at, 1, 10) >= :since")
                params["since"] = since
            if until:
                where.append("substr(created_at, 1, 10) <= :until")
                params["until"] = until
            if not since and not until and days > 0:
                where.append("substr(created_at, 1, 10) >= date('now', :days)")
                params["days"] = f"-{int(days)} days"
            sql = (
                "SELECT substr(created_at, 1, 10) AS day,"
                " COALESCE(SUM(json_extract(patch_data, '$.llm_usage.input_tokens')), 0),"
                " COALESCE(SUM(json_extract(patch_data, '$.llm_usage.output_tokens')), 0),"
                " COALESCE(SUM(json_extract(patch_data, '$.llm_usage.total_tokens')), 0),"
                " COALESCE(SUM(json_extract(patch_data, '$.llm_usage.cached_tokens')), 0),"
                " COUNT(*), tenant_id"
                " FROM traces WHERE " + " AND ".join(where) +
                " GROUP BY tenant_id, 1 ORDER BY 1 DESC"
            )
            for day, prompt, completion, total, cached, reqs, _row_tenant in (
                await kernel_reads.db_read_query(
                    face="traces", sql=sql, params=params, authorization=authorization
                )
            ):
                day_input = float(prompt or 0)
                day_cached = float(cached or 0)
                rows.append({
                    "date": day or "unknown",
                    "requests": int(reqs or 0),
                    "input_tokens": day_input,
                    "output_tokens": float(completion or 0),
                    "total_tokens": float(total or 0),
                    # 未命中夹非负（cached>input 的脏轨迹不产生负 missed）；input≤0 不除零
                    "cached_tokens": day_cached,
                    "missed_tokens": max(day_input - day_cached, 0.0),
                    "cache_hit_pct": (
                        round(day_cached / day_input * 100, 1) if day_input > 0 else 0.0
                    ),
                })
        except kernel_reads.DbReadQueryError as exc:  # noqa: BLE001 — 契约失败降级空行
            logger.warning("[monitoring] traces 按日 token 聚合失败（降级空）: %s", exc)
            rows = []

    return {
        "columns": [
            {"key": "date", "label": "日期 (UTC)"},
            {"key": "requests", "label": "调用次数"},
            {"key": "input_tokens", "label": "输入 Tokens"},
            {"key": "output_tokens", "label": "输出 Tokens"},
            {"key": "total_tokens", "label": "合计 Tokens"},
            {"key": "cached_tokens", "label": "缓存命中 Tokens"},
            {"key": "cache_hit_pct", "label": "命中率 (%)"},
        ],
        "rows": rows,
        "total": len(rows),
        # 图表形状（chart widget series 消费；与 rows 同源同值）
        "labels": [r["date"] for r in rows],
        "datasets": [
            {"label": "输入 Tokens", "data": [r["input_tokens"] for r in rows]},
            {"label": "输出 Tokens", "data": [r["output_tokens"] for r in rows]},
            {"label": "缓存命中 Tokens", "data": [r["cached_tokens"] for r in rows]},
        ],
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
    """按 path 分发到 monitoring 业务端点。

    数据 widget 端点（token-usage / token-usage/by-time / plugins）响应体即
    widget 消费形（顶层 rows/columns/labels/datasets），前端取数链不认具名
    信封；供 status_card valueKey 点号取值的端点保留单层域键
    （metrics / statistics / cache_stats / items+total）。
    """
    try:
        if path == "/ext/monitoring/system/metrics" and method == "GET":
            # psutil.cpu_percent(interval=0.1) 同步阻塞 ~100ms，必须落工作线程
            # （X26-F1）：直调会卡住 sidecar 事件循环，拖慢同循环其它端点
            return _ok(_json_response({"metrics": await asyncio.to_thread(_collect_system_metrics)}))

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
            # 租户收窄在 db-read 契约内建（身份经 Authorization 复权）：缓存键
            # 必须含租户，跨租户共享同键即串数据
            tenant = _header_value(headers, "x-agentos-tenant")
            auth = _authorization(headers)
            return _ok(_json_response(await _cached_aggregation(
                f"token-usage:{tenant}",
                lambda: _collect_token_usage(tenant, auth),
            )))

        if path == "/ext/monitoring/token-usage/by-time" and method == "GET":
            # 时间参数接线（P-146）：days/since/until 裁剪 traces 扫描范围
            # （聚合函数签名本就支持）；非法值走协议化 400 信封（protocol_error
            # 契约），不是 success=False 桥包 5xx。缓存键含归一化参数维度：
            # 不同时间窗各持条目，杜绝串数据。
            tenant = _header_value(headers, "x-agentos-tenant")
            auth = _authorization(headers)
            q = query or {}
            raw_days = str(q.get("days") or "").strip()
            days = 0
            if raw_days:
                try:
                    days = int(raw_days)
                except ValueError:
                    return _protocol_error(f"days 必须为 1-{_BY_TIME_MAX_DAYS} 的整数", 400)
                if not 1 <= days <= _BY_TIME_MAX_DAYS:
                    return _protocol_error(f"days 超出允许范围（1-{_BY_TIME_MAX_DAYS}）", 400)
            since = str(q.get("since") or "").strip()
            until = str(q.get("until") or "").strip()
            for name, value in (("since", since), ("until", until)):
                if value:
                    try:
                        # 严格 YYYY-MM-DD（聚合 SQL 按日期前缀文本比较，宽格式
                        # 如 20260901 会静默错配区间，必须在入口挡掉）
                        datetime.datetime.strptime(value, "%Y-%m-%d")
                    except ValueError:
                        return _protocol_error(f"{name} 必须为 ISO 日期（YYYY-MM-DD）", 400)
            return _ok(_json_response(await _cached_aggregation(
                f"token-usage/by-time:{tenant}:{days}:{since}:{until}",
                lambda: _collect_token_usage_by_time(
                    tenant, auth, days=days, since=since, until=until
                ),
            )))

        if path == "/ext/monitoring/cache-stats" and method == "GET":
            return _ok(_json_response({"cache_stats": _collect_cache_stats()}))

        # ── 插件组健康告警（卡5/卡9 四条规则）：last_crash_ts 跳变阈值（respawn
        # 恢复失败/组启动失败）+ 内核计数器（EOF 速率/tools-list 非法）；身份经
        # Authorization 透传 metrics-admin 读面复权（后台轮询走插件语境系统读，
        # 见 _plugin_health_loop），命中告警按冷却闸打「插件组健康告警 [LEVEL]」日志 ──
        if path == "/ext/monitoring/plugin-health" and method == "GET":
            return _ok(_json_response(await _collect_plugin_health(_authorization(headers))))

        # ── T4：Payload 诊断快照（列目录 + 读单文件）──
        # 数据源：logs/payload_diag/ 下 adapter.py 写的 {ts}_{model}_{hash}_{n}msg.json
        if path == "/ext/monitoring/payload-diag" and method == "GET":
            _diag_items = _list_payload_diag()  # 单次扫描：total 与 items 同源同快照
            return _ok(_json_response({"items": _diag_items, "total": len(_diag_items)}))

        if path == "/ext/monitoring/payload-diag/file" and method == "GET":
            q = query or {}
            name = q.get("name", "")
            return _ok(_json_response(_read_payload_diag(name)))

        # ── 诊断开关（用户空间 settings.yaml diagnostics 段读写；写后热生效
        # ——llm adapter 侧 mtime 缓存，下一次 LLM 调用即按新值，无需重启）──
        if path == "/ext/monitoring/diagnostics-settings" and method == "GET":
            from agentos_plugin_sdk.diagnostics_settings import read_diagnostics  # noqa: PLC0415

            return _ok(_json_response({"diagnostics": read_diagnostics()}))

        if path == "/ext/monitoring/diagnostics-settings" and method == "PUT":
            from agentos_plugin_sdk.diagnostics_settings import write_diagnostics  # noqa: PLC0415

            # decode_body：raw_body 为 base64 或明文 JSON 双形态（内核转发契约）
            try:
                partial = _decode_body(raw_body, strict_object=True)
            except ValueError:
                return _ok(_json_response({"error": "body must be a JSON object"}, 400))
            try:
                merged = write_diagnostics(partial)
            except ValueError as exc:
                return _ok(_json_response({"error": str(exc)}, 400))
            return _ok(_json_response({"diagnostics": merged}))

        # ── T5：工具调用记录（json_each 解包 traces.patch_data.tool_results）──
        # 租户过滤：内核对已认证 /ext 分发注入 X-AgentOS-Tenant，插件按该头
        # 过滤 traces（信任锚=内核验签身份，头不可由客户端伪造）。
        if path == "/ext/monitoring/tool-calls" and method == "GET":
            q = query or {}
            tenant = _header_value(headers, "x-agentos-tenant")
            return _ok(_json_response(await _query_tool_calls(q, tenant, _authorization(headers))))

        # 按工具聚合统计（窗口 = 最近 limit 条 trace 内的工具结果，非全量）
        if path == "/ext/monitoring/tool-calls/stats" and method == "GET":
            q = query or {}
            tenant = _header_value(headers, "x-agentos-tenant")
            return _ok(_json_response(await _query_tool_call_stats(q, tenant, _authorization(headers))))

        # ── 管道诊断域：step 级 trace 时间线 + pipeline_state 全字段 ──
        # （数据经 kernel_reads 能力桥，投影在 pipeline_diagnostics）
        if path == "/ext/monitoring/traces" and method == "GET":
            import pipeline_diagnostics  # noqa: PLC0415

            q = query or {}
            if not q.get("pipeline_id"):
                # 协议化 400 信封（protocol_error 契约）：_error 的 success=False
                # 会被内核桥成 5xx（P-160 实证），参数错误必须以 400 透传前端
                return _protocol_error("缺少 pipeline_id 参数", 400)
            try:
                limit = int(q.get("limit", 200))
            except (TypeError, ValueError):
                limit = 200
            return _ok(_json_response(await pipeline_diagnostics.list_pipeline_traces(
                pipeline_id=q["pipeline_id"], limit=limit
            )))

        if path == "/ext/monitoring/pipeline-state" and method == "GET":
            import pipeline_diagnostics  # noqa: PLC0415

            q = query or {}
            if not q.get("pipeline_id"):
                return _protocol_error("缺少 pipeline_id 参数", 400)
            return _ok(_json_response(await pipeline_diagnostics.get_pipeline_state_full(q["pipeline_id"])))

        # ── 孤儿管道观测：running 且时长超阈值的候选清单（人工 cancel 决策用）──
        if path == "/ext/monitoring/orphans" and method == "GET":
            import execution_records as er  # noqa: PLC0415

            q = query or {}
            try:
                min_minutes = int(q.get("min_minutes", 10))
            except (TypeError, ValueError):
                min_minutes = 10
            try:
                limit = int(q.get("limit", 50))
            except (TypeError, ValueError):
                limit = 50
            return _ok(_json_response(await er.list_orphan_runs(
                min_minutes=min_minutes, limit=limit
            )))

        # ── 插件运行态：metrics-admin 读面桥（kernel_reads.plugin_runtime）──
        if path == "/ext/monitoring/plugins" and method == "GET":
            return _ok(_json_response(await kernel_reads.plugin_runtime(authorization=_authorization(headers))))

        # ── T5 webview 页面 HTML ──
        if path == "/ext/monitoring/page/tool-calls" and method == "GET":
            return _ok(_html_response(_TOOL_CALLS_HTML))

        # ── execution/records 域：数据 = 内核只读能力桥（kernel_reads：
        # pipeline-runs.list/messages.list/pipeline-state.list），能力不可用
        # 降级空结构（HTTP 200 空载荷）。
        if path.startswith("/ext/monitoring/execution"):
            return await _handle_execution_domain(path, method, raw_body, query or {}, headers or {})

        # ── sessions token-usage 域 ──
        if path.startswith("/ext/monitoring/sessions"):
            return await _handle_sessions_domain(path, method, raw_body, query or {}, headers or {})

        # ── search 域：pipeline-state/messages 全局搜索 ──
        if path.startswith("/ext/monitoring/search"):
            return await _handle_search_domain(path, method, raw_body, query or {}, headers or {})

        logger.warning("http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:
        logger.exception("monitoring http.handle failed: %s", exc)
        return _error(f"monitoring service error: {exc}", 500)


# ════════════════════════════════════════════════════════════════════════════
# execution/records + sessions + search 域分发——路径语义与
# /ext/channel_api/** 原值逐项对齐，数据统一经 kernel_reads 能力桥
# （provider 由 _on_load 注入）。
# ════════════════════════════════════════════════════════════════════════════


def _qint(query: dict[str, str], key: str, default: int) -> int:
    """读取 query 整型参数；缺失/非法回退 default。"""
    try:
        return int(query[key]) if key in query else default
    except (TypeError, ValueError):
        return default


def _authorization(headers: dict[str, str] | None) -> str:
    """取入站请求的原始 Authorization 头值（内核 header key 已小写，大小写防御）。

    与 plugins/shared/db_admin/server.py 同一约定：插件只透传凭证，角色校验
    在内核 db-admin handler 执行（信任锚点，插件无法伪造角色）。
    """
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "authorization" and v:
            return v
    return ""


def _header_value(headers: dict[str, str] | None, name: str) -> str:
    """取内核注入的身份头值（x-agentos-tenant 等；内核 header key 已小写）。"""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == name and v:
            return v
    return ""


async def _handle_execution_domain(
    path: str, method: str, raw_body: str, query: dict[str, str], headers: dict[str, str]
) -> dict[str, Any]:
    """execution 域分发：/ext/monitoring/execution/** → execution_records 业务函数。

    仅迁前端实际消费的 /records* 子集（前端 executionRecords.ts / 调试中心），
    与 channel_api 原分发逐项同构（含 404/500 语义）。
    """
    import execution_records as er  # noqa: PLC0415
    import kernel_reads  # noqa: PLC0415

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
        # POST /records/clear-all（全量清理：内核 9 表 + registry + payload_diag 快照文件）
        if sub == "/records/clear-all" and method == "POST":
            result = await er.clear_all_records(authorization=_authorization(headers))
            result["payload_files_deleted"] = _clear_payload_diag_files()
            return _ok(_json_response(result))

        logger.warning("execution http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except kernel_reads.ClearExecutionDataError as exc:
        # 写面错误透传：409（运行中管道）/403（非 admin）/502/503/500 原样上抛
        return _ok(_json_response({"error": "clear failed", "detail": str(exc)}, exc.status))
    except Exception as exc:  # noqa: BLE001
        logger.error("execution http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_sessions_domain(
    path: str, method: str, raw_body: str, query: dict[str, str],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """sessions 域分发：/ext/monitoring/sessions/** → token-usage 业务函数。

    租户过滤（M3）：目标管道存在于 runs 表但归属他租户 → 404（不泄露存在性）；
    未知管道（runs 无行，无数据可泄露）放行，业务函数自然返回空用量。
    """
    import execution_records as er  # noqa: PLC0415

    prefix = "/ext/monitoring/sessions"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a sessions path", "path": path}, 404))
    sub = path[len(prefix):]  # "/{session_id}/total-token-usage" 等

    tenant = _header_value(headers, "x-agentos-tenant")
    if not tenant:
        return _ok(_json_response({"error": "missing tenant identity"}, 403))

    async def _owned(session_id: str) -> bool:
        return not await _pipeline_tenant_conflict(session_id, tenant, _authorization(headers))

    try:
        if sub.endswith("/total-token-usage") and method == "GET":
            session_id = sub[1:].rsplit("/total-token-usage", 1)[0]
            if not await _owned(session_id):
                return _ok(_json_response({"error": "not found", "path": path}, 404))
            return _ok(_json_response(await er.get_session_total_token_usage(session_id)))
        if sub.endswith("/context-token-usage") and method == "GET":
            session_id = sub[1:].rsplit("/context-token-usage", 1)[0]
            if not await _owned(session_id):
                return _ok(_json_response({"error": "not found", "path": path}, 404))
            parent = query.get("parent_execution_record_id")
            return _ok(_json_response(
                await er.get_session_context_token_usage(session_id, parent_execution_record_id=parent)
            ))

        logger.warning("sessions http.handle: no route for sub=%s method=%s", sub, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        logger.error("sessions http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


async def _handle_search_domain(
    path: str, method: str, raw_body: str, query: dict[str, str],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """search 域分发：/ext/monitoring/search → 内核搜索业务函数（会话标题+消息内容）。

    GET /search?q=xxx&type=all|session|message&limit=20 统一搜索。
    响应形态对齐前端 services/api/search.ts（SearchResponse）。type 非法 → 422
    （语义同原 APIError VAL_ENUM_7002）。租户过滤见 routes_search.search（M3）。
    """
    import routes_search as rsearch  # noqa: PLC0415

    prefix = "/ext/monitoring/search"
    if not path.startswith(prefix):
        return _ok(_json_response({"error": "not a search path", "path": path}, 404))
    sub = path[len(prefix):]  # "" / "/"

    try:
        if sub in ("", "/") and method == "GET":
            tenant = _header_value(headers, "x-agentos-tenant")
            return _ok(_json_response(await rsearch.search(
                q=query.get("q", ""),
                type=query.get("type", "all"),
                limit=_qint(query, "limit", 20),
                tenant_id=tenant,
                authorization=_authorization(headers),
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


def _payload_diag_dir() -> str:
    """返回 payload_diag 目录路径（SDK payload_diag_dir 单源，与 adapter.py
    写入端同一函数：AGENTOS_LOG_DIR 部署覆盖优先，否则用户空间
    logs/payload_diag——插件位置推导已退役，装机布局下会与写端分叉，
    ADR 2026-11-02-payload-diag-user-space-anchor）。"""
    return str(payload_diag_dir())


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


def _clear_payload_diag_files() -> int:
    """清空 payload_diag 快照目录（*.json，与 _list_payload_diag 同范围）。

    配套 /records/clear-all：LLM 请求轨迹与执行记录同属"清理所有记录与轨迹"
    语义。目录不存在/单文件删除失败不崩（容错计数返回）。
    """
    import glob

    deleted = 0
    for fpath in glob.glob(os.path.join(_payload_diag_dir(), "*.json")):
        try:
            os.remove(fpath)
            deleted += 1
        except OSError:
            logger.warning("[monitoring] payload_diag 文件删除失败: %s", fpath)
    return deleted


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


async def _pipeline_tenant_conflict(
    pipeline_id: str, tenant_id: str, authorization: str = ""
) -> bool:
    """True = 管道锚行归属他租户（跨租户访问拒绝的判据，db-read 契约面）。

    数据面 = pipeline_state tenant 档（身份经 authorization 内核复权后内建
    收窄）：调用方视角只见本租户锚行，行内 tenant_id 与请求租户比对为
    belt-and-braces（契约收窄下恒等，保持比对以兼容未来档位演进）。锚行
    不可见（他租户 / 未知管道——"会话已建但管道尚未跑过"同待遇）返回
    False 放行，业务层（内核租户化读面）自然返回空形态，两侧均无数据可泄露。
    （runs 表退役，ADR 2026-09-18：管道存在性锚 = pipeline_state 行。）
    """
    if not pipeline_id or not tenant_id:
        return False
    try:
        rows = await kernel_reads.db_read_query(
            face="pipeline_state",
            sql="SELECT tenant_id FROM pipeline_state"
            " WHERE pipeline_id = :pid LIMIT 1",
            params={"pid": pipeline_id},
            authorization=authorization,
        )
    except kernel_reads.DbReadQueryError:
        return False
    return bool(rows) and str(rows[0][0]) != tenant_id


async def _query_tool_calls(
    q: dict[str, str], tenant_id: str = "", authorization: str = ""
) -> dict[str, Any]:
    """从 traces 表查询工具调用记录（db-read 契约面，租户收窄内建 + fail-closed）。

    数据源 = 引擎 persist_step_trace 落的 step 级轨迹：traces.plugin_id 是**配置
    step id**（如 core/prepare），工具结果数组由 tool_core 的 state_updates 合并
    进 state 后经引擎 diff 落在 patch_data.tool_results（行级按 json_each 解包）。
    故明细窗口按 created_at 取，与 plugin_id 无关——按 'pipeline_tool_core'
    过滤恒空（插件 id 不落 traces.plugin_id）。

    明细窗口（诚实口径）：**最近 limit 条 trace**（默认 50 / 上限 200，limit
    同时钳窗口与返回行数）内展开的工具结果，行数 ≤ limit。语义非"全历史最近
    N 条工具结果"——近期 trace 若无工具结果，更早的工具调用不会回补进窗口
    （内容谓词 json_extract 无索引可用，全表逐行解析大 blob 随库增长无界，
    实测 1.5GB/2 万行 8.2s，端点必超时——BUG-47；窗口化后 O(window)）。
    内核侧 (tenant_id, created_at) 索引（idx_traces_tenant_created）使窗口
    子查询走索引倒序扫描；窗口子查询只取 trace_id，避免大 blob 进排序器，
    无索引时也保持在超时预算内。

    租户过滤：``tenant_id`` 必须来自内核注入的 X-AgentOS-Tenant 头（已认证
    租户）；缺失/为空一律返回空集（fail-closed）——绝不全量查询兜底，否则
    匿名化身份缺失会退化为跨租户泄露。数据面 = traces tenant 档（db-read
    契约，身份经 authorization 内核复权后包装收窄）；窗口子查询与外层行集
    双重绑定租户（行级过滤不依赖窗口派生正确性；内层谓词值由调用方自持，
    契约包装仍兜底强制）。

    用 json_each 解包窗口内 patch_data.tool_results 数组，支持按 tool_name/
    status/min_duration 筛选（筛选作用于窗口内，不扩窗）。不改 schema，纯
    查询层。已知粒度局限：patch_data 是 step 末态 diff，同 step 多轮工具迭代
    只留最后一轮结果（trace 以配置 step 为界）。

    Args:
        q: 查询参数（tool_name / status / min_duration / limit）
        tenant_id: 内核注入的已认证租户（空 = 身份缺失，返回空集）

    Returns:
        含 items + total 的字典
    """
    if not tenant_id:
        return {"items": [], "total": 0, "error": "missing tenant identity"}

    limit = min(_qint(q, "limit", 50), 200)
    tool_name_filter = q.get("tool_name", "").strip()
    status_filter = q.get("status", "").strip()  # success / error
    min_duration = q.get("min_duration", "").strip()

    # 收窄查询参数须为命名 dict（契约注入 :__tenant__ 与调用方占位共存）：
    # 位置占位改命名绑定；外层投影尾列 tenant_id 供契约收窄引用真实租户列
    sql = """
        SELECT t.trace_id, t.pipeline_id, t.created_at,
               json_extract(item.value, '$.tool_name')   AS tool_name,
               json_extract(item.value, '$.success')     AS success,
               json_extract(item.value, '$.error')       AS error,
               json_extract(item.value, '$.duration_ms') AS duration_ms,
               t.tenant_id
        FROM (
            SELECT trace_id FROM traces
            WHERE tenant_id = :t
            ORDER BY created_at DESC LIMIT :limit
        ) w
        JOIN traces t ON t.trace_id = w.trace_id,
        json_each(t.patch_data, '$.tool_results') AS item
        WHERE t.tenant_id = :t
    """
    params: dict[str, Any] = {"t": tenant_id, "limit": limit}
    if tool_name_filter:
        sql += " AND json_extract(item.value, '$.tool_name') = :tool_name"
        params["tool_name"] = tool_name_filter
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
            sql += " AND CAST(json_extract(item.value, '$.duration_ms') AS REAL) >= :min_duration"
            params["min_duration"] = min_duration_f
    sql += " ORDER BY t.created_at DESC LIMIT :limit"

    try:
        db_rows = await kernel_reads.db_read_query(
            face="traces", sql=sql, params=params, authorization=authorization
        )
    except kernel_reads.DbReadQueryError as exc:
        return {"items": [], "total": 0, "error": str(exc)}

    items = [
        {
            "trace_id": r[0],
            "pipeline_id": r[1],
            "created_at": r[2],
            "tool_name": r[3],
            "success": r[4],
            "error": r[5],
            "duration_ms": r[6],
        }
        for r in db_rows
    ]
    return {"items": items, "total": len(items)}


async def _query_tool_call_stats(
    q: dict[str, str], tenant_id: str = "", authorization: str = ""
) -> dict[str, Any]:
    """按工具聚合调用统计（工具调用记录页「按工具统计」视图的数据源）。

    数据源与租户过滤同 _query_tool_calls（json_each 解包 patch_data.tool_results，
    缺租户身份 fail-closed 空集；db-read 契约 traces tenant 档收窄内建）。
    聚合窗口 = **最近 window 条 trace**（含未携工具结果的行——占窗口位不产生
    计数；先按 created_at 倒序取窗口再展开聚合）——诚实口径：非全生命周期
    总量，窗口大小随 limit 参数（默认 500 条 trace，上限 2000）。窗口子查询
    只取 trace_id（大 blob 不进排序器），配合内核 idx_traces_tenant_created
    走索引倒序扫描；内容谓词 json_extract 无索引可用，全表逐行解析随库增长
    无界（实测 1.5GB/2 万行 8.5s，端点必超时——BUG-47）。

    聚合形状纪律：无 GROUP BY 租户列的裸聚合把全租户聚成一行（租户列成任意
    值），契约包装无法正确收窄——GROUP BY tenant_id + tool_name 后交包装
    （71dca0fff 实证教训）；投影尾列 tenant_id 供收窄引用真实列，items 装配
    时剔除（响应形状与迁移前逐键一致）。

    Returns:
        {items: [{tool_name, calls, success_calls, error_calls, avg_duration_ms,
        last_call_at}], total, window}；按调用次数倒序。
    """
    if not tenant_id:
        return {"items": [], "total": 0, "window": 0, "error": "missing tenant identity"}

    window = min(_qint(q, "limit", 500), 2000)

    sql = """
        SELECT json_extract(item.value, '$.tool_name')  AS tool_name,
               COUNT(*) AS calls,
               SUM(CASE WHEN json_extract(item.value, '$.success') = 1 THEN 1 ELSE 0 END) AS success_calls,
               SUM(CASE WHEN json_extract(item.value, '$.success') = 0 THEN 1 ELSE 0 END) AS error_calls,
               AVG(CAST(json_extract(item.value, '$.duration_ms') AS REAL)) AS avg_duration_ms,
               MAX(t.created_at) AS last_call_at,
               t.tenant_id
        FROM (
            SELECT trace_id FROM traces
            WHERE tenant_id = :t
            ORDER BY created_at DESC LIMIT :window
        ) w
        JOIN traces t ON t.trace_id = w.trace_id,
        json_each(t.patch_data, '$.tool_results') AS item
        GROUP BY t.tenant_id, tool_name
        ORDER BY calls DESC
    """
    params: dict[str, Any] = {"t": tenant_id, "window": window}

    try:
        db_rows = await kernel_reads.db_read_query(
            face="traces", sql=sql, params=params, authorization=authorization
        )
    except kernel_reads.DbReadQueryError as exc:
        return {"items": [], "total": 0, "window": window, "error": str(exc)}

    items = [
        {
            "tool_name": r[0],
            "calls": r[1],
            "success_calls": r[2],
            "error_calls": r[3],
            "avg_duration_ms": r[4],
            "last_call_at": r[5],
        }
        for r in db_rows
    ]
    return {"items": items, "total": len(items), "window": window}


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
  button.active { background: #2563eb; color: #fff; }
  table { width: 100%; border-collapse: collapse; background: #fff; }
  th { text-align: left; padding: 8px; background: #f1f5f9; color: #475569; font-size: 11px; text-transform: uppercase; position: sticky; top: 0; }
  td { padding: 6px 8px; border-bottom: 1px solid #e2e8f0; vertical-align: top; }
  tr.rowmain { cursor: pointer; }
  tr.rowmain:hover { background: #f8fafc; }
  tr.rowdetail td { background: #f8fafc; padding: 8px 12px 12px 28px; }
  .ok { color: #16a34a; }
  .fail { color: #dc2626; font-weight: 600; }
  .slow { color: #d97706; font-weight: 600; }
  .empty { color: #94a3b8; text-align: center; padding: 40px; }
  #status { color: #94a3b8; font-size: 11px; }
  pre { white-space: pre-wrap; word-break: break-all; margin: 4px 0 0; font-family: Consolas, monospace; font-size: 12px; }
  .sub { color: #64748b; font-size: 11px; }
</style></head><body>
<h2>工具调用记录（从 traces 表查询）</h2>
<div class="toolbar">
  <button id="tab_stats" class="active" onclick="switchView('stats')">按工具统计</button>
  <button id="tab_list" onclick="switchView('list')">调用明细</button>
  <span id="count" style="color:#64748b"></span>
  <span id="status"></span>
</div>

<div id="view_stats">
  <div class="toolbar">
    <button onclick="loadStats()">刷新</button>
    <span class="sub" id="stats_window"></span>
  </div>
  <div style="overflow:auto; max-height:calc(100vh - 170px)">
    <table>
      <thead><tr><th>工具</th><th>调用次数</th><th>成功</th><th>失败</th><th>平均耗时</th><th>最近调用</th></tr></thead>
      <tbody id="stats_rows"></tbody>
    </table>
    <div id="stats_empty" class="empty">点击刷新加载数据</div>
  </div>
</div>

<div id="view_list" style="display:none">
  <div class="toolbar">
    <input id="f_tool" placeholder="工具名(精确)" style="width:140px">
    <select id="f_status">
      <option value="">全部状态</option>
      <option value="success">成功</option>
      <option value="error">失败</option>
    </select>
    <input id="f_dur" placeholder="最小耗时(ms)" style="width:120px" type="number">
    <button onclick="query()">查询</button>
  </div>
  <div style="overflow:auto; max-height:calc(100vh - 200px)">
    <table>
      <thead><tr><th>时间</th><th>工具</th><th>状态</th><th>耗时</th><th>管道</th><th>错误</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
    <div id="empty" class="empty">点击查询加载数据</div>
  </div>
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
function fmt(s) {
  if (!s) return '';
  try { return new Date(s).toLocaleString('zh-CN', {hour12: false}); } catch(e) { return s; }
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
function durCell(ms) {
  var dur = parseFloat(ms || 0);
  return '<td class="' + (dur > 1000 ? 'slow' : '') + '">' + dur.toFixed(0) + 'ms</td>';
}

// ── 视图切换 ──
function switchView(v) {
  document.getElementById('view_stats').style.display = v === 'stats' ? '' : 'none';
  document.getElementById('view_list').style.display = v === 'list' ? '' : 'none';
  document.getElementById('tab_stats').className = v === 'stats' ? 'active' : '';
  document.getElementById('tab_list').className = v === 'list' ? 'active' : '';
}

// ── 按工具统计视图 ──
var statsLoaded = false;
var expandedTools = {};
async function loadStats() {
  setStatus('查询中...');
  try {
    var res = await agentosFetch('/ext/monitoring/tool-calls/stats');
    var data = unwrap(res);
    if (data.error) {
      document.getElementById('stats_empty').textContent = '错误: ' + data.error;
      document.getElementById('stats_empty').style.display = 'block';
      document.getElementById('stats_rows').innerHTML = '';
      setStatus('查询错误');
      return;
    }
    var items = data.items || [];
    document.getElementById('count').textContent = '(' + items.length + ' 个工具)';
    document.getElementById('stats_window').textContent =
      '统计窗口：最近 ' + (data.window || 500) + ' 条 trace 内的工具调用（非全生命周期累计）';
    if (!items.length) {
      document.getElementById('stats_empty').style.display = 'block';
      document.getElementById('stats_rows').innerHTML = '';
      setStatus('无数据');
      return;
    }
    document.getElementById('stats_empty').style.display = 'none';
    expandedTools = {};
    var html = items.map(function(i) {
      var name = i.tool_name || '?';
      var err = parseInt(i.error_calls, 10) || 0;
      // 交互口径：只有失败数据可展开看详情；非失败行仅监控指标，不可展开
      var expandable = err > 0;
      return '<tr' + (expandable ? ' class="rowmain"' : '') + ' data-tool="' + escapeHtml(name) + '" id="row-' + escapeHtml(name) + '">' +
        '<td>' + escapeHtml(name) + '</td>' +
        '<td>' + (i.calls || 0) + '</td>' +
        '<td class="ok">' + (i.success_calls || 0) + '</td>' +
        '<td class="' + (err > 0 ? 'fail' : '') + '">' + err + '</td>' +
        durCell(i.avg_duration_ms) +
        '<td style="color:#64748b;font-size:11px">' + fmt(i.last_call_at) + '</td></tr>' +
        (expandable
          ? '<tr class="rowdetail" id="detail-' + escapeHtml(name) + '" style="display:none"><td colspan="6">加载中...</td></tr>'
          : '');
    }).join('');
    var tbody = document.getElementById('stats_rows');
    tbody.innerHTML = html;
    tbody.querySelectorAll('tr.rowmain').forEach(function(el) {
      el.addEventListener('click', function() { toggleToolDetail(el.getAttribute('data-tool')); });
    });
    statsLoaded = true;
    setStatus('');
  } catch(e) {
    document.getElementById('stats_empty').textContent = '查询失败: ' + escapeHtml(String(e));
    document.getElementById('stats_empty').style.display = 'block';
    setStatus('查询失败');
  }
}
// 展开/收起某工具的错误明细（懒加载：首次展开时拉该工具最近失败记录）
async function toggleToolDetail(name) {
  var detailRow = document.getElementById('detail-' + name);
  var mainRow = document.getElementById('row-' + name);
  if (!detailRow) return;
  if (detailRow.style.display !== 'none') {
    detailRow.style.display = 'none';
    return;
  }
  detailRow.style.display = '';
  if (expandedTools[name]) return; // 已加载
  expandedTools[name] = true;
  detailRow.getElementsByTagName('td')[0].innerHTML = '<span class="sub">加载失败明细中...</span>';
  try {
    var res = await agentosFetch('/ext/monitoring/tool-calls?tool_name=' + encodeURIComponent(name) + '&status=error&limit=5');
    var data = unwrap(res);
    var items = data.items || [];
    if (data.error) {
      detailRow.getElementsByTagName('td')[0].innerHTML = '<span class="fail">加载失败: ' + escapeHtml(data.error) + '</span>';
      return;
    }
    if (!items.length) {
      detailRow.getElementsByTagName('td')[0].innerHTML = '<span class="sub">统计窗口内无失败记录</span>';
      return;
    }
    var html = '<div class="sub fail">最近失败 ' + items.length + ' 条（点行收起）</div>' + items.map(function(i) {
      return '<div style="margin-bottom:6px"><span class="sub">' + fmt(i.created_at) + ' · ' +
        (parseFloat(i.duration_ms || 0)).toFixed(0) + 'ms</span>' +
        '<pre class="fail">' + escapeHtml(i.error || '(无错误信息)') + '</pre></div>';
    }).join('');
    detailRow.getElementsByTagName('td')[0].innerHTML = html;
  } catch(e) {
    detailRow.getElementsByTagName('td')[0].innerHTML = '<span class="fail">加载失败: ' + escapeHtml(String(e)) + '</span>';
  }
}

// ── 调用明细视图 ──
async function query() {
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
    var tbody = document.getElementById('rows');
    tbody.innerHTML = items.map(function(i, idx) {
      var ok = i.success === 1 || i.success === true;
      var errText = i.error || '';
      // 错误全文放展开行，主行只显示单行摘要（点击行展开/收起）
      var brief = errText.length > 60 ? errText.slice(0, 60) + '…' : errText;
      return '<tr class="rowmain" data-idx="' + idx + '">' +
        '<td>' + fmt(i.created_at) + '</td><td>' + escapeHtml(i.tool_name || '?') + '</td>' +
        '<td class="' + (ok ? 'ok' : 'fail') + '">' + (ok ? '成功' : '失败') + '</td>' +
        durCell(i.duration_ms) +
        '<td style="color:#94a3b8;font-size:11px">' + escapeHtml((i.pipeline_id || '').slice(0,8)) + '</td>' +
        '<td style="font-size:11px" class="' + (ok ? '' : 'fail') + '">' + (ok ? '' : escapeHtml(brief)) + '</td></tr>' +
        '<tr class="rowdetail" style="display:none"><td colspan="6"><pre>' + escapeHtml(errText || '(无错误信息)') + '</pre></td></tr>';
    }).join('');
    tbody.querySelectorAll('tr.rowmain').forEach(function(el) {
      el.addEventListener('click', function() {
        var detail = el.nextElementSibling;
        if (detail) detail.style.display = detail.style.display === 'none' ? '' : 'none';
      });
    });
    setStatus('');
  } catch(e) {
    document.getElementById('empty').textContent = '查询失败: ' + escapeHtml(String(e));
    document.getElementById('empty').style.display = 'block';
    setStatus('查询失败');
  }
}
loadStats();
</script></body></html>"""


if __name__ == "__main__":
    plugin.run()
