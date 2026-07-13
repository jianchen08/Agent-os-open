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
        """启动时将残留任务标记为 suspended，等待用户手动恢复。

        系统重启后不再自动恢复任务，而是将所有未完成的任务
        （running、pending、suspended）统一标记为 suspended，
        用户在前端通过按钮手动恢复执行。
        FAILED 任务保持原样不自动恢复。

        - running 任务 → suspended（执行中被中断）
        - pending 任务 → suspended（尚未开始执行）
        - suspended 任务 → 保持 suspended（已是暂停态）
        - failed 任务 → 保持 failed（确实是执行失败的，不应自动恢复）
        - 跳过容器任务（task_scope=container）
        """
        if not self._task_service:
            return

        # 局部导入：避免模块级循环依赖
        from tasks.types import TaskStatus  # noqa: PLC0415

        suspended_count = 0

        # ── 1. running 任务 → suspended ──
        running_tasks = self._task_service.list_by_status(TaskStatus.RUNNING)
        for task in running_tasks:
            task_scope = task.metadata.get("task_scope", "non_container")
            if task_scope == "container":
                logger.debug(
                    "TaskWorker: 跳过容器任务恢复: task_id=%s",
                    task.id,
                )
                continue
            try:
                await self._task_service.pause_task(task.id, paused_by="system")
                suspended_count += 1
                logger.info(
                    "TaskWorker: 恢复 running → suspended: task_id=%s",
                    task.id,
                )
            except Exception as e:
                logger.warning(
                    "TaskWorker: 恢复 running 任务失败: task_id=%s, error=%s",
                    task.id,
                    e,
                )

        # ── 2. pending 任务 → suspended（不再自动提交执行） ──
        pending_tasks = self._task_service.list_by_status(TaskStatus.PENDING)
        for task in pending_tasks:
            task_scope = task.metadata.get("task_scope", "non_container")
            if task_scope == "container":
                continue
            try:
                await self._task_service.pause_task(task.id, paused_by="system")
                suspended_count += 1
                logger.info(
                    "TaskWorker: 恢复 pending → suspended: task_id=%s",
                    task.id,
                )
            except Exception as e:
                logger.warning(
                    "TaskWorker: 恢复 pending 任务失败: task_id=%s, error=%s",
                    task.id,
                    e,
                )

        # ── 3. 已是 stopped 的任务保持原样 ──
        # 无论是用户手动暂停还是系统暂停，都应保持 stopped，不自动恢复
        paused_tasks = self._task_service.list_by_status(TaskStatus.STOPPED)
        for task in paused_tasks:
            paused_by = (task.metadata or {}).get("paused_by", "unknown")
            logger.info(
                "TaskWorker: 保持暂停任务: task_id=%s paused_by=%s",
                task.id,
                paused_by,
            )
            suspended_count += 1

        # FAILED 任务不处理：保持 failed，不自动恢复

        if suspended_count:
            logger.info(
                "TaskWorker: 已将 %d 个任务标记为 suspended，等待用户手动恢复",
                suspended_count,
            )

    async def _recover_evaluating_tasks(self) -> None:
        """恢复 evaluating 状态的任务：保持评估状态，重新激活评估管道。

        系统重启时，处于 EVALUATING 的任务不会被 _recover_running_tasks
        处理（它只管 RUNNING → PENDING）。这些任务需要直接重新触发
        EvaluationExecutor，对剩余未通过的指标重新评估。
        """
        if not self._task_service:
            return

        from tasks.types import TaskStatus  # noqa: PLC0415

        evaluating_tasks = self._task_service.list_by_status(
            TaskStatus.EVALUATING,
        )
        if not evaluating_tasks:
            return

        logger.info(
            "TaskWorker: 发现 %d 个 evaluating 任务，准备重新激活评估管道",
            len(evaluating_tasks),
        )

        for task in evaluating_tasks:
            task_scope = (
                task.metadata.get(
                    "task_scope",
                    "non_container",
                )
                if task.metadata
                else "non_container"
            )
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
                    "TaskWorker: 恢复 evaluating 任务失败: task_id=%s, error=%s",
                    task.id,
                    e,
                )
                try:
                    await self._task_service.fail_task(
                        task.id,
                        f"评估恢复失败: {e}",
                    )
                except Exception:
                    logger.warning("TaskWorker: fail_task 也失败: task_id=%s", task.id, exc_info=True)

    async def _complete_with_merge(self, task_id: str, passed: bool, result: dict | None = None) -> None:
        """评估通过后统一完成入口：worktree 模式先合并再 complete_evaluation。

        评估通过后统一调用 lifecycle.merge_worktree_before_complete，合并失败则标记
        failed，避免走恢复路径的 worktree 任务产出永不合并。

        Args:
            task_id: 任务 ID
            passed: 评估是否通过
            result: 评估结果数据（传入会被按需补充合并失败信息）
        """
        if passed:
            merge_error = self._try_merge_worktree(task_id)
            if merge_error:
                logger.error(
                    "TaskWorker: 恢复路径 worktree 合并失败，任务标记 failed: task_id=%s, error=%s",
                    task_id,
                    merge_error,
                )
                result = dict(result) if result else {}
                result["overall_passed"] = False
                result["merge_failure"] = merge_error
                result["summary"] = f"评估指标已通过，但 worktree 合并失败: {merge_error}"
                await self._task_service.complete_evaluation(
                    task_id,
                    passed=False,
                    result=result,
                )
                return
        await self._task_service.complete_evaluation(task_id, passed=passed, result=result)

    def _try_merge_worktree(self, task_id: str) -> str | None:
        """获取 lifecycle 并执行合并门控，lifecycle 不可用时返回 None。"""
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        lifecycle = get_service_provider().get("workspace_lifecycle_manager")
        if lifecycle is None:
            logger.warning(
                "TaskWorker: workspace_lifecycle_manager 不可用，跳过合并门控 | task=%s",
                task_id,
            )
            return None
        return lifecycle.merge_worktree_before_complete(task_id)

    async def _rerun_evaluation(self, task: Any) -> None:
        """为 evaluating 状态的任务重新运行评估。

        保持任务在 evaluating 状态，直接创建 EvaluationExecutor
        对剩余未通过的指标执行评估，完成后转换为终态。
        """
        import asyncio as _asyncio  # noqa: PLC0415

        from evaluation.executor import EvaluationExecutor  # noqa: PLC0415

        task_id = task.id
        metadata = task.metadata or {}

        # 1. 提取评估指标 ID
        metric_ids: list[str] = metadata.get(
            "evaluation_metric_ids",
            [],
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
            await self._complete_with_merge(
                task_id,
                passed=True,
                result={"overall_passed": True, "summary": "无评估指标，自动通过"},
            )
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
            await self._complete_with_merge(
                task_id,
                passed=True,
                result={"overall_passed": True, "summary": "所有指标已通过（历史记录）"},
            )
            return

        # 3. 构建 input_params
        input_params = self._build_recovery_input_params(task, metric_ids)

        # 4. 创建 EvaluationExecutor（evaluation 链已不再需要 pipeline_factory，
        #    引擎经 EngineRegistry.register_pipeline 创建）
        agent_registry = self._services.get("agent_registry")
        tool_registry = self._services.get("tool_registry")

        loop = _asyncio.get_running_loop()
        executor = EvaluationExecutor(
            task_service=self._task_service,
            agent_registry=agent_registry,
            tool_registry=tool_registry,
            main_loop=loop,
        )

        # 5. 运行评估
        timeout = float(metadata.get("eval_timeout", 600))
        logger.info(
            "TaskWorker: 重新激活评估管道: task_id=%s, remaining=%s, timeout=%ss",
            task_id,
            remaining,
            timeout,
        )

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
            await self._complete_with_merge(
                task_id,
                passed=True,
                result={
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
            failed_metrics = [r.metric_id for r in result.results if not r.passed]
            logger.warning(
                "TaskWorker: 评估恢复完成（未通过）: task_id=%s, failed=%s",
                task_id,
                failed_metrics,
            )
            await self._task_service.complete_evaluation(
                task_id,
                passed=False,
                result={
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

    def _build_recovery_input_params(  # noqa: PLR0912
        self,
        task: Any,
        metric_ids: list[str],
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
                "expected_output",
                "pass_threshold",
                "description",
            }
            for metric_id, config in ac.items():
                if isinstance(config, dict) and metric_id in metric_ids:
                    if "input_params" in config:
                        params[metric_id] = config["input_params"]
                    else:
                        params[metric_id] = {k: v for k, v in config.items() if k not in _non_param_keys}

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
