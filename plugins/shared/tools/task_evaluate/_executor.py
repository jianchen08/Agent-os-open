"""评估执行器（0.2 生产版，批次B 2026-08-24）。

替代 _eval_core.EvaluationExecutor 的 RuntimeError 占位：真实执行评估。

- **tool 型指标**（evaluator_type=tool，如 file_check）：本地执行——按指标
  input_schema 语义（exists/not_empty/contains/is_directory），相对路径以
  任务 workspace 为根解析（_get_input_params 已注入 workspace）。
- **agent 型指标**（evaluator_type=agent，如 semantic_check）：经 chat
  capability 派发**评估子管道**（evaluator_agent）——R2 裁定：出生 state 带
  被评估任务的 workspace/ws_meta（workspace_lifecycle 幂等跳过 → 评估者在
  被评估任务的工作区里跑，执行环境一致）；lineage 有父（挂被评估任务下）；
  plugin_configs.task_reminder.evaluation_mode=true（评估者模式：催
  evaluation_result JSON，不催 task_evaluate，防递归）。结论回收：轮询
  pipeline-state（同源白名单）evaluation.detected_result（task_reminder
  评估模式写入）+ task.status 终态，超时诚实失败。

方案：docs/working/管道工作区关联与评估管道装配方案_20260824.md 批次B；
出口契约：docs/decisions/2026-08-24-eval-pipeline-state-keys.md。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from _eval_core import EvaluationResult, MetricResult

logger = logging.getLogger(__name__)

# 注入约定（duck-typing，server.py on_load 装配）：
#   chat_send: async (params: dict) -> dict          —— chat.send_message 能力
#   state_rows: async () -> list[dict]                —— pipeline-state.list 能力
ChatSend = Callable[[dict[str, Any]], Any]
StateRows = Callable[[], Any]

# agent 型指标回收轮询间隔与内部回收上限（调用方 _get_eval_timeout 另有
# asyncio.wait_for 包裹，两者取先到）
_POLL_INTERVAL_S = 2.0
_AGENT_RECOVER_TIMEOUT_S = 600.0

# 评估者 agent（config/agents/system/evaluator_agent.yaml，L3 语义评估专家）
_EVALUATOR_AGENT_ID = "evaluator_agent"

# task.status 终态（失败族）：评估子管道翻车 → 指标诚实失败
_FAILED_STATUSES = {"failed", "cancelled", "timeout"}


class PipelineEvaluationExecutor:
    """0.2 评估执行器：tool 型本地跑，agent 型派评估子管道（R2 工作区继承）。"""

    def __init__(
        self,
        chat_send: ChatSend,
        state_rows: StateRows,
        metrics_config_path: str | None = None,
    ) -> None:
        self._chat_send = chat_send
        self._state_rows = state_rows
        self._metrics_config_path = metrics_config_path or self._default_metrics_path()
        self._metrics_cache: dict[str, dict[str, Any]] | None = None

    @staticmethod
    def _default_metrics_path() -> str:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
        return os.path.join(root, "config", "evaluation", "evaluation_metrics.yaml")

    # ── 指标定义加载 ───────────────────────────────────────────

    def _load_metrics(self) -> dict[str, dict[str, Any]]:
        """加载指标定义（name → 定义）。缺文件/坏格式 → 空表（指标按未定义诚实失败）。"""
        if self._metrics_cache is not None:
            return self._metrics_cache
        table: dict[str, dict[str, Any]] = {}
        try:
            import yaml  # noqa: PLC0415

            with open(self._metrics_config_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for m in data.get("metrics", []) or []:
                if isinstance(m, dict) and m.get("name"):
                    table[str(m["name"])] = m
        except Exception as exc:  # noqa: BLE001 — 指标面不可用不炸评估调用
            logger.warning(
                "[EvalExecutor] 指标配置加载失败 | path=%s err=%s",
                self._metrics_config_path,
                exc,
            )
        self._metrics_cache = table
        return table

    # ── 执行入口（对齐 _eval_core 契约）─────────────────────────

    async def run_evaluation(
        self,
        task_id: str,
        metric_ids: list[str] | None = None,
        input_params: dict[str, dict[str, Any]] | None = None,
        fail_fast: bool = True,  # noqa: ARG002 — 逐指标独立回收，语义兼容保留
        skip_state_update: bool = False,  # noqa: ARG002 — 状态写面在工具层（writer），此处不写
    ) -> EvaluationResult:
        result = EvaluationResult(task_id=task_id)
        params = input_params or {}
        for mid in metric_ids or []:
            try:
                result.results.append(await self._evaluate_metric(task_id, mid, params.get(mid) or {}))
            except Exception as exc:  # noqa: BLE001 — 单指标炸不连坐
                logger.exception("[EvalExecutor] 指标评估异常 | task=%s metric=%s", task_id, mid)
                result.results.append(
                    MetricResult(metric_id=mid, passed=False, error=f"评估异常: {exc}")
                )
        result.compute_overall()
        return result

    async def _evaluate_metric(
        self,
        task_id: str,
        metric_id: str,
        params: dict[str, Any],
    ) -> MetricResult:
        metrics = self._load_metrics()
        definition = metrics.get(metric_id)
        if definition is None:
            return MetricResult(
                metric_id=metric_id,
                passed=False,
                error=f"指标 {metric_id} 未在 evaluation_metrics.yaml 定义（诚实失败，不猜测语义）",
            )
        evaluator_type = str(definition.get("evaluator_type") or "tool")
        if evaluator_type == "agent":
            return await self._evaluate_via_pipeline(task_id, metric_id, params)
        return self._evaluate_tool_local(metric_id, params)

    # ── tool 型：本地执行 ──────────────────────────────────────

    @staticmethod
    def _evaluate_tool_local(metric_id: str, params: dict[str, Any]) -> MetricResult:
        """file_check 族：exists/not_empty/contains/is_directory（0.1 语义）。"""
        raw_path = str(params.get("path") or "")
        if not raw_path:
            return MetricResult(metric_id=metric_id, passed=False, error="path 参数缺失")
        workspace = str(params.get("workspace") or "")
        p = Path(raw_path)
        if not p.is_absolute() and workspace:
            p = Path(workspace) / p
        check = str(params.get("check") or "exists")

        if check == "exists":
            ok = p.exists()
            msg = f"存在: {p}" if ok else f"不存在: {p}"
        elif check == "is_directory":
            ok = p.is_dir()
            msg = f"目录存在: {p}" if ok else f"不是目录或不存在: {p}"
        elif check == "not_empty":
            min_size = int(params.get("min_size") or 1)
            ok = p.is_file() and p.stat().st_size >= min_size
            msg = f"非空（≥{min_size}B）: {p}" if ok else f"为空/过小/不存在: {p}"
        elif check == "contains":
            pattern = str(params.get("pattern") or "")
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return MetricResult(metric_id=metric_id, passed=False, error=f"读取失败: {exc}")
            ok = bool(pattern) and pattern in text
            msg = f"包含模式: {p}" if ok else f"未包含模式 {pattern!r}: {p}"
        else:
            return MetricResult(metric_id=metric_id, passed=False, error=f"未知检查类型: {check}")

        return MetricResult(metric_id=metric_id, passed=ok, message=msg)

    # ── agent 型：评估子管道（R2 工作区/执行环境继承）───────────

    async def _evaluate_via_pipeline(
        self,
        task_id: str,
        metric_id: str,
        params: dict[str, Any],
    ) -> MetricResult:
        task_row = await self._find_state_row(task_id)
        ws_meta = (task_row or {}).get("ws_meta")
        if not isinstance(ws_meta, dict) or not ws_meta.get("path"):
            return MetricResult(
                metric_id=metric_id,
                passed=False,
                error=f"任务 {task_id[:8]} 无工作区坐标（ws_meta.path），无法继承执行环境",
            )
        origin_session = str(
            (task_row or {}).get("lineage.origin_session_id") or task_id
        )

        kickoff = (
            f"评估任务 {task_id[:8]} 的指标 {metric_id}。\n"
            f"评估参数：{json.dumps(params, ensure_ascii=False, default=str)}\n"
            "请在工作区内核对任务产出物后给出结论，输出：\n"
            '```json\n{"evaluation_result": {"passed": true/false, "score": 0-100, '
            '"feedback": "评估说明..."}}\n```'
        )
        dispatch: dict[str, Any] = {
            "create": True,
            "message": kickoff,
            "user_id": "task_system",
            "agent_id": _EVALUATOR_AGENT_ID,
            "lineage": {
                "parent_pipeline_id": task_id,
                "origin_session_id": origin_session,
            },
            "state": {
                # R2 核心：出生即带被评估任务的工作区坐标（非保留字，注入合法；
                # workspace_lifecycle 见 state.workspace 已有幂等跳过 → 同目录跑）
                "workspace": str(ws_meta["path"]),
                "ws_meta": ws_meta,
                # 防递归：评估者模式（催 evaluation_result JSON，不催 task_evaluate）
                "plugin_configs": {"task_reminder": {"evaluation_mode": True}},
                # 评估域登记（ADR 2026-08-24-eval-pipeline-state-keys 出口）
                "evaluation.of_task": task_id,
                "evaluation.metric_id": metric_id,
                "display_name": f"评估·{metric_id}",
            },
            "background": True,
        }

        try:
            resp = await self._chat_send(dispatch)
        except Exception as exc:  # noqa: BLE001
            return MetricResult(metric_id=metric_id, passed=False, error=f"评估子管道派发失败: {exc}")
        eval_pid = str((resp or {}).get("pipeline_id") or "") if isinstance(resp, dict) else ""
        if not eval_pid:
            return MetricResult(
                metric_id=metric_id,
                passed=False,
                error=f"评估子管道派发未返回 pipeline_id: {resp!r:.200}",
            )
        logger.info(
            "[EvalExecutor] 评估子管道已派发 | task=%s metric=%s eval_pipe=%s ws=%s",
            task_id,
            metric_id,
            eval_pid,
            ws_meta["path"],
        )
        return await self._recover_result(eval_pid, metric_id)

    async def _recover_result(self, eval_pid: str, metric_id: str) -> MetricResult:
        """轮询回收 evaluation.detected_result / 失败终态 / 超时（诚实失败）。"""
        deadline = asyncio.get_running_loop().time() + _AGENT_RECOVER_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            row = await self._find_state_row(eval_pid)
            if row:
                detected = row.get("evaluation.detected_result")
                if isinstance(detected, dict):
                    passed = bool(detected.get("passed"))
                    score = detected.get("score")
                    return MetricResult(
                        metric_id=metric_id,
                        passed=passed,
                        score=float(score) if isinstance(score, (int, float)) else -1.0,
                        message=str(detected.get("feedback") or ""),
                        evaluator_output=detected,
                        pipeline_run_id=eval_pid,
                    )
                status = str(row.get("task.status") or "")
                if status in _FAILED_STATUSES:
                    return MetricResult(
                        metric_id=metric_id,
                        passed=False,
                        error=f"评估子管道终态 {status}",
                        pipeline_run_id=eval_pid,
                    )
            await asyncio.sleep(_POLL_INTERVAL_S)
        return MetricResult(
            metric_id=metric_id,
            passed=False,
            error=f"评估回收超时（{_AGENT_RECOVER_TIMEOUT_S:.0f}s）",
            pipeline_run_id=eval_pid,
        )

    async def _find_state_row(self, pipeline_id: str) -> dict[str, Any] | None:
        try:
            rows = self._state_rows()
            if asyncio.iscoroutine(rows):
                rows = await rows
        except Exception as exc:  # noqa: BLE001
            logger.warning("[EvalExecutor] state 行读取失败 | pipeline=%s err=%s", pipeline_id, exc)
            return None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if isinstance(row, dict) and str(row.get("pipeline_id") or "") == pipeline_id:
                return row
        return None
