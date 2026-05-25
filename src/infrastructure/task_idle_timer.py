"""任务 idle 计时器管理 Mixin。

负责 idle 超时回调、计时器重建/取消、挂起管道唤醒等逻辑。

从 task_worker.py 拆分而出，降低原文件复杂度。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from typing import Any

logger = logging.getLogger(__name__)

_IDLE_REMIND_LIMIT = 3


class TaskIdleTimerMixin:
    """任务 idle 计时器管理混入类。

    提供 _on_idle_timeout、_cancel_idle_timer_async、_do_cancel_timer、
    _recreate_idle_timer_async、_try_resume_engine、_get_pipeline_last_activity
    方法，由 TaskWorker 通过多继承组合使用。
    """

    def _on_idle_timeout(self, task_id: str) -> None:
        """idle 计时器超时回调。

        idle 计时器在管道运行全生命周期中持续监控：
        - 管道活跃时：通过 checkpoint 文件龄判断是否真正存活，
          存活则重建 timer 继续监控，死亡则标记 failed
        - 管道挂起时：
          - 若有活跃子任务（running/pending/evaluating/scheduled），
            重建 timer 继续等待，不计入提醒次数
          - 若无活跃子任务，提醒并唤醒（最多 idle_remind_limit 次），超限则标记 failed

        BUG-FIX-fix_20260514_active_pipeline_deadlock:
        新增活跃管道存活检测，解决管道进程异常死亡后任务永远卡在 running 的问题。

        BUG-FIX-fix_20260522_idle_suspended_children:
        挂起管道等待子任务时，检查子任务是否仍在活跃运行。
        活跃子任务存在时不计入 remind 次数，避免编排 Agent 被误杀。

        Args:
            task_id: 超时的任务ID
        """
        ctx = self._contexts.get(task_id)
        task_service = self._task_service
        if not task_service:
            logger.warning(
                "TaskWorker: idle 超时但无 task_service，"
                "无法处理: task_id=%s",
                task_id,
            )
            return

        task = task_service.get_task(task_id)
        if task is None:
            self._cancel_idle_timer_async(task_id)
            return

        status_str = (
            task.status
            if isinstance(task.status, str)
            else task.status.value
        )
        if status_str != "running":
            logger.debug(
                "TaskWorker: idle 超时但任务已不在 running"
                " 状态: task_id=%s, status=%s",
                task_id, status_str,
            )
            if ctx:
                ctx.active = False
                ctx.idle_remind_count = 0
            self._cancel_idle_timer_async(task_id)
            return

        idle_remind_limit = _IDLE_REMIND_LIMIT
        remind_count = ctx.idle_remind_count if ctx else 0

        # BUG-FIX-fix_20260514_active_pipeline_deadlock:
        # 原逻辑: 管道活跃时直接取消 timer → 管道死亡后无检测机制 → 任务永远卡在 running
        # 新逻辑: 管道活跃时通过 checkpoint 文件龄判断是否真正存活，
        #         存活则重建 timer 继续监控，死亡则标记任务 failed
        if ctx and ctx.active:
            timer_manager = self._services.get("timer_manager")
            idle_threshold = (
                getattr(timer_manager, "idle_threshold", 300)
                if timer_manager else 300
            )
            max_stale_seconds = idle_threshold * 3

            last_activity = self._get_pipeline_last_activity(task_id)
            if last_activity is not None:
                stale_seconds = _time.time() - last_activity
                if stale_seconds < max_stale_seconds:
                    logger.debug(
                        "TaskWorker: idle 超时但管道活跃且近期有进度"
                        "(checkpoint %.0fs 前)，重建 timer: task_id=%s",
                        stale_seconds, task_id,
                    )
                    if timer_manager:
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(
                                self._recreate_idle_timer_async(
                                    task_id, timer_manager,
                                ),
                            )
                        except RuntimeError:
                            pass
                    return
                logger.warning(
                    "TaskWorker: 管道标记活跃但 checkpoint 已 %.0fs "
                    "无更新(阈值 %ds)，判定管道死亡: task_id=%s",
                    stale_seconds, max_stale_seconds, task_id,
                )
            else:
                logger.warning(
                    "TaskWorker: 管道标记活跃但无 checkpoint 记录，"
                    "判定管道死亡: task_id=%s",
                    task_id,
                )

        if ctx and ctx.suspended_engine is not None:
            has_active_children = self._has_active_children(task_id)

            if has_active_children:
                logger.info(
                    "TaskWorker: idle 超时但有挂起管道且子任务仍在运行，"
                    "重建 timer 继续等待（不计入提醒次数）: task_id=%s",
                    task_id,
                )
                timer_manager = self._services.get("timer_manager")
                if timer_manager:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(
                            self._recreate_idle_timer_async(
                                task_id, timer_manager,
                            ),
                        )
                    except RuntimeError:
                        pass
                return

            if remind_count < idle_remind_limit:
                ctx.idle_remind_count = remind_count + 1
                logger.info(
                    "TaskWorker: idle 超时但有挂起管道（无活跃子任务），"
                    "提醒 #%d: task_id=%s",
                    remind_count + 1, task_id,
                )
                self._try_resume_engine(task_id)

                timer_manager = self._services.get("timer_manager")
                if timer_manager:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(
                            self._recreate_idle_timer_async(
                                task_id, timer_manager,
                            ),
                        )
                    except RuntimeError:
                        logger.warning(
                            "TaskWorker: no event loop to "
                            "recreate idle timer: task_id=%s",
                            task_id,
                        )
                return

        try:
            timer_mgr = self._services.get("timer_manager")
            threshold = (
                getattr(timer_mgr, "idle_threshold", "?")
                if timer_mgr else "?"
            )
            # BUG-FIX-fix_20260512_async_compat:
            # _on_idle_timeout 是同步回调，但 fail_task 现在是 async，
            # 使用 asyncio.create_task 调度异步调用
            loop = asyncio.get_running_loop()
            loop.create_task(
                task_service.fail_task(
                    task_id,
                    f"idle 超时({threshold}s无活动)",
                )
            )
            logger.warning(
                "TaskWorker: 任务 idle 超时，已标记 failed: "
                "task_id=%s", task_id,
            )
            if ctx:
                ctx.set_terminal()
                ctx.cleanup(timer_mgr)
        except Exception as e:
            logger.error(
                "TaskWorker: idle 超时处理失败: "
                "task_id=%s, error=%s", task_id, e,
            )

    def _get_pipeline_last_activity(self, task_id: str) -> float | None:
        """通过 checkpoint 文件最后修改时间判断管道是否仍在运行。

        遍历 data/pipeline_checkpoints/ 目录，查找以 pipeline_run_id
        为前缀的 .json 文件，返回最新修改时间的 Unix 时间戳。
        仅在 _on_idle_timeout 中调用，用于检测"活跃但实际已死亡"的管道。

        BUG-FIX-fix_20260514_active_pipeline_deadlock:
        问题根因: 管道进程异常死亡后 task_id 残留在 _active_tasks，
                 idle_timer 因"管道活跃"被取消不再触发，任务永远卡在
                 running 状态，只能等系统重启时 _recover_running_tasks 恢复。
        修复方案: idle_timer 触发时通过 checkpoint 文件龄判断管道是否真正存活，
                 超过阈值则标记任务失败。

        Args:
            task_id: 任务 ID

        Returns:
            最新 checkpoint 的 Unix 时间戳，无 checkpoint 时返回 None
        """
        if not self._task_service:
            return None

        task = self._task_service.get_task(task_id)
        if not task or not task.pipeline_run_id:
            return None

        checkpoint_dir = os.path.join(
            os.getcwd(), "data", "pipeline_checkpoints",
        )
        if not os.path.exists(checkpoint_dir):
            return None

        latest_mtime = 0.0
        prefix = task.pipeline_run_id + "_"
        try:
            for f in os.listdir(checkpoint_dir):
                if f.startswith(prefix) and f.endswith(".json"):
                    fpath = os.path.join(checkpoint_dir, f)
                    try:
                        latest_mtime = max(
                            latest_mtime,
                            os.path.getmtime(fpath),
                        )
                    except OSError:
                        pass
        except OSError:
            return None

        return latest_mtime if latest_mtime > 0 else None

    def _cancel_idle_timer_async(self, task_id: str) -> None:
        """异步取消残留的 idle 计时器（从同步回调调用）。

        当 _on_idle_timeout 发现任务已不在 running 状态时，
        通过此方法调度异步计时器取消，防止计时器残留触发风暴。

        Args:
            task_id: 任务 ID
        """
        timer_manager = self._services.get("timer_manager")
        if not timer_manager:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._do_cancel_timer(task_id, timer_manager))
        except RuntimeError:
            pass

    async def _do_cancel_timer(
        self, task_id: str, timer_manager: Any,
    ) -> None:
        """实际执行计时器取消。

        Args:
            task_id: 任务 ID
            timer_manager: 计时器管理器实例
        """
        try:
            await timer_manager.cancel_timer(task_id)
            logger.debug(
                "TaskWorker: 残留 idle 计时器已取消: "
                "task_id=%s", task_id,
            )
        except Exception:
            pass

    async def _recreate_idle_timer_async(
        self, task_id: str, timer_manager: Any,
    ) -> None:
        """idle 超时提醒后异步重新创建计时器。

        在 _on_idle_timeout 发送提醒后调用，
        为下一个超时周期创建新计时器。
        重建前先检查任务状态，避免在任务已终态后
        无意义地重建计时器（防止超时风暴）。

        Args:
            task_id: 任务 ID
            timer_manager: 计时器管理器实例
        """
        try:
            # BUG-FIX: 重建前先检查任务是否仍在 running
            # 防止在任务已终态（failed/completed/evaluating）
            # 后无意义地重建计时器
            if self._task_service:
                task = self._task_service.get_task(task_id)
                if task is not None:
                    status = (
                        task.status
                        if isinstance(task.status, str)
                        else task.status.value
                    )
                    if status != "running":
                        logger.debug(
                            "TaskWorker: 跳过 idle 计时器重建，"
                            "任务已非 running: task_id=%s, "
                            "status=%s",
                            task_id, status,
                        )
                        ctx = self._contexts.get(task_id)
                        if ctx:
                            ctx.active = False
                            ctx.idle_remind_count = 0
                        return
            try:
                await timer_manager.cancel_timer(task_id)
            except Exception:
                pass
            await timer_manager.create_timer(
                task_id=task_id,
                timeout=float(timer_manager.idle_threshold),
                callback=lambda tid=task_id: self._on_idle_timeout(tid),
            )
            logger.info(
                "TaskWorker: idle timer recreated after "
                "remind for task %s", task_id,
            )
        except Exception as e:
            logger.warning(
                "TaskWorker: recreate idle timer failed: "
                "task_id=%s, error=%s",
                task_id, e,
            )

    def _has_active_children(self, task_id: str) -> bool:
        """检查任务是否有仍在活跃状态的子任务。

        在 _on_idle_timeout 中使用，判断挂起管道是否因等待活跃子任务而
        处于合理等待状态。如果子任务仍在 running/pending/evaluating/scheduled，
        说明父任务的"无活动"是预期行为，不应计入 idle remind 次数。

        参考实现: TaskReminderPlugin._has_active_children

        Args:
            task_id: 父任务 ID

        Returns:
            True 表示有活跃子任务，False 表示无活跃子任务
        """
        task_service = self._task_service
        if not task_service:
            return False

        try:
            subtasks = task_service.list_subtasks(task_id)
        except Exception:
            return False

        active_statuses = {"pending", "running", "evaluating", "scheduled"}
        for st in subtasks:
            status = st.status.value if hasattr(st.status, "value") else str(st.status)
            if status in active_statuses:
                return True
        return False

    def _try_resume_engine(self, task_id: str) -> None:
        """通过标记和 wake_event 请求主循环执行 resume。

        idle 超时回调是同步的，不能直接 await engine.resume()。
        旧方案通过 asyncio.create_task fire-and-forget 执行 resume，
        存在竞态和异常静默问题。新方案标记 resume_requested 并
        直接 set wake_event，由主循环统一执行 resume，
        保证 engine 操作的串行性。

        Args:
            task_id: 挂起管道对应的任务 ID
        """
        ctx = self._contexts.get(task_id)
        if not ctx or ctx.suspended_engine is None:
            return

        ctx.resume_requested = True
        logger.debug("TaskWorker: resume requested for task %s", task_id)

        ctx.wake_event.set()

    async def reset_idle_timer(self, task_id: str) -> None:
        """主动重置 idle 计时器。

        在管道每轮迭代完成时调用，确保 Agent 即使长时间 thinking，
        只要完成了迭代就会重置定时器，避免被误判为 idle 超时。

        机制：取消当前计时器并重新创建，等同于重新开始 idle 倒计时。

        Args:
            task_id: 任务 ID
        """
        timer_manager = self._services.get("timer_manager")
        if not timer_manager:
            return

        ctx = self._contexts.get(task_id)
        if not ctx:
            return

        try:
            await timer_manager.cancel_timer(task_id)
            await timer_manager.create_timer(
                task_id=task_id,
                timeout=float(timer_manager.idle_threshold),
                callback=lambda tid=task_id: self._on_idle_timeout(tid),
            )
            logger.debug(
                "TaskWorker: idle timer 主动重置: task_id=%s",
                task_id,
            )
        except Exception as e:
            logger.warning(
                "TaskWorker: idle timer 重置失败（非致命）: "
                "task_id=%s, error=%s",
                task_id, e,
            )
