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
        - 管道挂起时：唤醒并提醒（最多 idle_remind_limit 次），超限则标记 failed

        BUG-FIX-fix_20260514_active_pipeline_deadlock:
        新增活跃管道存活检测，解决管道进程异常死亡后任务永远卡在 running 的问题。

        Args:
            task_id: 超时的任务ID
        """
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
            self._active_tasks.discard(task_id)
            self._idle_remind_counts.pop(task_id, None)
            self._cancel_idle_timer_async(task_id)
            return

        idle_remind_limit = _IDLE_REMIND_LIMIT
        remind_count = self._idle_remind_counts.get(task_id, 0)

        # BUG-FIX-fix_20260514_active_pipeline_deadlock:
        # 原逻辑: 管道活跃时直接取消 timer → 管道死亡后无检测机制 → 任务永远卡在 running
        # 新逻辑: 管道活跃时通过 checkpoint 文件龄判断是否真正存活，
        #         存活则重建 timer 继续监控，死亡则标记任务 failed
        if task_id in self._active_tasks:
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

        if task_id in self._suspended_engines and remind_count < idle_remind_limit:
            self._idle_remind_counts[task_id] = remind_count + 1
            logger.info(
                "TaskWorker: idle 超时但有挂起管道，"
                "提醒 #%d: task_id=%s",
                remind_count + 1, task_id,
            )
            self._try_resume_engine(task_id)

            # BUG-FIX-fix_20260422_idle_timer_reset:
            # 提醒后重新创建计时器
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
            self._active_tasks.discard(task_id)
            self._idle_remind_counts.pop(task_id, None)
            evt = self._terminal_events.pop(task_id, None)
            if evt is not None:
                evt.set()
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
                        self._active_tasks.discard(task_id)
                        self._idle_remind_counts.pop(
                            task_id, None,
                        )
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

    def _try_resume_engine(self, task_id: str) -> None:
        """通过标记和 wake_event 请求主循环执行 resume。

        idle 超时回调是同步的，不能直接 await engine.resume()。
        旧方案通过 asyncio.create_task fire-and-forget 执行 resume，
        存在竞态和异常静默问题。新方案标记 _resume_requested 并
        直接 set wake_event，由主循环统一执行 resume，
        保证 engine 操作的串行性。

        Args:
            task_id: 挂起管道对应的任务 ID
        """
        if task_id not in self._suspended_engines:
            return

        self._resume_requested[task_id] = True
        logger.debug("TaskWorker: resume requested for task %s", task_id)

        # 直接 set wake_event 唤醒 while 循环，无需发虚假事件
        wake_evt = self._wake_events.get(task_id)
        if wake_evt is not None:
            wake_evt.set()
