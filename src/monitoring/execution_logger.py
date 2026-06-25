"""
执行日志记录器

提供项目和任务执行过程的详细日志记录功能。
"""

import logging
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class ExecutionEventType(str, Enum):
    """执行事件类型"""

    # 项目事件
    PROJECT_CREATED = "project_created"
    PROJECT_STARTED = "project_started"
    PROJECT_PAUSED = "project_paused"
    PROJECT_RESUMED = "project_resumed"
    PROJECT_COMPLETED = "project_completed"
    PROJECT_FAILED = "project_failed"
    AUTO_EXECUTE_ENABLED = "auto_execute_enabled"
    AUTO_EXECUTE_DISABLED = "auto_execute_disabled"

    # 任务事件
    TASK_CREATED = "task_created"
    TASK_STARTED = "task_started"
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_BLOCKED = "task_blocked"
    TASK_RETRIED = "task_retried"
    TASK_TIMEOUT = "task_timeout"

    # Watchdog 事件
    WATCHDOG_TRIGGERED = "watchdog_triggered"
    WATCHDOG_RECOVERY = "watchdog_recovery"
    WATCHDOG_NOTIFICATION = "watchdog_notification"

    # 自动执行事件
    AUTO_EXECUTE_TRIGGERED = "auto_execute_triggered"
    AUTO_EXECUTE_FAILED = "auto_execute_failed"


class ExecutionLogger:
    """
    执行日志记录器

    记录项目和任务执行过程中的关键事件和状态变化。
    """

    def __init__(self, session: AsyncSession):
        """
        初始化执行日志记录器

        Args:
            session: 数据库会话
        """
        self.session = session

    async def log_event(
        self,
        event_type: ExecutionEventType,
        project_id: str | None = None,
        task_id: str | None = None,
        user_id: str | None = None,
        message: str = "",
        details: dict[str, Any] | None = None,
        level: str = "info",
    ):
        """
        记录执行事件

        Args:
            event_type: 事件类型
            project_id: 项目 ID（可选）
            task_id: 任务 ID（可选）
            user_id: 用户 ID（可选）
            message: 事件消息
            details: 事件详细信息（可选）
            level: 日志级别
        """
        timestamp = datetime.now()

        # 构建日志条目
        log_entry = {
            "timestamp": timestamp.isoformat(),
            "event_type": event_type.value,
            "project_id": project_id,
            "task_id": task_id,
            "user_id": user_id,
            "message": message,
            "details": details or {},
            "level": level,
        }

        # 记录到标准日志
        log_message = f"[{event_type.value}] {message}"
        if project_id:
            log_message += f" (项目: {project_id})"
        if task_id:
            log_message += f" (任务: {task_id})"

        if level == "error":
            logger.error(log_message)
        elif level == "warning":
            logger.warning(log_message)
        else:
            logger.info(log_message)

        # 存储到数据库（如果有执行日志表）
        try:
            await self._store_to_database(log_entry)
        except Exception as e:
            logger.error(f"存储执行日志失败: {e}")

    async def _store_to_database(self, log_entry: dict[str, Any]):
        """存储日志到数据库（待实现）"""
        pass

    # ==================== 项目事件记录 ====================

    async def log_project_created(
        self, project_id: str, user_id: str, goal: str, auto_execute: bool = False
    ):
        """记录项目创建事件"""
        await self.log_event(
            event_type=ExecutionEventType.PROJECT_CREATED,
            project_id=project_id,
            user_id=user_id,
            message=f"项目已创建: {goal}",
            details={
                "goal": goal,
                "auto_execute": auto_execute,
            },
        )

    async def log_project_started(self, project_id: str, user_id: str, task_count: int):
        """记录项目开始执行事件"""
        await self.log_event(
            event_type=ExecutionEventType.PROJECT_STARTED,
            project_id=project_id,
            user_id=user_id,
            message=f"项目开始执行，包含 {task_count} 个任务",
            details={"task_count": task_count},
        )

    async def log_project_completed(
        self, project_id: str, user_id: str, duration: float, task_count: int
    ):
        """记录项目完成事件"""
        await self.log_event(
            event_type=ExecutionEventType.PROJECT_COMPLETED,
            project_id=project_id,
            user_id=user_id,
            message=f"项目已完成，耗时 {duration:.2f} 秒，完成 {task_count} 个任务",
            details={
                "duration": duration,
                "task_count": task_count,
            },
        )

    async def log_auto_execute_toggled(
        self, project_id: str, user_id: str, enabled: bool
    ):
        """记录自动执行开关切换事件"""
        event_type = (
            ExecutionEventType.AUTO_EXECUTE_ENABLED
            if enabled
            else ExecutionEventType.AUTO_EXECUTE_DISABLED
        )
        await self.log_event(
            event_type=event_type,
            project_id=project_id,
            user_id=user_id,
            message=f"项目自动执行已{'启用' if enabled else '禁用'}",
            details={"enabled": enabled},
        )

    # ==================== 任务事件记录 ====================

    async def log_task_created(
        self, task_id: str, project_id: str, user_id: str, title: str, task_type: str
    ):
        """记录任务创建事件"""
        await self.log_event(
            event_type=ExecutionEventType.TASK_CREATED,
            project_id=project_id,
            task_id=task_id,
            user_id=user_id,
            message=f"任务已创建: {title}",
            details={
                "title": title,
                "task_type": task_type,
            },
        )

    async def log_task_started(
        self, task_id: str, project_id: str, user_id: str, auto_triggered: bool = False
    ):
        """记录任务开始执行事件"""
        trigger_type = "自动触发" if auto_triggered else "手动启动"
        await self.log_event(
            event_type=ExecutionEventType.TASK_STARTED,
            project_id=project_id,
            task_id=task_id,
            user_id=user_id,
            message=f"任务开始执行 ({trigger_type})",
            details={"auto_triggered": auto_triggered},
        )

    async def log_task_progress(
        self,
        task_id: str,
        project_id: str,
        user_id: str,
        progress_percent: float,
        passed_criteria: int,
        total_criteria: int,
    ):
        """记录任务进度更新事件"""
        await self.log_event(
            event_type=ExecutionEventType.TASK_PROGRESS,
            project_id=project_id,
            task_id=task_id,
            user_id=user_id,
            message=f"任务进度更新: {progress_percent:.1f}% ({passed_criteria}/{total_criteria})",
            details={
                "progress_percent": progress_percent,
                "passed_criteria": passed_criteria,
                "total_criteria": total_criteria,
            },
        )

    async def log_task_completed(
        self,
        task_id: str,
        project_id: str,
        user_id: str,
        duration: float,
        retry_count: int = 0,
    ):
        """记录任务完成事件"""
        message = f"任务已完成，耗时 {duration:.2f} 秒"
        if retry_count > 0:
            message += f"，重试 {retry_count} 次"

        await self.log_event(
            event_type=ExecutionEventType.TASK_COMPLETED,
            project_id=project_id,
            task_id=task_id,
            user_id=user_id,
            message=message,
            details={
                "duration": duration,
                "retry_count": retry_count,
            },
        )

    async def log_task_failed(
        self,
        task_id: str,
        project_id: str,
        user_id: str,
        error: str,
        retry_count: int = 0,
        max_retries: int = 3,
    ):
        """记录任务失败事件"""
        await self.log_event(
            event_type=ExecutionEventType.TASK_FAILED,
            project_id=project_id,
            task_id=task_id,
            user_id=user_id,
            message=f"任务执行失败: {error}",
            details={
                "error": error,
                "retry_count": retry_count,
                "max_retries": max_retries,
            },
            level="error",
        )

    async def log_task_blocked(
        self,
        task_id: str,
        project_id: str,
        user_id: str,
        reason: str,
        idle_count: int = 0,
    ):
        """记录任务阻塞事件"""
        await self.log_event(
            event_type=ExecutionEventType.TASK_BLOCKED,
            project_id=project_id,
            task_id=task_id,
            user_id=user_id,
            message=f"任务已阻塞: {reason}",
            details={
                "reason": reason,
                "idle_count": idle_count,
            },
            level="warning",
        )

    async def log_task_retried(
        self,
        task_id: str,
        project_id: str,
        user_id: str,
        retry_count: int,
        max_retries: int,
        reason: str = "",
    ):
        """记录任务重试事件"""
        await self.log_event(
            event_type=ExecutionEventType.TASK_RETRIED,
            project_id=project_id,
            task_id=task_id,
            user_id=user_id,
            message=f"任务重试 ({retry_count}/{max_retries}): {reason}",
            details={
                "retry_count": retry_count,
                "max_retries": max_retries,
                "reason": reason,
            },
            level="warning",
        )

    # ==================== Watchdog 事件记录 ====================

    async def log_watchdog_triggered(
        self, project_id: str, task_id: str, user_id: str, trigger_reason: str
    ):
        """记录 Watchdog 触发事件"""
        await self.log_event(
            event_type=ExecutionEventType.WATCHDOG_TRIGGERED,
            project_id=project_id,
            task_id=task_id,
            user_id=user_id,
            message=f"Watchdog 触发: {trigger_reason}",
            details={"trigger_reason": trigger_reason},
        )

    async def log_watchdog_recovery(
        self,
        task_id: str,
        project_id: str,
        user_id: str,
        recovery_action: str,
        idle_count: int,
    ):
        """记录 Watchdog 恢复事件"""
        await self.log_event(
            event_type=ExecutionEventType.WATCHDOG_RECOVERY,
            project_id=project_id,
            task_id=task_id,
            user_id=user_id,
            message=f"Watchdog 恢复: {recovery_action}",
            details={
                "recovery_action": recovery_action,
                "idle_count": idle_count,
            },
        )

    async def log_auto_execute_triggered(
        self,
        project_id: str,
        task_id: str,
        user_id: str,
        success: bool = True,
        error: str = "",
    ):
        """记录自动执行触发事件"""
        event_type = (
            ExecutionEventType.AUTO_EXECUTE_TRIGGERED
            if success
            else ExecutionEventType.AUTO_EXECUTE_FAILED
        )
        message = "自动执行触发成功" if success else f"自动执行触发失败: {error}"
        level = "info" if success else "error"

        await self.log_event(
            event_type=event_type,
            project_id=project_id,
            task_id=task_id,
            user_id=user_id,
            message=message,
            details={
                "success": success,
                "error": error,
            },
            level=level,
        )

    # ==================== 日志查询 ====================

    async def get_project_execution_log(
        self, project_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """获取项目执行日志（待实现）"""
        return []

    async def get_task_execution_log(
        self, task_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """获取任务执行日志（待实现）"""
        return []

    async def get_execution_summary(
        self,
        project_id: str | None = None,
        user_id: str | None = None,
        time_range: int | None = None,
    ) -> dict[str, Any]:
        """获取执行摘要（待实现）"""
        return {
            "project_id": project_id,
            "user_id": user_id,
            "time_range": time_range,
            "summary": "执行日志摘要功能待实现",
            "generated_at": datetime.now().isoformat(),
        }
