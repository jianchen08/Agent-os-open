#!/usr/bin/env python3
"""复盘系统 MCP 服务端。

trigger_review → review_agent 子管道（B 路径）链路。

复盘不再做"metrics<0.5 拼字符串"模拟，而是通过 pipeline-executor 能力起
review_agent 子管道，由 LLM 做深度复盘。报告回写通过 store_report 内部方法
（由 memory 侧 / event-bus 完成事件触发）。

真实完成语义（F-REVIEW-2）：
- 内核 start_run 是 fire-and-forget（只 create_run 返回 run_id，无完成事件/wait）。
- 本插件在 trigger_review 后记 running+run_id；get_report 时经
  pipeline-executor.get_run_status 能力（内核 capability_router 新增，查 runs 表）
  惰性轮询子管道真实状态：run 完成才落 status=completed，失败落 failed，
  进行中保持 running——不再"启动即 completed"。
- 报告正文（lessons 等）仍由 store_report 回写（事件接线为后续 P1，见审计）。

agent_id/source 命名沿用 src/memory/maintenance/service.py 约定：
- agent_id = "review_agent"（config/agents/system/review_agent.yaml）
- source = "tool_review"（触发来源溯源）

[来源: docs/tasks/task_10_system_plugins.md AC-09-4]
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

# hindsight_memory 插件目录（wiring.py 所在处）加入 sys.path
_HINDSIGHT_MEMORY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hindsight_memory"))
if _HINDSIGHT_MEMORY_DIR not in sys.path:
    sys.path.insert(0, _HINDSIGHT_MEMORY_DIR)

from wiring import build_memory_backend  # noqa: E402

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("review_service")

# review_agent 配置 ID（与 config/agents/system/review_agent.yaml 对齐）
_REVIEW_AGENT_ID = "review_agent"
# 触发来源标记（与 src/memory/maintenance/service.py _try_launch_review_agent tags.source 对齐）
_REVIEW_SOURCE = "tool_review"

# 复盘报告存储：review_id -> report dict
# report 含 status: pending(子管道已起,报告未回写) / running(子管道进行中) /
# completed(子管道真实完成,报告已落) / failed(子管道失败)
_reports: dict[str, dict[str, Any]] = {}
# review_id -> 子管道 run_id（供 get_report 查询状态/前端跳转）
_run_ids: dict[str, str] = {}

# 长期记忆后端（IMemoryBackend），用于把复盘报告持久化到 Hindsight，供跨会话检索/注入。
# 由插件宿主在加载时注入；未注入时 store_report 仅写内存 _reports（降级，不崩）。
_memory_backend: Any = None


def set_memory_backend(backend: Any) -> None:
    """注入 IMemoryBackend 实例（HindsightBackend / KernelMemoryBackend）。

    生产环境由 review 插件宿主把 hindsight_memory.get_memory_backend() 产出的
    后端注入进来；测试环境传 AsyncMock。传 None 可重置为未注入（仅内存路径）。
    """
    global _memory_backend
    _memory_backend = backend


async def store_report(review_id: str, report: dict[str, Any]) -> None:
    """内部方法：回写 review_agent 子管道产出的报告。

    触发时机：review_agent 子管道跑完，通过 memory.store 工具或 event-bus 完成事件
    回调本方法（事件监听接线留 TODO）。当前由 get_report 按需查询 / 外部调用方直接
    注入。不暴露为 MCP 工具（内部 API）。

    落库策略：
    - 内存：始终写 ``_reports[review_id]``，供 get_report 立即轮询。
    - 长期记忆：若 ``_memory_backend`` 已注入，调用 ``backend.add`` 把整份报告
      （JSON）落到 Hindsight，memory_type=``review``，tags 含 ``review_id:<id>`` 与
      ``review_report``，source=``review_agent``，供后续会话检索/注入。

    Args:
        review_id: trigger 阶段分配的复盘 ID。
        report: review_agent 产出的完整报告（lessons/improvements/recommendations 等）。
    """
    existing = _reports.get(review_id, {})
    existing.update(report)
    existing["review_id"] = review_id
    existing["status"] = "completed"
    existing["updated_at"] = time.time()
    _reports[review_id] = existing

    # 持久化到长期记忆后端（Hindsight）。未注入时降级，仅保留内存路径。
    if _memory_backend is not None:
        try:
            await _memory_backend.add(
                user_id=existing.get("task_id") or "review",
                content=json.dumps(existing, ensure_ascii=False),
                memory_type="review",
                tags=[f"review_id:{review_id}", "review_report"],
                source="review_agent",
            )
        except Exception as exc:  # noqa: BLE001 — 记忆后端失败不崩复盘回写
            logger.warning(
                "[Review] 报告落 Hindsight 失败 review_id=%s: %s", review_id, exc
            )

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
    name="trigger_review",
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
    description="Get review report by ID (polls sub-pipeline run status: completed only after the run truly finishes)",
)
async def get_report(review_id: str) -> dict[str, Any]:
    """Retrieve a stored review report.

    - 报告已回写（store_report 调用过）：返回完整报告，status=completed
    - 子管道进行中：经 pipeline-executor.get_run_status 惰性轮询 run 状态——
      run 真实完成才落 completed，失败落 failed，进行中保持 running
    - 未找到：返回 error
    """
    report = _reports.get(review_id)
    if report is None:
        return {"error": "review not found", "review_id": review_id}
    # 子管道进行中：查询内核 run 状态，真实完成才落 completed（F-REVIEW-2）
    if report.get("status") == "running":
        await _maybe_finalize_on_run_completion(report)
    return report


async def _query_run_status(run_id: str) -> str | None:
    """经 pipeline-executor.get_run_status 能力查询子管道 run 状态。

    能力未注入 / 调用失败 / run 不存在时返回 None——调用方保持现状，绝不崩。
    """
    try:
        pipeline = plugin.get_capability("pipeline-executor")
    except KeyError:
        logger.warning(
            "[Review] pipeline-executor 能力未注入，无法查询子管道状态 run_id=%s", run_id
        )
        return None
    try:
        result = await pipeline.call("get_run_status", {"run_id": run_id})
    except Exception as exc:  # noqa: BLE001 — 内核/管道层错误统一降级，不崩 get_report
        logger.warning("[Review] 查询子管道状态失败 run_id=%s: %s", run_id, exc)
        return None
    if isinstance(result, dict) and isinstance(result.get("status"), str):
        return result["status"]
    logger.warning(
        "[Review] get_run_status 返回异常 run_id=%s result=%s", run_id, result
    )
    return None


async def _maybe_finalize_on_run_completion(report: dict[str, Any]) -> None:
    """子管道真实完成时把 review 落为 completed（轮询语义，F-REVIEW-2）。

    在 get_report 调用时惰性轮询（最小可行，无后台任务）：
    - run 状态 completed → 落 report status=completed。报告正文（lessons 等）
      仍待 store_report 回写（事件接线为后续 P1）；状态语义先行——completed
      只由子管道真实完成触发，不再"启动即 completed（乐观，空 lessons）"。
    - run 状态 failed → report status=failed（不再无限 running）。
    - running/suspended/查询失败 → 保持 running（记录 run_status 供调用方）。
    """
    run_id = report.get("run_id")
    if not run_id:
        return
    status = await _query_run_status(run_id)
    if status == "completed":
        report["status"] = "completed"
        report["run_status"] = "completed"
        report["completed_at"] = time.time()
        logger.info(
            "[Review] 子管道真实完成，落 completed review_id=%s run_id=%s",
            report.get("review_id"),
            run_id,
        )
    elif status == "failed":
        report["status"] = "failed"
        report["run_status"] = "failed"
        report["failed_at"] = time.time()
        logger.info(
            "[Review] 子管道失败 review_id=%s run_id=%s",
            report.get("review_id"),
            run_id,
        )
    elif status:
        report["run_status"] = status


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize review service + 注入记忆后端。"""
    backend = build_memory_backend(plugin)
    if backend:
        set_memory_backend(backend)
        logger.info("[Review] 记忆后端已注入，复盘报告将持久化")
    else:
        logger.warning("[Review] 记忆后端未注入，复盘报告仅存内存（降级）")


if __name__ == "__main__":
    plugin.run()
