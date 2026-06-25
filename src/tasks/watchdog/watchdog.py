"""
自动执行看门狗（基于 Task 模型）

提供任务监控和自动执行功能：
- 定期扫描开启自动执行的长期任务（根任务，parent_task_id=None）
- 检测任务完成状态，自动触发下一个任务
- 处理任务超时和卡住情况
- 维护长期任务的自动执行流程
- 检测评估提醒并发送通知

注意：没有独立的 Project 数据模型，长期任务通过 Task 表的 parent_task_id=None 实现
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from src.tasks.timer_manager import get_timer_manager
from src.tasks.watchdog.components import (
    FailureHandler,
    ProjectController,
    TaskMonitor,
    TaskTrigger,
    TimeoutHandler,
)

logger = logging.getLogger(__name__)


class AutoExecuteWatchdog:
    """
    自动执行看门狗（基于 Task 模型）- 协调器

    核心职责：
    1. 协调各组件工作
    2. 提供统一的对外接口
    3. 管理组件生命周期

    组件职责分工：
    - TaskMonitor: 任务监控、项目检查、事件处理
    - TaskTrigger: 任务触发、状态转换
    - TimeoutHandler: 超时检测、卡住处理
    - FailureHandler: 失败处理、重试管理
    - ProjectController: 项目控制、状态变更

    注意：长期任务存储在 Task 表中，通过 parent_task_id=None 标识
    """

    # 默认配置
    DEFAULT_CHECK_INTERVAL = 30  # 检查间隔（秒）
    DEFAULT_TASK_TIMEOUT = 3600  # 任务超时时间（秒），默认 1 小时
    DEFAULT_STUCK_THRESHOLD = 600  # 卡住阈值（秒），超过此时间无输出则认为卡住

    def __init__(
        self,
        session_factory: Any | None = None,
        task_manager_callback: Callable | None = None,
        check_interval: int = DEFAULT_CHECK_INTERVAL,
        task_timeout: int = DEFAULT_TASK_TIMEOUT,
        stuck_threshold: int = DEFAULT_STUCK_THRESHOLD,
        notification_callback: Callable | None = None,
        evaluation_reminder_service: Any | None = None,
    ):
        """
        初始化自动执行看门狗

        Args:
            session_factory: 数据库会话工厂（可选，用于向后兼容）
            task_manager_callback: 任务管理器回调（用于启动任务，已废弃）
            check_interval: 检查间隔（秒）
            task_timeout: 任务超时时间（秒）
            stuck_threshold: 卡住阈值（秒）
            notification_callback: 通知回调函数
            evaluation_reminder_service: 评估提醒服务实例
        """
        self.session_factory = session_factory
        self.task_manager_callback = task_manager_callback
        self.check_interval = check_interval
        self.task_timeout = task_timeout
        self.stuck_threshold = stuck_threshold
        self.notification_callback = notification_callback
        self.evaluation_reminder_service = evaluation_reminder_service

        # 初始化 TimerManager
        self._timer_manager = get_timer_manager()

        # 初始化组件
        self._init_components()

        # 订阅事件
        self.monitor.subscribe_events()

    def _init_components(self) -> None:
        """初始化各组件"""
        # 创建项目控制器（无依赖）
        self.project_controller = ProjectController(
            notification_callback=self.notification_callback,
        )

        # 创建失败处理器（依赖项目控制器）
        self.failure_handler = FailureHandler(
            project_controller=self.project_controller,
        )

        # 创建超时处理器
        self.timeout_handler = TimeoutHandler(
            task_timeout=self.task_timeout,
            stuck_threshold=self.stuck_threshold,
            notification_callback=self.notification_callback,
        )

        # 创建任务触发器
        self.trigger = TaskTrigger(
            task_manager_callback=self.task_manager_callback,
            heartbeat_callback=self._update_heartbeat,
        )

        # 创建任务监控器（依赖其他所有组件）
        self.monitor = TaskMonitor(
            check_interval=self.check_interval,
            task_timeout=self.task_timeout,
            stuck_threshold=self.stuck_threshold,
            task_manager_callback=self.task_manager_callback,
            notification_callback=self.notification_callback,
            evaluation_reminder_service=self.evaluation_reminder_service,
            trigger_component=self.trigger,
            timeout_component=self.timeout_handler,
            failure_component=self.failure_handler,
            project_controller=self.project_controller,
        )

    def _update_heartbeat(self, project_id: str) -> None:
        """
        更新项目心跳（委托给监控器）

        Args:
            project_id: 项目 ID
        """
        self.monitor._update_heartbeat(project_id)

    # ==================== 对外接口 ====================

    async def start(self) -> None:
        """启动看门狗后台任务"""
        # 从数据库恢复计时器
        restored_count = await self._timer_manager.restore_from_db(
            callback=self._on_timer_timeout
        )
        if restored_count > 0:
            logger.info(f"从数据库恢复了 {restored_count} 个计时器")

        await self.monitor.start_monitoring()

    async def stop(self) -> None:
        """停止看门狗后台任务"""
        await self.monitor.stop_monitoring()
        # 清理所有计时器
        await self._timer_manager.clear_all()
        logger.info("已清理所有计时器")

    def _on_timer_timeout(self, task_id: str) -> None:
        """
        计时器超时回调

        Args:
            task_id: 任务 ID
        """
        logger.warning(f"计时器超时: task_id={task_id}")
        # 委托给 monitor 处理
        asyncio.create_task(self.monitor.handle_timer_timeout(task_id))

    async def check_projects(self) -> dict[str, Any]:
        """
        检查所有开启自动执行的长期任务（根任务）

        Returns:
            检查结果统计
        """
        return await self.monitor.check_projects()

    async def trigger_next_task(self, project_id: str) -> dict[str, Any]:
        """
        触发项目的下一个任务执行

        Args:
            project_id: 项目 ID

        Returns:
            触发结果
        """
        return await self.trigger.trigger_next_task(project_id)

    async def handle_task_timeout(
        self,
        task_id: str,
        idle_seconds: float,
    ) -> dict[str, Any]:
        """
        处理任务执行超时

        Args:
            task_id: 任务 ID
            idle_seconds: 空闲秒数

        Returns:
            处理结果
        """
        return await self.timeout_handler.handle_timeout(task_id, idle_seconds)

    async def handle_stuck_detection(
        self,
        project_id: str,
        task_id: str,
        idle_seconds: float,
    ) -> dict[str, Any]:
        """
        检测任务是否卡住（长时间无输出）

        Args:
            project_id: 项目 ID
            task_id: 任务 ID
            idle_seconds: 空闲秒数

        Returns:
            处理结果
        """
        return await self.timeout_handler.handle_stuck_detection(
            project_id, task_id, idle_seconds
        )

    def get_heartbeat_age(self, project_id: str) -> float | None:
        """
        获取项目心跳年龄

        Args:
            project_id: 项目 ID

        Returns:
            心跳年龄（秒），如果不存在返回 None
        """
        return self.monitor.get_heartbeat_age(project_id)

    async def manual_trigger(self, project_id: str) -> dict[str, Any]:
        """
        手动触发项目的下一个任务

        Args:
            project_id: 项目 ID

        Returns:
            触发结果
        """
        logger.info(f"手动触发项目 {project_id} 的下一个任务")
        return await self.trigger.trigger_next_task(project_id)

    # ==================== 配置更新接口 ====================

    def set_task_manager_callback(self, callback: Callable | None) -> None:
        """
        设置任务管理器回调

        Args:
            callback: 回调函数
        """
        self.task_manager_callback = callback
        self.trigger.set_task_manager_callback(callback)
        self.monitor.task_manager_callback = callback

    def set_notification_callback(self, callback: Callable | None) -> None:
        """
        设置通知回调

        Args:
            callback: 回调函数
        """
        self.notification_callback = callback
        self.timeout_handler.set_notification_callback(callback)
        self.project_controller.set_notification_callback(callback)
        self.monitor.notification_callback = callback

    def set_pending_tasks_callback(self, callback: Callable | None) -> None:
        """
        设置待处理任务回调

        Args:
            callback: 回调函数
        """
        self.monitor.set_pending_tasks_callback(callback)

    # ==================== 项目控制接口 ====================

    async def pause_project(self, project_id: str, reason: str) -> dict[str, Any]:
        """
        暂停项目自动执行

        Args:
            project_id: 项目 ID
            reason: 暂停原因

        Returns:
            处理结果
        """
        return await self.project_controller.pause_project(project_id, reason)

    async def resume_project(self, project_id: str) -> dict[str, Any]:
        """
        恢复项目自动执行

        Args:
            project_id: 项目 ID

        Returns:
            处理结果
        """
        return await self.project_controller.resume_project(project_id)

    async def complete_project(self, project_id: str) -> dict[str, Any]:
        """
        完成项目

        Args:
            project_id: 项目 ID

        Returns:
            处理结果
        """
        return await self.project_controller.complete_project(project_id)

    async def cancel_project(
        self,
        project_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """
        取消项目

        Args:
            project_id: 项目 ID
            reason: 取消原因

        Returns:
            处理结果
        """
        return await self.project_controller.cancel_project(project_id, reason)

    async def get_project_status(self, project_id: str) -> dict[str, Any]:
        """
        获取项目状态

        Args:
            project_id: 项目 ID

        Returns:
            项目状态信息
        """
        return await self.project_controller.get_project_status(project_id)
