"""任务域事件派生（ADR 2026-08-28 事件下沉）。

内核 run 终态只广播运行域 run.* 事件；本模块订阅 run.completed / run.failed，
经 pipeline-state.list 读该管道最终 state，按任务域语义派生
task_completed / task_failed 并经 event-bus.emit_domain 发回域事件总线——
裁决词汇（task.status 值、task.* 键面）归任务域所有者（本插件），内核零知识。

判定语义（与内核原 derive_run_terminal_events 行为逐条对齐）：
- 任务管道判据 = state 含 ``task.`` 前缀键且不含 ``task.owned.``（后者是
  父管道登记子任务的键，不是任务自身声明）；
- run.failed + 任务管道 → task_failed；
- run.completed：suspended / router.stop_reason=user_requested → 不派生
  （挂起与用户停止都不是任务终态）；task.status=completed → task_completed；
  task.status=failed → task_failed；其余（pending/pending_evaluation 等）→
  不派生（杜绝"跑完就假完成通知上级"）。

事件标签：pipeline_id / thread_id / task_id / parent_pipeline_id / user_id /
title / error / retry_count / eval_summary / context_usage——triggers_ext 的
父任务通知注入器（_auto_notify_parent）依赖这组标签拼装 0.1 同款富通知
（标题/失败原因/重试计数/评估结论/上下文使用率）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TASK_PREFIX = "task."
_OWNED_PREFIX = "task.owned."


def _is_task_state(row: dict[str, Any]) -> bool:
    """任务管道判据：含 task.* 自身键且不含 task.owned.* 登记键。"""
    has_task = False
    for key in row:
        k = str(key)
        if k.startswith(_OWNED_PREFIX):
            continue
        if k.startswith(_TASK_PREFIX):
            has_task = True
            break
    return has_task


def _tag(row: dict[str, Any], key: str) -> Any:
    val = row.get(key)
    return val if val is not None else ""


def _context_usage(row: dict[str, Any]) -> dict[str, Any]:
    """从 state 摘要行提取上下文使用率（0.1 通知同款遥测）。

    数据源：track.llm_usage（track 插件跨轮累计，已出口）+ context_window
    （llm_core 每轮写入，已出口）。缺任一键返回空 dict（通知侧按无遥测处理）。
    """
    usage = row.get("track.llm_usage")
    window = row.get("context_window")
    if not isinstance(usage, dict) or not window:
        return {}
    try:
        window = int(window)
        input_tokens = int(usage.get("total_input_tokens", 0) or 0)
    except (TypeError, ValueError):
        return {}
    if window <= 0:
        return {}
    pct = round((input_tokens / window) * 100, 1)
    return {
        "pct": pct,
        "input_tokens": input_tokens,
        "context_window": window,
    }


def derive_task_terminal_events(
    event_name: str, row: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """由 run 终态事件 + state 摘要行派生任务域事件（纯函数，可单测）。

    Returns:
        [(event_name, tags)]——空列表 = 该 run 不派生任务域事件。
    """
    if not isinstance(row, dict) or not _is_task_state(row):
        return []
    tags = {
        "pipeline_id": _tag(row, "pipeline_id"),
        "thread_id": _tag(row, "thread_id"),
        "task_id": _tag(row, "task.id"),
        "parent_pipeline_id": _tag(row, "lineage.parent_pipeline_id"),
        "user_id": _tag(row, "task.submitted_by"),
        # 富通知字段（0.1 task_notifier 同款）：标题/失败原因/重试计数/
        # 评估结论/上下文使用率——state 已有键直接带出，缺键空串由通知侧兜底
        "title": _tag(row, "task.goal"),
        "error": _tag(row, "task.error"),
        "retry_count": _tag(row, "task.eval_total_calls"),
        "eval_summary": _tag(row, "task.eval_summary"),
        "context_usage": _context_usage(row),
    }
    if event_name == "run.failed":
        return [("task_failed", tags)]
    if event_name != "run.completed":
        return []
    status = str(row.get("task.status") or "")
    if status == "completed":
        return [("task_completed", tags)]
    if status == "failed":
        return [("task_failed", tags)]
    # 评估未通过（pending/pending_evaluation/running）不派生——完成唯一判据 =
    # task_evaluate 评估通过落 task.status=completed
    return []


def pending_registration_clear_fields(tags: dict[str, Any]) -> dict[str, Any] | None:
    """子任务终态 → 父管道挂号键清除字段（纯函数，可单测）。

    挂号键 = task_submit 写入提交者管道 state 的 ``task.subtasks_pending.<task_id>``
    （值 = 提交时间戳，消费方 task_reminder 信号③按真值判定）。终态清除写
    null——pipeline-state.update 无键删除语义，null 即已回执；task.* 前缀满足
    该写面的任务域键约束。无父锚点（根任务）或无任务 id → None（不写）。
    """
    task_id = str(tags.get("task_id") or "")
    parent = str(tags.get("parent_pipeline_id") or "")
    if not task_id or not parent:
        return None
    return {f"task.subtasks_pending.{task_id}": None}


async def handle_run_terminal_event(
    event_name: str, params: dict[str, Any], state_capability: Any, bus_capability: Any
) -> int:
    """入口：查 state 摘要 → 派生 → 经 event-bus.emit_domain 发回域总线。

    派生出的任务终态事件同时清除父管道的子任务挂号键（信号③闭环：父管道
    收束等待 → 子任务终态唤醒 + 挂号解除）；清除失败仅告警，不破坏事件派生。

    Args:
        event_name: run.completed / run.failed。
        params: 域事件标签（取 pipeline_id 定位管道）。
        state_capability: pipeline-state 能力句柄（list 取摘要行 / update 清挂号）。
        bus_capability: event-bus 能力句柄（call emit_domain）。

    Returns:
        派生发出的事件数（0 = 未派生）。
    """
    pipeline_id = str(params.get("pipeline_id") or "")
    if not pipeline_id:
        return 0
    rows = await state_capability.call("list", {})
    if not isinstance(rows, list):
        return 0
    row = next(
        (r for r in rows if isinstance(r, dict) and str(r.get("pipeline_id") or "") == pipeline_id),
        None,
    )
    if row is None:
        return 0
    emitted = 0
    for name, tags in derive_task_terminal_events(event_name, row):
        # 终态对账（两态模型绑定不变量）：派生 task_failed 而该管道 state 的
        # task.status 未达 failed（kill 方未随写，如 stalled/预算署名终止），
        # 由此单点补落——读面（前端/轮询）否则永久看到 running。completed 由
        # 评估闸门自落，无需对账；写失败仅告警不阻断事件派生。
        if name == "task_failed" and str(row.get("task.status") or "") != "failed":
            try:
                await state_capability.call(
                    "update",
                    {"pipeline_id": pipeline_id, "fields": {"task.status": "failed"}},
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[task_service] task.status 终态对账写回失败（不影响事件派生）| pipeline=%s",
                    pipeline_id,
                )
        try:
            await bus_capability.call("emit_domain", {"event": name, "tags": tags})
            emitted += 1
            logger.info(
                "[task_service] 任务域事件派生 | event=%s | pipeline_id=%s | task_id=%s",
                name, tags.get("pipeline_id"), tags.get("task_id"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("[task_service] emit_domain 失败 | event=%s", name)
        clear_fields = pending_registration_clear_fields(tags)
        if clear_fields is None:
            continue
        try:
            await state_capability.call(
                "update",
                {
                    "pipeline_id": str(tags.get("parent_pipeline_id") or ""),
                    "fields": clear_fields,
                },
            )
            logger.info(
                "[task_service] 父管道子任务挂号已清除 | parent=%s | fields=%s",
                tags.get("parent_pipeline_id"),
                clear_fields,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[task_service] 父管道挂号键清除失败（不影响事件派生）| parent=%s | fields=%s",
                tags.get("parent_pipeline_id"),
                clear_fields,
            )
    return emitted
