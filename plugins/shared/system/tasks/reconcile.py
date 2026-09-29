"""启动期跨表终态调和器（U13/D9 任务域半边）。

域界定：docs/working/U13终态两写域界定_20260906.md §二——分歧行仲裁依据 =
pipeline_state.task.status 投影（谁持证据谁裁决）；内核 reap_orphan_runs 把
残留 running 一律扫成 failed，与投影终态（评估闸门落的 completed）冲突时由
本模块在任务域裁决：

- 投影 completed + runs failed/suspended → completed 权威（补派 task_completed，
  闭合崩溃期间丢失的完成通知；runs 侧改写无任务域写面，归内核位）。
- 投影未决 + runs failed/cancelled → failed 对齐 + 补派失败通知（ADR
  2026-09-14-run-status-authority-startup-reconcile；与 stop_check「跑到
  检测线仍未通过评估 = 失败」用户裁定同口径）。
- 投影未决 + runs completed → **不裁 completed**（ADR 2026-09-28：完成唯一
  判据 = task_evaluate 评估通过，用户裁定重申——run 正常收尾不构成完成证据），
  补落 pending_evaluation（表「run 已终、评估未决」，零派发不清挂号）；
  已是 pending_evaluation 的行幂等跳过。事故 2026-09-28：runs 托底把熔断
  收尾的 run 裁成 completed 并向父管道派发「已完成 ✅」，而验收产物从未
  产出、task_evaluate 从未被调。
- 真最新 run = running/suspended 一律不裁（在飞 run 与幽灵无法单次扫描区分
  ——幽灵 running 由恢复链前置仲裁识破，见 http_api.resume_task；候选集含
  running，「旧终态 run + 新在飞 run」按真最新判定不误裁）。

扫描触发点 = task_service on_load（内核启动 reap 之后、插件服务可用时）；插件
热重载（运行中 respawn）同路径安全：判定只看真最新 run 的终态，在飞 run
（running）参与最新性收敛、天然不进裁决。
"""

from __future__ import annotations

import logging
from typing import Any

import events

logger = logging.getLogger(__name__)

# 触发 completed 裁决的 runs 侧状态（stop→suspended→收尾实录形态：收尾写失败
# 后 runs 停留 suspended，reap 不扫 suspended，分歧长期悬挂）
_COMPLETED_CANDIDATE_RUN_STATUSES = frozenset({"failed", "suspended"})

# "无终态证据"集（未决投影）——仅这些值才允许 runs 托底裁决；用户侧终态
# （stopped/timeout/cancelled）与任务域终态（completed/failed）保守不裁。
# pending_evaluation 双面语义：runs failed 侧 = 未决（failed 对齐准入）；
# runs completed 侧 = 已调和产物（幂等跳过，见 classify_orphan_run）。
_UNDECIDED_TASK_STATUSES = frozenset({"", "running", "pending", "evaluating", "pending_evaluation"})

# 候选扫描 = runs 五态全量按 status 拉取（单次 500 上限），候选按管道收敛到
# 真最新 run（含 running——在飞 run 参与最新性收敛，防「旧终态候选 + 新在飞
# run」被误裁为终态）
_SCAN_RUN_STATUSES = ("running", "suspended", "failed", "completed", "cancelled")


def classify_orphan_run(run_status: str, task_status: str) -> str | None:
    """仲裁纯函数：runs 侧终态 × 投影终态 → 调和裁决。

    Returns:
        "completed"（投影 completed 权威，补派完成通知）/ "failed"（未决投影 ×
        runs failed/cancelled，对齐 failed + 补派失败通知）/ "pending_evaluation"
        （未决投影 × runs completed：run 正常收尾不构成完成证据，落待评估、
        零派发）/ None（一致行、在飞行、可恢复行、用户侧终态、已调和的
        pending_evaluation 行——不裁）。
    """
    if task_status == "completed" and run_status in _COMPLETED_CANDIDATE_RUN_STATUSES:
        return "completed"
    if task_status in _UNDECIDED_TASK_STATUSES:
        if run_status == "completed":
            # 完成唯一判据 = task_evaluate 评估通过（用户裁定 2026-09-28 重申）：
            # run 正常收尾（含熔断/自然停）不是完成证据。已是 pending_evaluation
            # 的行幂等跳过——on_load 高频触发（合宿成员集变化即 respawn），不churn。
            return None if task_status == "pending_evaluation" else "pending_evaluation"
        if run_status in ("failed", "cancelled"):
            return "failed"
    return None


class _StateReadFailureWatch:
    """state 读面包裹：记录派发途中读面（list）调用是否抛错。

    events 层对读面故障优雅降级（_lookup_state_row 回 None 不抛）、对写面
    故障自吞（对账/清挂号失败仅告警，不破坏派生）——调和面据此区分「读面
    死亡」（该行留痕跳过，不计入调和清单）与「正常完成派发」。异常在本包裹
    处标记后原样上抛，语义不变。
    """

    _READ_METHODS = frozenset({"list"})

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.read_failed = False

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        try:
            return await self._inner.call(method, params, timeout)
        except Exception:
            if method in self._READ_METHODS:
                self.read_failed = True
            raise


async def reconcile_startup(
    state_cap: Any, runs_cap: Any, bus_cap: Any
) -> list[dict[str, Any]]:
    """扫描分歧行并调和，返回调和清单（可观测）。

    数据面：pipeline-state.list（含 DB 冷兜底，重启后投影可见）×
    pipeline-runs.list（五态全量按 status 过滤）。每个候选管道取真最新 run
    判定（多 run 历史：旧 failed + 新 completed = 合法重跑，不裁；在飞 run
    参与最新性收敛，最新 run = running/suspended 不裁）。

    调和动作复用 events.handle_run_terminal_event 单点（合成 run 终态事件 →
    task_completed/task_failed 派生 + task.status 对账补落 + 父挂号清除）；
    runs completed × 投影未决不进派生链——只补落 pending_evaluation（完成
    通知零派发，完成唯一判据 = 评估通过）。任一能力读面故障 → 降级留痕返回
    空（启动扫描是兜底面，不阻断插件装载）。
    """
    try:
        rows = await state_cap.call("list", {})
        runs: list[dict[str, Any]] = []
        for status in _SCAN_RUN_STATUSES:
            page = await runs_cap.call("pipeline-runs.list", {"status": status, "limit": 500})
            if isinstance(page, list):
                runs.extend(r for r in page if isinstance(r, dict))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[task_reconcile] 能力读面缺席/故障，启动调和降级跳过 | err=%s", exc)
        return []
    if not isinstance(rows, list):
        return []

    # 候选行按 pipeline_id 收敛到最新 run（created_at 字典序 = ISO 时间序）
    candidates: dict[str, dict[str, Any]] = {}
    for run_row in runs:
        pid = str(run_row.get("pipeline_id") or "")
        if not pid:
            continue
        prev = candidates.get(pid)
        if prev is None or str(run_row.get("created_at") or "") > str(prev.get("created_at") or ""):
            candidates[pid] = run_row

    reconciled: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not events.is_task_state(row):
            continue
        pid = str(row.get("pipeline_id") or "")
        run = candidates.get(pid)
        if run is None:
            continue
        run_status = str(run.get("status") or "")
        task_status = str(row.get("task.status") or "")
        verdict = classify_orphan_run(run_status, task_status)
        if verdict is None:
            continue
        entry = {
            "pipeline_id": pid,
            "run_id": run.get("run_id"),
            "runs_status": run_status,
            "task_status": task_status,
            "verdict": verdict,
        }
        if verdict == "pending_evaluation":
            # 未决 × run 正常收尾：只补落待评估投影，零派发不清挂号（完成通知
            # 唯一来源 = 评估通过的 task_completed 派生链）。写失败留痕续扫。
            try:
                await state_cap.call(
                    "update",
                    {"pipeline_id": pid, "fields": {"task.status": "pending_evaluation"}},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[task_reconcile] 待评估补落失败（留痕续扫）| pipeline=%s | err=%s",
                    pid, exc,
                )
                continue
            reconciled.append(entry)
            logger.info(
                "[task_reconcile] run 完成但评估未决，落待评估（不派完成通知）| pipeline=%s | run=%s | 投影=%s",
                pid, run_status, task_status,
            )
            continue
        # 投影权威 completed（评估已通过，补派完成通知）/ 未决 × runs
        # failed/cancelled（failed 对齐，派生链自带 task.status=failed 对账
        # 补落）：合成 run 终态事件走既有派生链（派生 + 对账 + 清挂号），
        # 不另立写面。events 层对读面故障优雅降级（回 None 不抛），故以记录型
        # 包裹区分「读面死亡」（该行留痕跳过）与「正常零派生」（派发已完整发生）。
        watch = _StateReadFailureWatch(state_cap)
        synthetic_event = "run.completed" if verdict == "completed" else "run.failed"
        try:
            await events.handle_run_terminal_event(
                synthetic_event, {"pipeline_id": pid}, watch, bus_cap
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[task_reconcile] 分歧行调和派发失败（留痕续扫）| pipeline=%s | verdict=%s | err=%s",
                pid, verdict, exc,
            )
            continue
        if watch.read_failed:
            logger.warning(
                "[task_reconcile] 分歧行调和途中 state 读面故障（留痕续扫）| pipeline=%s | verdict=%s",
                pid, verdict,
            )
            continue
        reconciled.append(entry)
        logger.info(
            "[task_reconcile] 分歧行已调和 | pipeline=%s | run=%s | 投影=%s | 裁决=%s",
            pid, run_status, task_status, verdict,
        )
    return reconciled
