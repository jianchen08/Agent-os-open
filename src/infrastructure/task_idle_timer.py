"""任务 idle 计时器管理 Mixin。

负责 idle 超时回调、计时器重建/取消、挂起管道唤醒等逻辑。

从 task_executor.py 进一步拆分而出，降低文件复杂度。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_IDLE_REMIND_LIMIT = 3


class TaskIdleTimerMixin:
    """任务 idle 计时器管理混入类。

    提供 _on_idle_timeout、_cancel_idle_timer_async、_do_cancel_timer、
    _recreate_idle_timer_async、_try_resume_engine 方法，
    由 TaskWorker 通过多继承组合使用。
    """

    def _on_idle_timeout(self, task_id: str) -> None:
        """idle 计时器超时回调。

        idle 计时器仅在管道挂起（等待子任务）期间有意义：
        - 管道活跃时：取消计时器，不干预执行
        - 管道挂起时：唤醒并提醒（最多 _IDLE_REMIND_LIMIT 次），超限则标记 failed

        Args:
            task_id: 超时的任务ID
        """
        task_service = self._task_service
        if not task_service:
            logger.warning("TaskWorker: idle 超时但无 task_service: task_id=%s", task_id)
            return

        task = task_service.get_task(task_id)
        if task is None:
            self._cancel_idle_timer_async(task_id)
            return

        status_str = task.status if isinstance(task.status, str) else task.status.value
        if status_str != "running":
            logger.debug(
                "TaskWorker: idle 超时但任务已非 running: task_id=%s, status=%s",
                task_id, status_str,
            )
            self._active_tasks.discard(task_id)
            self._idle_remind_counts.pop(task_id, None)
            self._cancel_idle_timer_async(task_id)
            return

        # 活跃管道期间直接取消 idle 计时器（不重建）
        if task_id in self._active_tasks:
            logger.debug(
                "TaskWorker: idle 超时但管道活跃，取消计时器: task_id=%s",
                task_id,
            )
            timer_manager = self._services.get("timer_manager")
            if timer_manager:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(timer_manager.cancel_timer(task_id))
                except RuntimeError:
                    pass
            return

        remind_count = self._idle_remind_counts.get(task_id, 0)

        # 挂起管道 + 未达提醒上限 → 提醒并重建计时器
        if task_id in self._suspended_engines and remind_count < _IDLE_REMIND_LIMIT:
            self._idle_remind_counts[task_id] = remind_count + 1
            logger.info(
                "TaskWorker: idle 超时但有挂起管道，提醒 #%d: task_id=%s",
                remind_count + 1, task_id,
            )
            self._try_resume_engine(task_id)
            timer_manager = self._services.get("timer_manager")
            if timer_manager:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._recreate_idle_timer_async(task_id, timer_manager),
                    )
                except RuntimeError:
                    logger.warning(
                        "TaskWorker: no event loop to recreate idle timer: task_id=%s",
                        task_id,
                    )
            return

        # 超限或非挂起 → fail
        try:
            timer_mgr = self._services.get("timer_manager")
            threshold = (
                getattr(timer_mgr, "idle_threshold", "?")
                if timer_mgr
                else "?"
            )
            loop = asyncio.get_running_loop()
            loop.create_task(
                task_service.fail_task(
                    task_id, f"idle 超时({threshold}s无活动)",
                )
            )
            logger.warning(
                "TaskWorker: 任务 idle 超时，已标记 failed: task_id=%s",
                task_id,
            )
            self._active_tasks.discard(task_id)
            self._idle_remind_counts.pop(task_id, None)
            evt = self._terminal_events.pop(task_id, None)
            if evt is not None:
                evt.set()
        except Exception as e:
            logger.error(
                "TaskWorker: idle 超时处理失败: task_id=%s, error=%s",
                task_id, e,
            )

    def _cancel_idle_timer_async(self, task_id: str) -> None:
        """从同步回调异步取消残留的 idle 计时器。

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
                "TaskWorker: 残留 idle 计时器已取消: task_id=%s", task_id,
            )
        except Exception:
            pass

    async def _recreate_idle_timer_async(
        self, task_id: str, timer_manager: Any,
    ) -> None:
        """idle 超时提醒后异步重新创建计时器。

        重建前先检查任务状态，避免在任务已终态后无意义地重建计时器。

        Args:
            task_id: 任务 ID
            timer_manager: 计时器管理器实例
        """
        try:
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
                            "任务已非 running: task_id=%s, status=%s",
                            task_id, status,
                        )
                        self._active_tasks.discard(task_id)
                        self._idle_remind_counts.pop(task_id, None)
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
                "TaskWorker: idle timer recreated after remind for task %s",
                task_id,
            )
        except Exception as e:
            logger.warning(
                "TaskWorker: recreate idle timer failed: task_id=%s, error=%s",
                task_id, e,
            )

    def _try_resume_engine(self, task_id: str) -> None:
        """通过标记和 wake_event 请求主循环执行 resume。

        idle 超时回调是同步的，不能直接 await engine.resume()。
        标记 _resume_requested 并直接 set wake_event，
        由主循环统一执行 resume，保证 engine 操作的串行性。

        Args:
            task_id: 挂起管道对应的任务 ID
        """
        if task_id not in self._suspended_engines:
            return
        self._resume_requested[task_id] = True
        logger.debug("TaskWorker: resume requested for task %s", task_id)
        wake_evt = self._wake_events.get(task_id)
        if wake_evt is not None:
            wake_evt.set()
