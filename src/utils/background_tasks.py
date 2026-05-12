"""
后台任务管理器

提供统一的后台任务管理机制，解决 asyncio.create_task() 引用丢失问题：
- 保存 Task 对象引用，防止垃圾回收
- 自动清理已完成的任务，防止内存泄漏
- 支持任务取消和状态监控
- 提供异常处理和日志记录

使用示例：
    # 创建管理器实例
    task_manager = BackgroundTaskManager()

    # 启动后台任务
    await task_manager.start_task(
        name="my_background_task",
        coro=async_function(),
        on_complete=on_complete_callback,
        on_error=on_error_callback,
    )

    # 取消特定任务
    await task_manager.cancel_task("my_background_task")

    # 取消所有任务并清理
    await task_manager.shutdown()
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TaskInfo:
    """任务信息"""

    name: str
    task: asyncio.Task
    start_time: float = field(default_factory=time.time)
    on_complete: Callable[[asyncio.Task], None] | None = None
    on_error: Callable[[asyncio.Task, Exception], None] | None = None


class BackgroundTaskManager:
    """
    后台任务管理器

    核心职责：
    1. 统一管理后台任务的创建、追踪和清理
    2. 防止 Task 对象被垃圾回收导致的问题
    3. 提供任务异常处理和日志记录
    4. 支持服务关闭时的优雅清理

    设计原则：
    - 每个任务必须有唯一的名称标识
    - 自动清理已完成的任务，防止内存泄漏
    - 异常不会传播到调用方，而是通过回调或日志处理
    - 线程安全：所有操作都在事件循环中执行
    """

    def __init__(self, auto_cleanup_interval: float = 60.0) -> None:
        """
        初始化后台任务管理器

        Args:
            auto_cleanup_interval: 自动清理间隔（秒），默认60秒
        """
        self._tasks: dict[str, TaskInfo] = {}
        self._auto_cleanup_interval = auto_cleanup_interval
        self._cleanup_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """启动后台清理任务"""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.debug("后台任务管理器清理循环已启动")

    async def stop(self) -> None:
        """停止后台清理任务"""
        self._shutdown_event.set()
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        self._cleanup_task = None
        logger.debug("后台任务管理器清理循环已停止")

    async def shutdown(self, timeout: float = 5.0) -> None:
        """
        关闭管理器，取消所有任务

        Args:
            timeout: 等待任务取消的超时时间（秒）
        """
        logger.info(f"开始关闭后台任务管理器，当前任务数: {len(self._tasks)}")

        # 停止清理循环
        await self.stop()

        # 取消所有任务
        async with self._lock:
            tasks_to_cancel = list(self._tasks.values())

        if tasks_to_cancel:
            # 发送取消信号
            for task_info in tasks_to_cancel:
                if not task_info.task.done():
                    task_info.task.cancel()
                    logger.debug(f"已发送取消信号 | task_name={task_info.name}")

            # 等待任务完成或超时
            pending_tasks = [
                task_info.task
                for task_info in tasks_to_cancel
                if not task_info.task.done()
            ]

            if pending_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending_tasks, return_exceptions=True),
                        timeout=timeout,
                    )
                except TimeoutError:
                    logger.warning(
                        f"等待任务取消超时，强制清理 | pending_count={len(pending_tasks)}"
                    )

        # 清理所有任务引用
        async with self._lock:
            self._tasks.clear()

        logger.info("后台任务管理器已关闭")

    async def start_task(
        self,
        name: str,
        coro: Coroutine[Any, Any, Any],
        on_complete: Callable[[asyncio.Task], None] | None = None,
        on_error: Callable[[asyncio.Task, Exception], None] | None = None,
    ) -> asyncio.Task:
        """
        启动一个后台任务

        Args:
            name: 任务名称（唯一标识）
            coro: 协程对象
            on_complete: 任务完成时的回调（无论成功或失败）
            on_error: 任务异常时的回调

        Returns:
            创建的 Task 对象

        Raises:
            ValueError: 如果任务名称已存在且任务仍在运行
        """
        async with self._lock:
            # 检查是否存在同名任务
            if name in self._tasks:
                existing_task = self._tasks[name].task
                if not existing_task.done():
                    raise ValueError(f"任务 '{name}' 已在运行中")
                # 任务已完成，清理旧引用
                del self._tasks[name]

            # 创建任务
            task = asyncio.create_task(coro)
            task_info = TaskInfo(
                name=name,
                task=task,
                on_complete=on_complete,
                on_error=on_error,
            )
            self._tasks[name] = task_info

        # 添加完成回调
        task.add_done_callback(
            lambda t: asyncio.create_task(self._on_task_done(name, t))
        )

        logger.debug(f"后台任务已启动 | name={name}")
        return task

    async def cancel_task(self, name: str, wait: bool = True, timeout: float = 5.0) -> bool:
        """
        取消指定任务

        Args:
            name: 任务名称
            wait: 是否等待任务完成
            timeout: 等待超时时间（秒）

        Returns:
            是否成功取消
        """
        async with self._lock:
            if name not in self._tasks:
                logger.warning(f"尝试取消不存在的任务 | name={name}")
                return False

            task_info = self._tasks[name]
            task = task_info.task

            if task.done():
                # 任务已完成，直接清理
                del self._tasks[name]
                return True

            # 发送取消信号
            task.cancel()

        if wait:
            try:
                await asyncio.wait_for(task, timeout=timeout)
            except (TimeoutError, asyncio.CancelledError):
                pass

            async with self._lock:
                if name in self._tasks:
                    del self._tasks[name]

        logger.debug(f"任务已取消 | name={name}")
        return True

    def get_task(self, name: str) -> asyncio.Task | None:
        """
        获取指定任务的 Task 对象

        Args:
            name: 任务名称

        Returns:
            Task 对象，如果不存在则返回 None
        """
        task_info = self._tasks.get(name)
        return task_info.task if task_info else None

    def get_task_info(self, name: str) -> TaskInfo | None:
        """
        获取指定任务的详细信息

        Args:
            name: 任务名称

        Returns:
            TaskInfo 对象，如果不存在则返回 None
        """
        return self._tasks.get(name)

    def is_task_running(self, name: str) -> bool:
        """
        检查任务是否正在运行

        Args:
            name: 任务名称

        Returns:
            任务是否正在运行
        """
        task_info = self._tasks.get(name)
        return task_info is not None and not task_info.task.done()

    def list_tasks(self) -> list[dict[str, Any]]:
        """
        列出所有任务信息

        Returns:
            任务信息列表
        """
        result = []
        for name, task_info in self._tasks.items():
            task = task_info.task
            result.append({
                "name": name,
                "done": task.done(),
                "cancelled": task.cancelled() if task.done() else False,
                "start_time": task_info.start_time,
                "runtime": time.time() - task_info.start_time,
            })
        return result

    async def _on_task_done(self, name: str, task: asyncio.Task) -> None:
        """
        任务完成时的处理

        Args:
            name: 任务名称
            task: 完成的任务
        """
        async with self._lock:
            task_info = self._tasks.get(name)
            if task_info is None:
                return

        # 处理异常
        try:
            await task
        except asyncio.CancelledError:
            logger.debug(f"任务被取消 | name={name}")
        except Exception as e:
            logger.exception(f"后台任务异常 | name={name} | error={e}")

            # 调用错误回调
            if task_info.on_error:
                try:
                    task_info.on_error(task, e)
                except Exception as callback_error:
                    logger.error(f"错误回调执行失败 | name={name} | error={callback_error}")

        # 调用完成回调
        if task_info.on_complete:
            try:
                task_info.on_complete(task)
            except Exception as e:
                logger.error(f"完成回调执行失败 | name={name} | error={e}")

        # 延迟清理，允许外部在回调中访问任务信息
        asyncio.create_task(self._delayed_cleanup(name))

    async def _delayed_cleanup(self, name: str, delay: float = 1.0) -> None:
        """
        延迟清理任务引用

        Args:
            name: 任务名称
            delay: 延迟时间（秒）
        """
        await asyncio.sleep(delay)
        async with self._lock:
            if name in self._tasks:
                del self._tasks[name]
                logger.debug(f"任务引用已清理 | name={name}")

    async def _cleanup_loop(self) -> None:
        """定期清理循环"""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._auto_cleanup_interval,
                )
            except TimeoutError:
                await self._cleanup_completed_tasks()

    async def _cleanup_completed_tasks(self) -> None:
        """清理已完成的任务"""
        completed_names = []

        async with self._lock:
            for name, task_info in self._tasks.items():
                if task_info.task.done():
                    completed_names.append(name)

        # 在锁外执行清理
        for name in completed_names:
            async with self._lock:
                if name in self._tasks:
                    del self._tasks[name]
                    logger.debug(f"已清理完成任务 | name={name}")


# 全局管理器实例（用于简单的全局任务管理）
_global_task_manager: BackgroundTaskManager | None = None


def get_global_task_manager() -> BackgroundTaskManager:
    """
    获取全局后台任务管理器实例

    Returns:
        全局 BackgroundTaskManager 实例
    """
    global _global_task_manager
    if _global_task_manager is None:
        _global_task_manager = BackgroundTaskManager()
    return _global_task_manager


def reset_global_task_manager() -> None:
    """重置全局管理器（主要用于测试）"""
    global _global_task_manager
    _global_task_manager = None
