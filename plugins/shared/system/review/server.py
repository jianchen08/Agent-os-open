#!/usr/bin/env python3
"""复盘系统 MCP 服务端。

trigger_review → review_agent 子管道（B 路径）链路。

复盘不再做"metrics<0.5 拼字符串"模拟，而是通过 pipeline-executor 能力起
review_agent 子管道，由 LLM 做深度复盘。报告回写通过 store_report 内部方法
（由 memory 侧 / event-bus 完成事件触发）。

agent_id/source 命名沿用 src/memory/maintenance/service.py 约定：
- agent_id = "review_agent"（config/agents/system/review_agent.yaml）
- source = "tool_review"（触发来源溯源）

[来源: docs/tasks/task_10_system_plugins.md AC-09-4]
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("review_service")

# review_agent 配置 ID（与 config/agents/system/review_agent.yaml 对齐）
_REVIEW_AGENT_ID = "review_agent"
# 触发来源标记（与 src/memory/maintenance/service.py _try_launch_review_agent tags.source 对齐）
_REVIEW_SOURCE = "tool_review"

# 复盘报告存储：review_id -> report dict
# report 含 status: pending(子管道已起,报告未回写) / running(子管道进行中) / completed(报告已回写)
_reports: dict[str, dict[str, Any]] = {}
# review_id -> 子管道 run_id（供 get_report 查询状态/前端跳转）
_run_ids: dict[str, str] = {}


async def store_report(review_id: str, report: dict[str, Any]) -> None:
    """内部方法：回写 review_agent 子管道产出的报告。

    触发时机：review_agent 子管道跑完，通过 memory.store 工具或 event-bus 完成事件
    回调本方法（事件监听接线留 TODO）。当前由 get_report 按需查询 / 外部调用方直接
    注入。不暴露为 MCP 工具（内部 API）。

    Args:
        review_id: trigger 阶段分配的复盘 ID。
        report: review_agent 产出的完整报告（lessons/improvements/recommendations 等）。
    """
    existing = _reports.get(review_id, {})
    existing.update(report)
    existing["status"] = "completed"
    existing["updated_at"] = time.time()
    _reports[review_id] = existing
    logger.info(
        "[Review] 报告已回写 review_id=%s lessons=%d",
        review_id,
        len(existing.get("lessons", [])),
    )


async def _trigger_review_agent_subpipeline(
    review_id: str,
    task_id: str,
    summary: str,
    artifacts: list[str],
    metrics: dict[str, Any],
) -> str | None:
    """通过 pipeline-executor 能力起 review_agent 子管道。

    能力未注入（独立进程/降级运行）时返回 None，由调用方降级处理。

    Returns:
        子管道 run_id，能力不可用时返回 None。
    """
    try:
        pipeline = plugin.get_capability("pipeline-executor")
    except KeyError:
        logger.warning(
            "[Review] pipeline-executor 能力未注入，无法起 review_agent 子管道，降级本地分析"
        )
        return None

    # start_run 参数格式参考 kernel/crates/api/src/capability_router.rs 的
    # ("pipeline-executor","start_run") handler——它把整个 params 透传给
    # engine.start_run(&params)。params 即 run 配置。
    config = {
        "agent_id": _REVIEW_AGENT_ID,
        "input": {
            "task_id": task_id,
            "summary": summary,
            "artifacts": artifacts,
            "metrics": metrics,
            "review_id": review_id,
        },
        # 触发来源溯源（沿用现有 tags 命名，不自创字段）
        "tags": {
            "agent_id": _REVIEW_AGENT_ID,
            "source": _REVIEW_SOURCE,
            "parent_review_id": review_id,
        },
    }

    try:
        result = await pipeline.call("start_run", config)
    except Exception as exc:  # noqa: BLE001 — 内核/管道层错误统一降级，不崩 trigger
        logger.warning(
            "[Review] 起 review_agent 子管道失败 (review_id=%s): %s", review_id, exc
        )
        return None

    run_id: str | None = None
    if isinstance(result, dict):
        run_id = result.get("run_id")
    if not run_id:
        # review_agent 在 config/agents/ 不存在时 start_run 会失败——这是配置问题，
        # 不崩 trigger，日志告警即可。result 通常已含错误信息。
        logger.warning(
            "[Review] start_run 未返回 run_id (review_id=%s, result=%s)",
            review_id,
            result,
        )
        return None

    logger.info(
        "[Review] review_agent 子管道已启动 review_id=%s run_id=%s",
        review_id,
        run_id,
    )
    return run_id


def _local_degrade_report(
    review_id: str,
    task_id: str,
    summary: str,
    artifacts: list[str],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """能力不可用时的本地降级报告（非 LLM，仅基础结构化，保证不崩）。

    保留极简 metrics 分析仅为兜底，真正分析由 review_agent 做。
    """
    lessons: list[str] = []
    if metrics:
        for key, value in metrics.items():
            if isinstance(value, (int, float)) and value < 0.5:
                lessons.append(f"Metric '{key}' scored low ({value}): consider improvement")
    if not lessons:
        lessons.append("Local degrade mode: pipeline-executor unavailable, LLM review skipped")

    return {
        "review_id": review_id,
        "task_id": task_id,
        "summary": summary,
        "artifacts": artifacts,
        "metrics": metrics,
        "lessons": lessons,
        "recommendations": [
            "Re-run review when pipeline-executor capability is available for LLM analysis"
        ],
        "status": "completed",
        "mode": "local_degrade",
        "created_at": time.time(),
    }


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
    """Trigger a post-task review via review_agent sub-pipeline (B path).

    通过 pipeline-executor 能力起 review_agent 子管道做 LLM 深度复盘。
    能力未注入时降级为本地简单分析，绝不抛错。

    Returns:
        review_id + status:
        - running: 子管道已起，报告待回写（get_report 轮询）
        - completed (local_degrade): 能力不可用，已本地降级产出基础报告
    """
    review_id = f"review_{uuid.uuid4().hex[:8]}"
    artifacts = artifacts or []
    metrics = metrics or {}

    run_id = await _trigger_review_agent_subpipeline(
        review_id, task_id, summary, artifacts, metrics
    )

    if run_id is None:
        # 降级：能力不可用，本地生成基础报告
        report = _local_degrade_report(review_id, task_id, summary, artifacts, metrics)
        _reports[review_id] = report
        return {
            "review_id": review_id,
            "status": "completed",
            "mode": "local_degrade",
            "lessons_count": len(report["lessons"]),
        }

    # 子管道已起：先登记 pending 记录，报告待 store_report 回写
    _run_ids[review_id] = run_id
    _reports[review_id] = {
        "review_id": review_id,
        "task_id": task_id,
        "summary": summary,
        "artifacts": artifacts,
        "metrics": metrics,
        "status": "running",
        "run_id": run_id,
        "created_at": time.time(),
    }

    return {
        "review_id": review_id,
        "status": "running",
        "run_id": run_id,
        "message": "review_agent 子管道已启动，通过 review.get_report 查询报告",
    }


@plugin.tool(
    name="review.get_report",
    schema={
        "type": "object",
        "properties": {
            "review_id": {"type": "string"},
        },
        "required": ["review_id"],
    },
    description="Get review report by ID (returns status running if sub-pipeline in flight)",
)
async def get_report(review_id: str) -> dict[str, Any]:
    """Retrieve a stored review report.

    - 报告已回写（store_report 调用过）：返回完整报告，status=completed
    - 子管道运行中：返回 status=running + run_id，供调用方轮询
    - 未找到：返回 error
    """
    report = _reports.get(review_id)
    if report is None:
        return {"error": "review not found", "review_id": review_id}
    return report


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize review service on load."""
    logger.info("[Review] review_service loaded")


if __name__ == "__main__":
    plugin.run()
