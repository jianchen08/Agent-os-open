"""启动期跨表终态调和器（U13/D9 任务域半边）。

域界定：docs/working/U13终态两写域界定_20260906.md §二——分歧行仲裁依据 =
pipeline_state.task.status 投影（谁持证据谁裁决）；内核 reap_orphan_runs 把
残留 running 一律扫成 failed，与投影终态（评估闸门落的 completed）冲突时由
本模块在任务域裁决：

- 投影 completed + runs failed/suspended → completed 权威（补派 task_completed，
  闭合崩溃期间丢失的完成通知；runs 侧改写无任务域写面，归内核位）。
- 投影未决 + runs 侧权威终态 → runs 托底裁决（ADR
  2026-09-14-run-status-authority-startup-reconcile，翻案 U13 §二「completed
  之外不裁」的保守口径：内核 9a5881fb2 起 reap 补齐 run_status 投影、冷行
  携带权威 overlay，runs 侧已持真值证据）：runs completed → completed 补落 +
  补派完成通知；runs failed/cancelled → failed 对齐 + 补派失败通知。崩溃
  遗留的死管道不再永久悬挂为幽灵「执行中」（BUG-2b 面板根因）。
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
# （stopped/timeout/cancelled）与任务域终态（completed/failed）保守不裁
_UNDECIDED_TASK_STATUSES = frozenset({"", "running", "pending", "evaluating", "pending_evaluation"})

# 候选扫描 = runs 五态全量按 status 拉取（单次 500 上限），候选按管道收敛到
# 真最新 run（含 running——在飞 run 参与最新性收敛，防「旧终态候选 + 新在飞
# run」被误裁为终态）
_SCAN_RUN_STATUSES = ("running", "suspended", "failed", "completed", "cancelled")


def classify_orphan_run(run_status: str, task_status: str) -> str | None:
    """仲裁纯函数：runs 侧终态 × 投影终态 → 调和裁决。

    Returns:
        "completed"（投影 completed 权威，补派完成通知；或 runs 侧权威
        completed × 未决投影，补落 + 补派完成通知）/ "failed"（无终态证据或
        runs 侧 failed/cancelled 终态，对齐 failed）/ None（一致行、在飞行、
        可恢复行、用户侧终态——不裁）。
    """
    if task_status == "completed" and run_status in _COMPLETED_CANDIDATE_RUN_STATUSES:
        return "completed"
    if task_status in _UNDECIDED_TASK_STATUSES:
        if run_status == "completed":
            return "completed"
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
    runs completed × 投影未决先经任务域写面补落 completed，合成事件携带调和
    后投影 state 载荷派生完成通知（payload 路径免回查、不受出口白名单约束）。
    任一能力读面故障 → 降级留痕返回空（启动扫描是兜底面，不阻断插件装载）。
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
        # completed 权威裁决且投影未决：runs 侧真值托底、评估闸门对死 run 永不
        # 再结论——先经任务域写面补落 completed（写失败留痕续扫），合成事件携带
        # 调和后投影派生（否则 events 既有闸「跑完不假完成」会正确地零派生）。
        event_params: dict[str, Any] = {"pipeline_id": pid}
        if verdict == "completed" and task_status != "completed":
            try:
                await state_cap.call(
                    "update",
                    {"pipeline_id": pid, "fields": {"task.status": "completed"}},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[task_reconcile] completed 补落失败（留痕续扫）| pipeline=%s | err=%s",
                    pid, exc,
                )
                continue
            event_params["state"] = {**row, "task.status": "completed"}
        # 合成 run 终态事件走既有派生链（派生 + 对账 + 清挂号），不另立写面。
        # events 层对读面故障优雅降级（回 None 不抛），故以记录型包裹区分
        # 「读面死亡」（该行留痕跳过）与「正常零派生」（派发已完整发生）。
        watch = _StateReadFailureWatch(state_cap)
        synthetic_event = "run.completed" if verdict == "completed" else "run.failed"
        try:
            await events.handle_run_terminal_event(
                synthetic_event, event_params, watch, bus_cap
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
        reconciled.append(
            {
                "pipeline_id": pid,
                "run_id": run.get("run_id"),
                "runs_status": run_status,
                "task_status": task_status,
                "verdict": verdict,
            }
        )
        logger.info(
            "[task_reconcile] 分歧行已调和 | pipeline=%s | run=%s | 投影=%s | 裁决=%s",
            pid, run_status, task_status, verdict,
        )
    return reconciled
