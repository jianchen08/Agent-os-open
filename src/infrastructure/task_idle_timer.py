"""任务 idle 计时器管理 Mixin。

负责 idle 超时回调、计时器重建/取消等逻辑。

idle 新语义：
    idle = (管道协程已结束) AND (无活跃子任务)
只要管道协程还活着（bg_task.done() is False），就不算 idle；
只要还有任何子任务在 pending/running/evaluating/scheduled 状态，就不算 idle。

从 task_worker.py 拆分而出，降低原文件复杂度。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class TaskIdleTimerMixin:
    """任务 idle 计时器管理混入类。

    提供 _on_idle_timeout、is_actually_idle、_arm_idle_timer、
    _cancel_idle_timer_async、_do_cancel_timer、_recreate_idle_timer_async、
    reset_idle_timer、_has_active_children 方法，
    由 TaskWorker 通过多继承组合使用。
    """

    def _on_idle_timeout(self, task_id: str) -> None:
        """idle 计时器超时回调（新语义）。

        判定流程：
          1. 任务非 running 状态 → 直接取消计时器并返回；
          2. 调用 is_actually_idle(task_id) 判定是否真正空闲：
             - 真正 idle → fail_task("idle: 管道已退出且无活跃子任务")；
             - 非真正 idle → _arm_idle_timer 重建计时器继续监控。

        不再注入 remind 消息，不再依赖 checkpoint mtime。

        Args:
            task_id: 超时的任务 ID
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
            ctx = self._contexts.get(task_id)
            if ctx:
                ctx.active = False
                ctx.cleanup(self._services.get("timer_manager"))
            self._cancel_idle_timer_async(task_id)
            return

        timer_manager = self._services.get("timer_manager")

        if self.is_actually_idle(task_id):
            # 真正 idle：管道协程已结束且无活跃子任务 → 直接失败
            try:
                threshold = (
                    getattr(timer_manager, "idle_threshold", "?")
                    if timer_manager else "?"
                )
                loop = asyncio.get_running_loop()
                loop.create_task(
                    task_service.fail_task(
                        task_id,
                        f"idle: 管道已退出且无活跃子任务 ({threshold}s)",
                    )
                )
                logger.warning(
                    "TaskWorker: 管道已退出且无活跃子任务，"
                    "标记 failed: task_id=%s", task_id,
                )
                ctx = self._contexts.get(task_id)
                if ctx:
                    ctx.set_terminal()
                    ctx.cleanup(timer_manager)
            except Exception as e:
                logger.error(
                    "TaskWorker: idle 超时 fail 处理失败: "
                    "task_id=%s, error=%s", task_id, e,
                )
            return

        # 非真正 idle：管道协程仍在跑 或 有活跃子任务 → 重建计时器继续监控
        logger.debug(
            "TaskWorker: idle 超时但非真正 idle，重建计时器继续监控: "
            "task_id=%s", task_id,
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
                logger.warning(
                    "TaskWorker: no event loop to "
                    "recreate idle timer: task_id=%s",
                    task_id,
                )

    def is_actually_idle(self, task_id: str) -> bool:
        """判定任务是否真正处于 idle 状态（新语义）。

        判定顺序：
          a) 如果 ctx.bg_task 存在且 not bg_task.done()
             → 返回 False（管道协程还活着，不算 idle）；
          b) 如果 _has_active_children(task_id) 返回 True
             → 返回 False（仍有活跃子任务，不算 idle）；
          c) 否则返回 True。

        Args:
            task_id: 任务 ID

        Returns:
            True 表示真正 idle；False 表示仍有活动迹象
        """
        ctx = self._contexts.get(task_id)
        if ctx is not None and ctx.bg_task is not None:
            if not ctx.bg_task.done():
                return False
        if self._has_active_children(task_id):
            return False
        return True

    async def _arm_idle_timer(
        self, task_id: str, timer_manager: Any,
    ) -> None:
        """统一为任务装备 idle 计时器（取消旧 + 创建新）。

        作为 reset_idle_timer / _recreate_idle_timer_async /
        task_executor._register_idle_timer 共用的底层原语，
        消除三处重复的 cancel+create 模板代码。

        取消旧计时器失败被吞掉（可能不存在）；创建新计时器失败
        将抛出异常，由调用方决定是否触发 fail_task 等副作用。

        Args:
            task_id: 任务 ID
            timer_manager: 计时器管理器实例

        Raises:
            Exception: create_timer 失败时透出
        """
        try:
            await timer_manager.cancel_timer(task_id)
        except Exception:
            pass
        await timer_manager.create_timer(
            task_id=task_id,
            timeout=float(timer_manager.idle_threshold),
            callback=lambda tid=task_id: self._on_idle_timeout(tid),
        )

    def _cancel_idle_timer_async(self, task_id: str) -> None:
        """异步取消残留的 idle 计时器（从同步回调调用）。

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
        """idle 超时非真正 idle 时异步重建计时器。

        重建前会先校验任务状态，避免在任务已终态后
        无意义地重建计时器（防止超时风暴）。底层调用
        _arm_idle_timer 完成实际的 cancel+create。

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
                            "任务已非 running: task_id=%s, "
                            "status=%s",
                            task_id, status,
                        )
                        ctx = self._contexts.get(task_id)
                        if ctx:
                            ctx.active = False
                            ctx.cleanup(timer_manager)
                        return
            await self._arm_idle_timer(task_id, timer_manager)
            logger.info(
                "TaskWorker: idle timer recreated after "
                "timeout for task %s", task_id,
            )
        except Exception as e:
            logger.warning(
                "TaskWorker: recreate idle timer failed: "
                "task_id=%s, error=%s",
                task_id, e,
            )

    def _has_active_children(self, task_id: str) -> bool:
        """检查任务是否有仍在活跃状态的子任务。

        活跃状态集合：pending / running / evaluating / scheduled。

        Args:
            task_id: 父任务 ID

        Returns:
            True 表示有活跃子任务；False 表示无活跃子任务
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
            status = (
                st.status.value
                if hasattr(st.status, "value")
                else str(st.status)
            )
            if status in active_statuses:
                return True
        return False

    async def reset_idle_timer(self, task_id: str) -> None:
        """主动重置 idle 计时器。

        在管道每轮迭代完成时调用，确保 Agent 即使长时间 thinking，
        只要完成了迭代就会重置定时器，避免被误判为 idle 超时。

        机制：取消当前计时器并重新创建，等同于重新开始 idle 倒计时。
        底层调用 _arm_idle_timer 完成实际的 cancel+create。

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
            await self._arm_idle_timer(task_id, timer_manager)
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
