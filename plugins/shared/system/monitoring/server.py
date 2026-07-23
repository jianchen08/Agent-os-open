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

[来源: docs/working/module_migration_plan.md §六 P2 迁移]
"""
from __future__ import annotations

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


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize performance monitor on load."""
    global _monitor
    from performance_monitor import PerformanceMonitor

    _monitor = PerformanceMonitor()
    await _monitor.start()
    logger.info("Monitoring service started")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Stop monitor and cleanup on unload."""
    global _monitor
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
