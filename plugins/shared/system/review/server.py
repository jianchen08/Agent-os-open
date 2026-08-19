#!/usr/bin/env python3
"""复盘系统 MCP 服务端。

trigger_review → review_agent 子管道（B 路径）链路。

复盘不再做"metrics<0.5 拼字符串"模拟，而是通过 pipeline-executor 能力起
review_agent 子管道，由 LLM 做深度复盘。报告回写通过 store_report 内部方法
（由 memory 侧 / event-bus 完成事件触发）。

0.2 收尾（旧引擎 AdrEngineImpl 已清理）：
- 内核 pipeline-executor.start_run 占位能力随旧引擎移除——它只 create_run
  返回 run_id，无 execute_step/end_run 驱动，子管道从不真正执行。
- trigger_review 当前直接走本地降级（_local_degrade_report）；review_agent
  深度复盘待接入 chat.send_message → PipelineExecutor 路径（与任务执行同构）。
- get_report 的 get_run_status 惰性轮询能力保留（未来子管道接入后复用）。

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
    """注入 IMemoryBackend 实例（HindsightBackend）。

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

    0.2 收尾：旧引擎 start_run 占位能力已移除（子管道从不真正执行），
    trigger 直接本地降级产出基础报告；review_agent 深度复盘待接入
    chat.send_message → PipelineExecutor 路径（见模块头注释）。

    Returns:
        review_id + status:
        - completed (local_degrade): 本地降级产出基础报告
    """
    review_id = f"review_{uuid.uuid4().hex[:8]}"
    artifacts = artifacts or []
    metrics = metrics or {}

    # ── GAP-1：深度复盘经 chat.send_message 起 review_agent 管道 ──
    # 不再"启动即 completed（乐观，空 lessons）"：派发成功 → 报告 running，
    # get_report 轮询复盘管道状态（pipeline-state 聚合）真实完成才落 completed。
    # chat capability 缺席 / 派发失败 → local_degrade 兜底（保留既有降级语义）。
    try:
        chat = plugin.get_capability("chat")
    except KeyError:
        chat = None
    if chat is not None:
        params = {
            "create": True,
            "background": True,
            "message": (
                f"对任务 {task_id} 进行深度复盘。" + "\n" + f"任务摘要：{summary}"
                + ("\n" + "产物：" + ", ".join(artifacts) if artifacts else "")
                + ("\n" + f"指标：{json.dumps(metrics, ensure_ascii=False)}" if metrics else "")
                + "\n" + "请产出结构化复盘报告（总结 / 教训 lessons / 建议 recommendations）。"
            ),
            "user_id": "review_system",
            "state": {
                "task.id": task_id,
                "review.id": review_id,
                "review.summary": summary,
                "review.artifacts": artifacts,
                "review.metrics": metrics,
            },
            # 血缘：根形式（系统组件，诚实声明复盘来源——不伪造父/默认 session）
            "lineage": {"root": True, "origin": {"kind": "plugin", "source": "review"}},
        }
        try:
            resp = await chat.call("send_message", params)
            pipeline_id = (
                str(resp.get("pipeline_id") or "") if isinstance(resp, dict) else ""
            )
        except Exception as exc:  # noqa: BLE001 — 派发失败降级，不崩复盘入口
            logger.error(
                "[Review] 复盘管道派发失败 review_id=%s: %s", review_id, exc
            )
            pipeline_id = ""
        if pipeline_id:
            _reports[review_id] = {
                "review_id": review_id,
                "task_id": task_id,
                "summary": summary,
                "artifacts": artifacts,
                "metrics": metrics,
                "lessons": [],
                "recommendations": [],
                "status": "running",
                "mode": "pipeline",
                "pipeline_id": pipeline_id,
                "created_at": time.time(),
            }
            logger.info(
                "[Review] 复盘管道已创建 review_id=%s pipeline_id=%s",
                review_id,
                pipeline_id,
            )
            return {
                "review_id": review_id,
                "status": "running",
                "mode": "pipeline",
                "pipeline_id": pipeline_id,
            }

    report = _local_degrade_report(review_id, task_id, summary, artifacts, metrics)
    _reports[review_id] = report
    return {
        "review_id": review_id,
        "status": "completed",
        "mode": "local_degrade",
        "lessons_count": len(report["lessons"]),
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
    # 子管道进行中：先轮询复盘管道状态（GAP-1 chat.send_message 路径），
    # 再回退既有 run_id 轮询（F-REVIEW-2 路径）。真实完成才落 completed。
    if report.get("status") == "running":
        if report.get("pipeline_id"):
            await _maybe_finalize_on_pipeline_completion(report)
        else:
            await _maybe_finalize_on_run_completion(report)
    return report


async def _maybe_finalize_on_pipeline_completion(report: dict[str, Any]) -> None:
    """按复盘管道 state 聚合行终结报告状态（GAP-1 派发路径的轮询）。

    - 行 status=completed → 报告 completed（mode=pipeline，内容取 raw_result）
    - 行 status=failed → 报告 failed（诚实状态，不伪造完成）
    - running/查询失败/行缺失 → 保持 running（可重复轮询，不崩）
    """
    pipeline_id = report.get("pipeline_id") or ""
    try:
        handle = plugin.get_capability("pipeline-state")
        rows = await handle.call("list", {})
    except Exception as exc:  # noqa: BLE001 — 轮询失败保持现状
        logger.warning(
            "[Review] 复盘管道状态轮询失败 pipeline_id=%s: %s", pipeline_id, exc
        )
        return
    row = next(
        (r for r in rows if isinstance(r, dict) and r.get("pipeline_id") == pipeline_id),
        None,
    )
    if row is None:
        return
    # 聚合行无独立 "status" 键（此前读它恒 None → 报告永久卡 running，
    # 2026-08-19 e2e 实测）：终态判定 = task.status（任务域）或
    # ended/current_phase（执行域），两者任一信号到位即可。
    status = row.get("task.status") or row.get("status")
    ended = row.get("ended") is True or row.get("current_phase") == "exit"
    if status == "completed" or (ended and status != "failed"):
        report["status"] = "completed"
        report["mode"] = "pipeline"
        raw = row.get("raw_result")
        if isinstance(raw, str) and raw.strip():
            report["summary"] = raw
            report["lessons"] = [raw.strip()]
        logger.info(
            "[Review] 复盘管道完成 review_id=%s pipeline_id=%s",
            report.get("review_id"),
            pipeline_id,
        )
    elif status == "failed" or (ended and status == "failed"):
        report["status"] = "failed"
        logger.warning(
            "[Review] 复盘管道失败 review_id=%s pipeline_id=%s",
            report.get("review_id"),
            pipeline_id,
        )


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
