"""管道退出后状态处理 Mixin。

负责管道执行完成后的任务状态检查：
- 有输出 → 转为 evaluating 并触发评估
- 无输出 → 标记 failed（含精确错误诊断）

从 task_worker.py 拆分而出，降低原文件复杂度。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


class TaskPostPipelineMixin:
    """管道退出后状态处理混入类。

    提供 _check_post_pipeline_state、_transition_to_evaluating、
    _fail_after_pipeline_exit、_cleanup_post_pipeline、
    _build_pipeline_exit_error 方法，
    由 TaskWorker 通过多继承组合使用。
    """

    async def _check_post_pipeline_state(
        self,
        task_id: str,
        task_service: Any,
        pipeline_state: dict | None,
        lifecycle: Any,
        workspace: str,
        ws_meta: dict,
        ctx: Any,
        timer_manager: Any,
    ) -> None:
        """管道退出后检查任务状态，处理 evaluating 或 failed 转换。

        管道执行完成后任务仍为 RUNNING 时：
        - 有 result 输出 → 转为 evaluating 触发评估
        - 无输出 → 标记 failed（含精确错误诊断信息）

        Args:
            task_id: 任务 ID
            task_service: 任务服务实例
            pipeline_state: 管道执行返回的状态字典
            lifecycle: 工作空间生命周期管理器
            workspace: 工作空间路径
            ws_meta: 工作空间元数据
            ctx: 任务执行上下文（TaskExecutionContext）
            timer_manager: 计时器管理器
        """
        if not task_service:
            return
        task = task_service.get_task(task_id)
        if task is None:
            return

        status_str = task.status if isinstance(task.status, str) else task.status.value
        if status_str != "running":
            return

        task_result = getattr(task, "result", None)
        if task_result:
            await self._transition_to_evaluating(
                task_id,
                task_service,
                lifecycle,
                workspace,
                ws_meta,
                ctx,
                timer_manager,
            )
        else:
            await self._fail_after_pipeline_exit(
                task_id,
                task_service,
                pipeline_state,
                ctx,
                timer_manager,
            )

    async def _transition_to_evaluating(
        self,
        task_id: str,
        task_service: Any,
        lifecycle: Any,
        workspace: str,
        ws_meta: dict,
        ctx: Any,
        timer_manager: Any,
    ) -> None:
        """有输出 → 转为 evaluating 并触发评估。

        move_to_evaluating 成功后，调用 _rerun_evaluation 触发实际评估执行
        （复用系统重启恢复的逻辑），确保任务不会卡在 evaluating 状态。
        """
        logger.info(
            "TaskWorker: task %s still RUNNING after pipeline exit, has result output -> moving to evaluating",
            task_id,
        )
        if lifecycle:
            try:
                lifecycle.on_before_evaluate(workspace, ws_meta)
            except Exception as e:
                logger.warning(
                    "TaskWorker: lifecycle on_before_evaluate failed: task_id=%s, error=%s",
                    task_id,
                    e,
                )
        try:
            await task_service.move_to_evaluating(task_id)
        except Exception as e:
            logger.warning(
                "TaskWorker: move_to_evaluating failed for %s: %s, falling back to fail",
                task_id,
                e,
            )
            try:
                await task_service.fail_task(task_id, f"管道退出后状态转移失败: {e}")
            except Exception as fail_exc:
                logger.error(
                    "TaskWorker: fallback fail_task also failed for %s: %s",
                    task_id,
                    fail_exc,
                )
            ctx.set_terminal()
            ctx.cleanup(timer_manager)
            return

        ctx.set_terminal()
        ctx.cleanup(timer_manager)
        refreshed_task = task_service.get_task(task_id)
        if refreshed_task is not None:
            try:
                await self._rerun_evaluation(refreshed_task)
            except Exception as rerun_exc:
                logger.error(
                    "TaskWorker: _rerun_evaluation failed for %s: %s",
                    task_id,
                    rerun_exc,
                )
                with contextlib.suppress(Exception):
                    await task_service.fail_task(
                        task_id,
                        f"管道退出后评估执行失败: {rerun_exc}",
                    )

    async def _fail_after_pipeline_exit(
        self,
        task_id: str,
        task_service: Any,
        pipeline_state: dict | None,
        ctx: Any,
        timer_manager: Any,
    ) -> None:
        """无输出 → 从管道状态构建精确错误信息并标记 failed。

        BUG-FIX: 中断恢复 - is_interrupted 时 reset_to_pending 而非 fail_task，
        允许系统重启后自动恢复被中断的任务。
        """
        # 从 pipeline state 中提取完整诊断信息
        iteration_count = pipeline_state.get("iteration", "?") if pipeline_state else "?"
        max_iter = pipeline_state.get("max_iterations", "?") if pipeline_state else "?"
        ended = pipeline_state.get("ended", "?") if pipeline_state else "?"
        raw_error = pipeline_state.get("raw_error") if pipeline_state else None
        llm_error_info = pipeline_state.get("llm_error_info") if pipeline_state else None
        task_complete = pipeline_state.get("task_complete") if pipeline_state else None
        error_analysis = pipeline_state.get("error_analysis") if pipeline_state else None
        stop_reason = pipeline_state.get("router.stop_reason", "") if pipeline_state else ""
        pipeline_id = pipeline_state.get("pipeline_id", "unknown") if pipeline_state else "unknown"

        logger.info(
            "TaskWorker: _fail_after_pipeline_exit 诊断 | task=%s pipeline=%s "
            "state_empty=%s state_not_ended=%s iteration=%s/%s ended=%s "
            "raw_error=%s stop_reason=%s",
            task_id,
            pipeline_id[:12] if isinstance(pipeline_id, str) else pipeline_id,
            not pipeline_state,
            isinstance(ended, bool) and not ended,
            iteration_count,
            max_iter,
            ended,
            raw_error or "(none)",
            stop_reason or "(none)",
        )

        # 统计 LLM 错误类型分布（从 pipeline state 的单一数据源取）
        # 供 task metadata、watchdog、通知器等任意消费方使用
        from collections import Counter  # noqa: PLC0415

        error_history = pipeline_state.get("llm_error_history", []) if pipeline_state else []
        error_kinds: dict[str, int] = {}
        if error_history:
            error_kinds = dict(Counter(h["kind"] for h in error_history))

        # 根据实际原因构建精确的错误信息
        parts: list[str] = []
        hit_max_iter = isinstance(iteration_count, int) and isinstance(max_iter, int) and iteration_count >= max_iter
        state_is_empty = not pipeline_state
        state_not_ended = isinstance(ended, bool) and not ended

        if raw_error:
            # 管道内有明确错误（LLM 调用失败、工具异常等）→ 直接透传
            parts.append(f"管道异常退出: {raw_error}")
            if llm_error_info:
                etype = llm_error_info.get("error_type", "")
                if etype:
                    parts.append(f"错误类型={etype}")
        elif stop_reason:
            # 有明确的停止原因 → 直接透传，不做分类匹配
            parts.append(stop_reason)
        elif hit_max_iter:
            # 确实是迭代耗尽
            parts.append(f"管道迭代耗尽({iteration_count}/{max_iter})")
        elif state_is_empty or state_not_ended:
            # pipeline_state 为空（进程被杀/重启导致 asyncio task 未完成）
            # 或 ended=False（管道循环被外部中断，如 CancelledError 后进程退出）
            parts.append(
                f"管道被中断(可能原因: 进程重启或被强制终止)"
                f"(iterations={iteration_count}/{max_iter},"
                f" ended={ended},"
                f" pipeline={pipeline_id[:12] if isinstance(pipeline_id, str) else pipeline_id})"
            )
        else:
            # 其他未知原因
            parts.append(f"管道异常结束(iterations={iteration_count}/{max_iter})")

        if error_analysis:
            parts.append(f"错误分析: {error_analysis}")
        if error_kinds:
            summary = "、".join(f"{k}:{v}次" for k, v in sorted(error_kinds.items(), key=lambda x: -x[1]))
            parts.append(f"错误统计: {summary}")
        if task_complete is False:
            parts.append("Agent 标记任务未完成")

        error_msg = "，".join(parts) if parts else "管道异常退出，Agent 未完成评估"

        logger.warning(
            "TaskWorker: task %s still RUNNING "
            "after pipeline exit. "
            "iterations=%s/%s, ended=%s, "
            "raw_error=%s, "
            "has_result=False → %s",
            task_id,
            iteration_count,
            max_iter,
            ended,
            raw_error or "(none)",
            error_msg,
        )

        if task_service:
            await task_service.fail_task(
                task_id,
                error_msg,
                extra_meta={"error_kinds": error_kinds} if error_kinds else None,
            )
            logger.info(
                "TaskWorker: task %s marked failed after pipeline exit: %s",
                task_id,
                error_msg,
            )
        ctx.set_terminal()
        ctx.cleanup(timer_manager)

    def _build_pipeline_exit_error(self, pipeline_state: dict | None) -> str:
        """从管道状态构建精确的错误信息。

        根据实际原因（迭代耗尽 / LLM 调用失败 / 超时 / 无路由信号）
        构建不同的错误描述，便于排查。

        Args:
            pipeline_state: 管道执行返回的状态字典

        Returns:
            人类可读的错误描述字符串
        """
        if not pipeline_state:
            return "管道异常退出，Agent 未完成评估"

        iteration_count = pipeline_state.get("iteration", "?")
        max_iter = pipeline_state.get("max_iterations", "?")
        raw_error = pipeline_state.get("raw_error")
        llm_error_info = pipeline_state.get("llm_error_info")
        task_complete = pipeline_state.get("task_complete")
        error_analysis = pipeline_state.get("error_analysis")
        stop_reason = pipeline_state.get("router.stop_reason", "")

        parts: list[str] = []
        hit_max_iter = isinstance(iteration_count, int) and isinstance(max_iter, int) and iteration_count >= max_iter

        if raw_error:
            parts.append(f"管道异常退出: {raw_error}")
            if llm_error_info and llm_error_info.get("error_type"):
                parts.append(f"错误类型={llm_error_info['error_type']}")
        elif stop_reason:
            # 有明确的停止原因 → 直接透传
            parts.append(stop_reason)
        elif hit_max_iter:
            parts.append(f"管道迭代耗尽({iteration_count}/{max_iter})")
        else:
            parts.append(f"管道异常结束(iterations={iteration_count}/{max_iter})")

        if error_analysis:
            parts.append(f"错误分析: {error_analysis}")
        if task_complete is False:
            parts.append("Agent 标记任务未完成")

        return "，".join(parts) if parts else "管道异常退出，Agent 未完成评估"
