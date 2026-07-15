#!/usr/bin/env python3
"""复盘系统 MCP 服务端。

trigger_review → 经验报告链路。
核心业务逻辑参考 0.1 src/review/review_service.py。

[来源: docs/tasks/task_10_system_plugins.md AC-09-4]
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from lingxi_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("review_service")

# 复盘报告存储
_reports: dict[str, dict[str, Any]] = {}


@plugin.tool(
    name="review.trigger",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "summary": {"type": "string"},
            "artifacts": {"type": "array", "items": {"type": "string"}, "default": []},
            "metrics": {"type": "object", "default": {}},
        },
        "required": ["task_id", "summary"],
    },
    description="Trigger a review for a completed task and generate experience report",
)
async def trigger_review(
    task_id: str,
    summary: str,
    artifacts: list[str] | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trigger a post-task review.

    Analyzes task execution, identifies lessons learned, and generates
    an experience report for future reference.
    """
    review_id = f"review_{uuid.uuid4().hex[:8]}"

    # Simple review analysis (production: use LLM + knowledge extraction)
    lessons: list[str] = []
    if metrics:
        for key, value in metrics.items():
            if isinstance(value, (int, float)) and value < 0.5:
                lessons.append(f"Metric '{key}' scored low ({value}): consider improvement")

    if not lessons:
        lessons.append("All metrics within acceptable range")

    report = {
        "review_id": review_id,
        "task_id": task_id,
        "summary": summary,
        "artifacts": artifacts or [],
        "metrics": metrics or {},
        "lessons": lessons,
        "recommendations": [
            "Document successful patterns for reuse",
            "Flag low-scoring metrics for attention",
        ],
        "status": "completed",
        "created_at": time.time(),
    }

    _reports[review_id] = report

    # DEBT: 经验报告持久化到 memory_service 未实现。ceiling: 当前仅进程内存储。
    # upgrade: memory_service MCP 集成后通过 pipeline-executor 句柄存储。
    # memory_service = plugin.get_capability("pipeline-executor")
    # await memory_service.call("store_review", {"report": report})

    return {"review_id": review_id, "status": "completed", "lessons_count": len(lessons)}


@plugin.tool(
    name="review.get_report",
    schema={
        "type": "object",
        "properties": {
            "review_id": {"type": "string"},
        },
        "required": ["review_id"],
    },
    description="Get review report by ID",
)
async def get_report(review_id: str) -> dict[str, Any]:
    """Retrieve a stored review report."""
    report = _reports.get(review_id)
    if report is None:
        return {"error": "review not found", "review_id": review_id}
    return report


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize review service on load."""
    pass


if __name__ == "__main__":
    plugin.run()
