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
import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("monitoring_service")

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


if __name__ == "__main__":
    plugin.run()
