"""任务恢复 Mixin。

负责系统启动时的任务恢复逻辑：running/pending 任务恢复、
evaluating 任务重新激活、评估重跑、恢复参数构建。

从 task_worker.py 拆分而出，降低原文件复杂度。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TaskRecoveryMixin:
    """任务恢复混入类。

    提供 _recover_running_tasks、_recover_evaluating_tasks、
    _rerun_evaluation、_build_recovery_input_params 方法，
    由 TaskWorker 通过多继承组合使用。
    """

    async def _recover_running_tasks(self) -> None:
        """恢复残留的 running、pending 和 paused 任务。

        Worker 启动时扫描所有 status=running、status=pending 和 status=paused 的任务，
        running/paused 任务先重置为 pending，然后统一通过 submit_task 触发执行。
        跳过容器任务（task_scope=container）。

        BUG-FIX-fix_20260525_paused_recovery:
            Worker stop() 会将 RUNNING/PENDING 任务标记为 PAUSED（见 task_worker.py L292-306），
            但旧版 _recover_running_tasks 只恢复 RUNNING 和 PENDING，遗漏了 PAUSED 状态，
            导致 stop→start 后 PAUSED 任务永远无法恢复。修复：增加对 PAUSED 任务的恢复。
        """
        if not self._task_service:
            return

        # 局部导入：避免模块级循环依赖
        from tasks.types import TaskStatus

        # 1. running 任务重置为 pending
        running_tasks = self._task_service.list_by_status(TaskStatus.RUNNING)
        for task in running_tasks:
            task_scope = task.metadata.get("task_scope", "non_container")
            if task_scope == "container":
                logger.debug(
                    "TaskWorker: 跳过容器任务恢复: task_id=%s", task.id,
                )
                continue
            try:
                await self._task_service.reset_to_pending(task.id)
                logger.info(
                    "TaskWorker: 恢复 running → pending: task_id=%s", task.id,
                )
            except Exception as e:
                logger.warning(
                    "TaskWorker: 恢复任务失败: task_id=%s, error=%s", task.id, e,
                )

        # 1.5. paused 任务重置为 pending（stop→start 后 RUNNING/PENDING 被标记为 PAUSED）
        paused_tasks = self._task_service.list_by_status(TaskStatus.SUSPENDED)
        for task in paused_tasks:
            task_scope = task.metadata.get("task_scope", "non_container")
            if task_scope == "container":
                logger.debug(
                    "TaskWorker: 跳过容器任务恢复(paused): task_id=%s", task.id,
                )
                continue
            try:
                await self._task_service.reset_to_pending(task.id)
                logger.info(
                    "TaskWorker: 恢复 paused → pending: task_id=%s", task.id,
                )
            except Exception as e:
                logger.warning(
                    "TaskWorker: 恢复 paused 任务失败: task_id=%s, error=%s",
                    task.id, e,
                )

        # 2. 所有 pending 任务统一通过 submit_task 触发
        recovered = 0
        pending_tasks = self._task_service.list_by_status(TaskStatus.PENDING)
        for task in pending_tasks:
            task_scope = task.metadata.get("task_scope", "non_container")
            if task_scope == "container":
                continue
            if not task.metadata.get("target_id"):
                continue
            try:
                self.submit_task({
                    "task_id": task.id,
                    "target_type": task.target_type or "agent",
                    "target_id": task.metadata.get("target_id", ""),
                    "user_input": task.title,
                    "description": task.description,
                    "acceptance_criteria": task.metadata.get("acceptance_criteria", {}),
                    "workspace": task.metadata.get("workspace", ""),
                })
                recovered += 1
                logger.info(
                    "TaskWorker: 恢复 pending 任务: task_id=%s", task.id,
                )
            except Exception as e:
                logger.warning(
                    "TaskWorker: 恢复 pending 任务失败: task_id=%s, error=%s", task.id, e,
                )

        if recovered:
            logger.info("TaskWorker: 恢复了 %d 个任务", recovered)

    async def _recover_evaluating_tasks(self) -> None:
        """恢复 evaluating 状态的任务：保持评估状态，重新激活评估管道。

        系统重启时，处于 EVALUATING 的任务不会被 _recover_running_tasks
        处理（它只管 RUNNING → PENDING）。这些任务需要直接重新触发
        EvaluationExecutor，对剩余未通过的指标重新评估。
        """
        if not self._task_service:
            return

        from tasks.types import TaskStatus

        evaluating_tasks = self._task_service.list_by_status(
            TaskStatus.EVALUATING,
        )
        if not evaluating_tasks:
            return

        logger.info(
            "TaskWorker: 发现 %d 个 evaluating 任务，"
            "准备重新激活评估管道",
            len(evaluating_tasks),
        )

        for task in evaluating_tasks:
            task_scope = task.metadata.get(
                "task_scope", "non_container",
            ) if task.metadata else "non_container"
            if task_scope == "container":
                logger.debug(
                    "TaskWorker: 跳过容器任务评估恢复: task_id=%s",
                    task.id,
                )
                continue

            try:
                await self._rerun_evaluation(task)
            except Exception as e:
                logger.error(
                    "TaskWorker: 恢复 evaluating 任务失败: "
                    "task_id=%s, error=%s",
                    task.id, e,
                )
                try:
                    # BUG-FIX-fix_20260512_async_compat: fail_task 现在是 async
                    await self._task_service.fail_task(
                        task.id, f"评估恢复失败: {e}",
                    )
                except Exception:
                    logger.warning("TaskWorker: fail_task 也失败: task_id=%s", task.id, exc_info=True)

    async def _rerun_evaluation(self, task: Any) -> None:
        """为 evaluating 状态的任务重新运行评估。

        保持任务在 evaluating 状态，直接创建 EvaluationExecutor
        对剩余未通过的指标执行评估，完成后转换为终态。
        """
        import asyncio as _asyncio
        from evaluation.executor import EvaluationExecutor

        task_id = task.id
        metadata = task.metadata or {}

        # 1. 提取评估指标 ID
        metric_ids: list[str] = metadata.get(
            "evaluation_metric_ids", [],
        )
        if not metric_ids:
            ac = metadata.get("acceptance_criteria", {})
            if isinstance(ac, dict):
                metric_ids = list(ac.keys())

        if not metric_ids:
            logger.info(
                "TaskWorker: 任务 %s 无评估指标，直接标记完成",
                task_id,
            )
            # BUG-FIX-fix_20260512_async_compat: complete_evaluation 现在是 async
            await self._task_service.complete_evaluation(task_id, passed=True)
            return

        # 2. 从 evaluation_history 收集已通过指标
        history = metadata.get("evaluation_history", [])
        latest: dict[str, bool] = {}
        if isinstance(history, list):
            for entry in history:
                metrics = entry.get("metrics", [])
                for m in metrics:
                    mid = m.get("metric_id")
                    if mid:
                        latest[mid] = m.get("passed", False)

        remaining = [mid for mid in metric_ids if not latest.get(mid, False)]

        if not remaining:
            logger.info(
                "TaskWorker: 任务 %s 所有指标已通过，直接标记完成",
                task_id,
            )
            # BUG-FIX-fix_20260512_async_compat: complete_evaluation 现在是 async
            await self._task_service.complete_evaluation(task_id, passed=True)
            return

        # 3. 构建 input_params
        input_params = self._build_recovery_input_params(task, metric_ids)

        # 4. 创建 EvaluationExecutor
        pipeline_factory = self._services.get("pipeline_factory")
        agent_registry = self._services.get("agent_registry")
        tool_registry = self._services.get("tool_registry")

        loop = _asyncio.get_running_loop()
        executor = EvaluationExecutor(
            task_service=self._task_service,
            pipeline_factory=pipeline_factory,
            agent_registry=agent_registry,
            tool_registry=tool_registry,
            main_loop=loop,
        )

        # 5. 运行评估
        timeout = float(metadata.get("eval_timeout", 600))
        logger.info(
            "TaskWorker: 重新激活评估管道: task_id=%s, "
            "remaining=%s, timeout=%ss",
            task_id, remaining, timeout,
        )

        # BUG-FIX-fix_20260531_idle_timer_race:
        # 问题根因: 评估管道在等待人类交互时(最长300s)，idle timer
        #   仍在倒计时，300s后触发将任务标记为 failed，覆盖评估结果。
        #   导致用户点击"通过"后任务状态仍为 failed。
        # 修复方案: 评估期间取消 idle timer，评估完成后恢复。
        _idle_timer_cancelled = False
        if hasattr(self, "_cancel_idle_timer_async"):
            try:
                self._cancel_idle_timer_async(task_id)
                _idle_timer_cancelled = True
                logger.debug(
                    "TaskWorker: 评估期间取消 idle timer: task_id=%s",
                    task_id,
                )
            except Exception:
                pass

        # BUG-FIX-fix_20260512_async_compat: run_evaluation 现在是 async，直接 await
        result = await _asyncio.wait_for(
            executor.run_evaluation(
                task_id=task_id,
                metric_ids=remaining,
                input_params=input_params,
                skip_state_update=True,
            ),
            timeout=timeout,
        )

        # 6. 处理评估结果
        if result.overall_passed:
            logger.info(
                "TaskWorker: 评估恢复完成（通过）: task_id=%s",
                task_id,
            )
            # BUG-FIX-fix_20260512_async_compat: complete_evaluation 现在是 async
            await self._task_service.complete_evaluation(
                task_id, passed=True, result={
                    "overall_passed": True,
                    "summary": result.summary,
                    "recovered": True,
                    "metrics": [
                        {
                            "metric_id": r.metric_id,
                            "passed": r.passed,
                            "score": r.score,
                            "message": r.message,
                        }
                        for r in result.results
                    ],
                },
            )
        else:
            failed_metrics = [
                r.metric_id for r in result.results if not r.passed
            ]
            logger.warning(
                "TaskWorker: 评估恢复完成（未通过）: task_id=%s, "
                "failed=%s",
                task_id, failed_metrics,
            )
            # BUG-FIX-fix_20260512_async_compat: complete_evaluation 现在是 async
            await self._task_service.complete_evaluation(
                task_id, passed=False, result={
                    "overall_passed": False,
                    "summary": result.summary,
                    "recovered": True,
                    "metrics": [
                        {
                            "metric_id": r.metric_id,
                            "passed": r.passed,
                            "score": r.score,
                            "message": r.message,
                            "error": r.error,
                        }
                        for r in result.results
                    ],
                },
            )

    def _build_recovery_input_params(
        self, task: Any, metric_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """为评估恢复构建 input_params。

        从 task.metadata.acceptance_criteria 提取参数，
        并注入 workspace 路径确保评估工具能正确访问文件。
        """
        metadata = task.metadata or {}
        params: dict[str, dict[str, Any]] = {}

        ac = metadata.get("acceptance_criteria", {})
        if isinstance(ac, dict):
            _non_param_keys = {
                "expected_output", "pass_threshold", "description",
            }
            for metric_id, config in ac.items():
                if isinstance(config, dict) and metric_id in metric_ids:
                    if "input_params" in config:
                        params[metric_id] = config["input_params"]
                    else:
                        params[metric_id] = {
                            k: v for k, v in config.items()
                            if k not in _non_param_keys
                        }

        # 解析 workspace
        workspace_abs: str | None = None
        ws_meta = metadata.get("ws_meta")
        if ws_meta and isinstance(ws_meta, dict):
            ws_path = ws_meta.get("path")
            if ws_path:
                p = Path(ws_path)
                if not p.is_absolute():
                    p = Path.cwd() / p
                workspace_abs = str(p)

        if not workspace_abs:
            task_workspace = metadata.get("workspace")
            if task_workspace:
                workspace_abs = str(Path.cwd() / task_workspace)

        # 注入 workspace 和 criteria
        task_desc = ""
        if hasattr(task, "description") and task.description:
            task_desc = task.description
        elif hasattr(task, "title") and task.title:
            task_desc = task.title

        for metric_id in metric_ids:
            p = params.get(metric_id, {})
            if not p.get("criteria") and task_desc:
                p.setdefault("criteria", task_desc)
            if workspace_abs and "workspace" not in p:
                p["workspace"] = workspace_abs
            params[metric_id] = p

        return params
