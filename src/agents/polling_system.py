"""
轮询机制任务结果获取系统

提供基于轮询的任务结果查询，适用于不支持 WebSocket 的场景
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from src.core.states import ExecutionStatus

logger = logging.getLogger(__name__)


@dataclass
class TaskResult:
    """任务结果"""

    task_id: str
    status: ExecutionStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: Any | None = None
    error: str | None = None
    progress_percentage: float = 0.0
    current_step: str = ""
    total_steps: int = 0
    step_index: int = 0
    metadata: dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    @property
    def duration_seconds(self) -> float | None:
        """执行时长（秒）"""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def is_finished(self) -> bool:
        """是否已完成（成功或失败）"""
        return self.status.is_terminal

    @property
    def is_execution_finished(self) -> bool:
        """
        是否执行完成（不等于任务完成）

        执行完成包括：EVALUATING, COMPLETED, FAILED, CANCELLED, TIMEOUT
        这些状态表示执行器已完成工作，可以返回结果。
        """
        return self.status in {
            ExecutionStatus.EVALUATING,
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.TIMEOUT,
        }


class PollingTaskManager:
    """轮询任务管理器"""

    def __init__(self, cleanup_interval: int = 3600):  # 1小时清理一次
        self.tasks: dict[str, TaskResult] = {}
        self.cleanup_interval = cleanup_interval
        self.max_task_age = timedelta(hours=24)  # 任务结果保留24小时

        # 启动清理任务
        asyncio.create_task(self._cleanup_loop())

    def create_task(self, task_id: str, metadata: dict[str, Any] = None) -> TaskResult:
        """创建任务记录"""
        task_result = TaskResult(
            task_id=task_id,
            status=ExecutionStatus.PENDING,
            created_at=datetime.now(),
            metadata=metadata or {},
        )
        self.tasks[task_id] = task_result

        logger.info(f"[PollingTask] 创建任务记录 | task_id={task_id}")
        return task_result

    def start_task(self, task_id: str, total_steps: int = 0) -> bool:
        """开始执行任务"""
        if task_id not in self.tasks:
            return False

        task = self.tasks[task_id]
        task.status = ExecutionStatus.RUNNING
        task.started_at = datetime.now()
        task.total_steps = total_steps

        logger.info(
            f"[PollingTask] 任务开始执行 | task_id={task_id} | total_steps={total_steps}"
        )
        return True

    def update_progress(
        self,
        task_id: str,
        step_index: int,
        current_step: str,
        progress_percentage: float = None,
    ):
        """更新任务进度"""
        if task_id not in self.tasks:
            return False

        task = self.tasks[task_id]
        task.step_index = step_index
        task.current_step = current_step

        if progress_percentage is not None:
            task.progress_percentage = progress_percentage
        elif task.total_steps > 0:
            task.progress_percentage = (step_index / task.total_steps) * 100

        logger.debug(
            f"[PollingTask] 更新进度 | task_id={task_id} | step={current_step} | progress={task.progress_percentage:.1f}%"
        )
        return True

    def complete_task(
        self, task_id: str, success: bool, result: Any = None, error: str = None
    ):
        """
        完成任务执行

        核心原则：执行器不处理状态转换
        - 只记录执行结果，不设置任何状态
        - 状态转换完全由 should_continue 机制和评估服务处理
        - 客户端可以通过 result 和 error 判断执行情况
        """
        if task_id not in self.tasks:
            return False

        task = self.tasks[task_id]
        # 不设置状态，只记录结果
        # 状态由 should_continue 机制和评估服务处理
        task.completed_at = datetime.now()
        task.result = result
        task.error = error
        task.progress_percentage = 100.0 if success else task.progress_percentage

        logger.info(
            f"[PollingTask] 任务执行完成 | task_id={task_id} | success={success} | 状态由 should_continue 机制处理"
        )
        return True

    def get_task(self, task_id: str) -> TaskResult | None:
        """获取任务结果"""
        return self.tasks.get(task_id)

    def get_task_status(self, task_id: str) -> ExecutionStatus | None:
        """获取任务状态"""
        task = self.tasks.get(task_id)
        return task.status if task else None

    def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        if task_id not in self.tasks:
            return False

        task = self.tasks[task_id]
        if task.status in [ExecutionStatus.PENDING, ExecutionStatus.RUNNING]:
            task.status = ExecutionStatus.CANCELLED
            task.completed_at = datetime.now()
            logger.info(f"[PollingTask] 任务已取消 | task_id={task_id}")
            return True

        return False

    def list_tasks(self, status_filter: ExecutionStatus | None = None) -> list[TaskResult]:
        """列出任务"""
        tasks = list(self.tasks.values())
        if status_filter:
            tasks = [t for t in tasks if t.status == status_filter]
        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    async def _cleanup_loop(self):
        """清理过期任务的循环"""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._cleanup_old_tasks()
            except Exception as e:
                logger.error(f"[PollingTask] 清理任务失败: {e}")

    async def _cleanup_old_tasks(self):
        """清理过期任务"""
        now = datetime.now()
        expired_tasks = []

        for task_id, task in self.tasks.items():
            # 清理已完成且超过保留时间的任务
            if task.is_finished and (now - task.completed_at) > self.max_task_age:
                expired_tasks.append(task_id)
            # 清理创建时间过久但未开始的任务
            elif task.status == ExecutionStatus.PENDING and (
                now - task.created_at
            ) > timedelta(hours=1):
                expired_tasks.append(task_id)

        for task_id in expired_tasks:
            del self.tasks[task_id]

        if expired_tasks:
            logger.info(f"[PollingTask] 清理过期任务 | count={len(expired_tasks)}")


class PollingClient:
    """轮询客户端"""

    def __init__(self, task_manager: PollingTaskManager, default_interval: float = 2.0):
        self.task_manager = task_manager
        self.default_interval = default_interval

    async def wait_for_completion(
        self,
        task_id: str,
        timeout: float = 300.0,  # 5分钟超时
        interval: float = None,
        progress_callback: Callable[[TaskResult], None] = None,
    ) -> TaskResult:
        """
        等待任务执行完成

        注意：执行完成 ≠ 任务完成
        - 执行完成：执行器已完成工作，返回 EVALUATING 或终态
        - 任务完成：评估服务确认任务完成，返回 COMPLETED

        Args:
            task_id: 任务ID
            timeout: 超时时间（秒）
            interval: 轮询间隔（秒）
            progress_callback: 进度回调函数

        Returns:
            任务结果

        Raises:
            TimeoutError: 超时
            ValueError: 任务不存在
        """
        if interval is None:
            interval = self.default_interval

        start_time = datetime.now()
        last_progress = -1

        while True:
            task = self.task_manager.get_task(task_id)
            if not task:
                raise ValueError(f"任务不存在: {task_id}")

            # 调用进度回调
            if progress_callback and task.progress_percentage != last_progress:
                progress_callback(task)
                last_progress = task.progress_percentage

            # 检查是否执行完成（包括 EVALUATING 状态）
            if task.is_execution_finished:
                return task

            # 检查超时
            elapsed = (datetime.now() - start_time).total_seconds()
            if elapsed >= timeout:
                # 标记任务为超时
                self.task_manager.complete_task(task_id, False, error="等待超时")
                raise TimeoutError(f"等待任务完成超时: {task_id}")

            # 等待下次轮询
            await asyncio.sleep(interval)

    async def poll_until_condition(
        self,
        task_id: str,
        condition: Callable[[TaskResult], bool],
        timeout: float = 60.0,
        interval: float = None,
    ) -> TaskResult:
        """
        轮询直到满足条件

        Args:
            task_id: 任务ID
            condition: 条件函数
            timeout: 超时时间
            interval: 轮询间隔

        Returns:
            满足条件时的任务结果
        """
        if interval is None:
            interval = self.default_interval

        start_time = datetime.now()

        while True:
            task = self.task_manager.get_task(task_id)
            if not task:
                raise ValueError(f"任务不存在: {task_id}")

            if condition(task):
                return task

            # 检查超时
            elapsed = (datetime.now() - start_time).total_seconds()
            if elapsed >= timeout:
                raise TimeoutError(f"轮询超时: {task_id}")

            await asyncio.sleep(interval)


# 全局轮询任务管理器
polling_task_manager = PollingTaskManager()


# 装饰器：自动管理轮询任务
def polling_task(task_id_key: str = "task_id"):
    """
    装饰器：自动管理轮询任务

    Args:
        task_id_key: 从函数参数中获取 task_id 的键名
    """

    def decorator(func):
        async def wrapper(*args, **kwargs):
            # 获取 task_id
            task_id = kwargs.get(task_id_key)
            if not task_id and args:
                task_id = getattr(args[0], task_id_key, None)

            if task_id:
                # 创建任务记录
                polling_task_manager.create_task(task_id, {"function": func.__name__})
                polling_task_manager.start_task(task_id)

                try:
                    result = await func(*args, **kwargs)
                    polling_task_manager.complete_task(task_id, True, result)
                    return result
                except Exception as e:
                    polling_task_manager.complete_task(task_id, False, error=str(e))
                    raise
            else:
                return await func(*args, **kwargs)

        return wrapper

    return decorator
