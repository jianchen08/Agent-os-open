"""管道退出后状态处理 Mixin。

负责管道执行完成后的任务状态检查：
- 有输出 → 转为 evaluating 并触发评估
- 无输出 → 标记 failed（含精确错误诊断）

从 task_executor.py 拆分而出，降低文件复杂度。
"""

from __future__ import annotations

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
        terminal_evt: Any,
        timer_manager: Any,
        idle_timer_registered: bool,
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
            terminal_evt: 终态事件
            timer_manager: 计时器管理器
            idle_timer_registered: 是否已注册 idle 计时器
        """
        if not task_service:
            return
        task = task_service.get_task(task_id)
        if task is None:
            return

        status_str = (
            task.status if isinstance(task.status, str) else task.status.value
        )
        if status_str != "running":
            return

        task_result = getattr(task, "result", None)
        if task_result:
            await self._transition_to_evaluating(
                task_id, task_service, lifecycle, workspace, ws_meta,
                terminal_evt, timer_manager, idle_timer_registered,
            )
        else:
            await self._fail_after_pipeline_exit(
                task_id, task_service, pipeline_state,
                terminal_evt, timer_manager, idle_timer_registered,
            )

    async def _transition_to_evaluating(
        self,
        task_id: str,
        task_service: Any,
        lifecycle: Any,
        workspace: str,
        ws_meta: dict,
        terminal_evt: Any,
        timer_manager: Any,
        idle_timer_registered: bool,
    ) -> None:
        """有输出 → 转为 evaluating 并触发评估。

        BUG-FIX-fix_20260510_evaluating_stuck:
        move_to_evaluating 成功后，调用 _rerun_evaluation
        触发实际评估执行（复用系统重启恢复的逻辑）。
        """
        logger.info(
            "TaskWorker: task %s still RUNNING after pipeline exit, "
            "has result → evaluating",
            task_id,
        )
        if lifecycle:
            try:
                lifecycle.on_before_evaluate(workspace, ws_meta)
            except Exception as e:
                logger.warning(
                    "TaskWorker: lifecycle on_before_evaluate failed: "
                    "task_id=%s, error=%s",
                    task_id, e,
                )
        self._terminal_events.pop(task_id, None)
        try:
            await task_service.move_to_evaluating(task_id)
        except Exception as e:
            logger.warning(
                "TaskWorker: move_to_evaluating failed for %s: %s",
                task_id, e,
            )
            try:
                await task_service.fail_task(
                    task_id, f"管道退出后状态转移失败: {e}",
                )
            except Exception:
                pass
            await self._cleanup_post_pipeline(
                task_id, terminal_evt, timer_manager, idle_timer_registered,
            )
            return

        await self._cleanup_post_pipeline(
            task_id, terminal_evt, timer_manager, idle_timer_registered,
        )
        refreshed_task = task_service.get_task(task_id)
        if refreshed_task is not None:
            try:
                await self._rerun_evaluation(refreshed_task)
            except Exception as rerun_exc:
                logger.error(
                    "TaskWorker: _rerun_evaluation failed for %s: %s",
                    task_id, rerun_exc,
                )
                try:
                    await task_service.fail_task(
                        task_id, f"管道退出后评估执行失败: {rerun_exc}",
                    )
                except Exception:
                    pass

    async def _fail_after_pipeline_exit(
        self,
        task_id: str,
        task_service: Any,
        pipeline_state: dict | None,
        terminal_evt: Any,
        timer_manager: Any,
        idle_timer_registered: bool,
    ) -> None:
        """无输出 → 从管道状态构建精确错误信息并标记 failed。"""
        error_msg = self._build_pipeline_exit_error(pipeline_state)
        logger.warning(
            "TaskWorker: task %s still RUNNING after pipeline exit → %s",
            task_id, error_msg,
        )
        self._terminal_events.pop(task_id, None)
        await task_service.fail_task(task_id, error_msg)
        await self._cleanup_post_pipeline(
            task_id, terminal_evt, timer_manager, idle_timer_registered,
        )

    async def _cleanup_post_pipeline(
        self,
        task_id: str,
        terminal_evt: Any,
        timer_manager: Any,
        idle_timer_registered: bool,
    ) -> None:
        """管道退出后的资源清理（状态重置、计时器取消）。"""
        terminal_evt.set()
        self._active_tasks.discard(task_id)
        self._idle_remind_counts.pop(task_id, None)
        if idle_timer_registered and timer_manager:
            try:
                await timer_manager.cancel_timer(task_id)
            except Exception:
                pass

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
        hit_max_iter = (
            isinstance(iteration_count, int)
            and isinstance(max_iter, int)
            and iteration_count >= max_iter
        )

        if raw_error:
            parts.append(f"管道异常退出: {raw_error}")
            if llm_error_info and llm_error_info.get("error_type"):
                parts.append(f"错误类型={llm_error_info['error_type']}")
        elif stop_reason == "timeout":
            parts.append("执行超时")
        elif hit_max_iter:
            parts.append(f"管道迭代耗尽({iteration_count}/{max_iter})")
        else:
            parts.append(
                f"管道异常结束(iterations={iteration_count}/{max_iter})"
            )

        if error_analysis:
            parts.append(f"错误分析: {error_analysis}")
        if task_complete is False:
            parts.append("Agent 标记任务未完成")

        return (
            "，".join(parts)
            if parts
            else "管道异常退出，Agent 未完成评估"
        )
