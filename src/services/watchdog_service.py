"""
Watchdog 服务管理器

负责启动、管理和协调各种 Watchdog 实例。
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from src.core.di import get_global_container
from src.tasks.watchdog.watchdog import AutoExecuteWatchdog

logger = logging.getLogger(__name__)


class WatchdogServiceManager:
    """
    Watchdog 服务管理器

    统一管理任务看门狗和自动执行看门狗的生命周期。
    """

    DEFAULT_SERVICE_CHECK_INTERVAL = 5
    DEFAULT_SERVICE_CHECK_MAX_RETRIES = 12

    def __init__(self, session_factory: async_sessionmaker):
        self.session_factory = session_factory
        self.auto_execute_watchdog: AutoExecuteWatchdog | None = None
        self._started = False
        self._execution_service_ready = False
        self._pending_tasks: list[dict] = []

    async def start(self):
        """启动所有 Watchdog 服务"""
        if self._started:
            logger.warning("Watchdog 服务已启动")
            return

        try:
            self.auto_execute_watchdog = AutoExecuteWatchdog(
                session_factory=self.session_factory,
                task_manager_callback=self._task_manager_callback,
                check_interval=30,
                task_timeout=3600,
                stuck_threshold=600,
                notification_callback=self._notification_callback,
            )
            self.auto_execute_watchdog._pending_tasks_callback = self.process_pending_tasks
            await self.auto_execute_watchdog.start()

            container = get_global_container()
            if not container.has("watchdog_manager"):
                container.register_instance("auto_execute_watchdog", self.auto_execute_watchdog)
                container.register_instance("watchdog_manager", self)

            self._started = True
            logger.info("Watchdog 服务已启动")

        except Exception as e:
            logger.error(f"启动 Watchdog 服务失败: {e}")
            await self.stop()
            raise

    async def stop(self):
        """停止所有 Watchdog 服务"""
        if not self._started:
            return

        try:
            if self.auto_execute_watchdog:
                await self.auto_execute_watchdog.stop()
                self.auto_execute_watchdog = None

            self._started = False
            logger.info("Watchdog 服务已停止")

        except Exception as e:
            logger.error(f"停止 Watchdog 服务失败: {e}")

    async def _wait_for_execution_service(
        self,
        max_retries: int = DEFAULT_SERVICE_CHECK_MAX_RETRIES,
        interval: float = DEFAULT_SERVICE_CHECK_INTERVAL,
    ) -> bool:
        for attempt in range(max_retries):
            container = get_global_container()
            if container.has("execution_service"):
                self._execution_service_ready = True
                logger.info("执行服务已就绪")
                return True

            if attempt < max_retries - 1:
                logger.debug(
                    f"执行服务未就绪，等待 {interval}秒后重试 ({attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(interval)

        logger.warning(f"执行服务在 {max_retries * interval}秒内未就绪")
        return False

    async def _task_manager_callback(
        self,
        task_id: str,
        project_id: str,
        task: dict,
    ):
        logger.info(f"任务管理器回调: 启动任务 {task_id} (项目: {project_id})")

        from src.orchestration import schedule as schedule_task

        try:
            schedule_result = await schedule_task(task_id)

            if schedule_result.get("success"):
                logger.info(f"任务 {task_id} 已通过统一调度入口启动")
                return {
                    "status": "success",
                    "task_id": task_id,
                    "project_id": project_id,
                    "message": "任务已成功启动",
                }
            else:
                error = schedule_result.get("error", "未知错误")
                logger.warning(f"任务 {task_id} 启动失败: {error}")
                return {
                    "status": "failed",
                    "task_id": task_id,
                    "project_id": project_id,
                    "message": f"任务启动失败: {error}",
                    "error": error,
                }

        except Exception as e:
            logger.error(f"任务管理器回调失败: {e}")
            pending_task = {
                "task_id": task_id,
                "project_id": project_id,
                "task": task,
                "created_at": datetime.now(UTC).isoformat(),
                "retry_count": 0,
                "error": str(e),
            }
            self._pending_tasks.append(pending_task)

            logger.warning(
                f"任务 {task_id} 启动失败，已加入待处理队列，"
                f"当前队列长度: {len(self._pending_tasks)}"
            )

            return {
                "status": "failed_but_queued",
                "task_id": task_id,
                "project_id": project_id,
                "message": f"任务启动失败: {e}，已加入待处理队列",
                "error": str(e),
                "pending_queue_length": len(self._pending_tasks),
            }

    async def process_pending_tasks(self) -> dict:
        if not self._pending_tasks:
            return {"processed": 0, "succeeded": 0, "failed": 0, "remaining": 0}

        from src.orchestration import schedule as schedule_task

        processed = 0
        succeeded = 0
        failed = 0
        still_pending = []

        for pending_task in self._pending_tasks:
            processed += 1
            task_id = pending_task["task_id"]
            pending_task["project_id"]

            retry_count = pending_task.get("retry_count", 0)
            if retry_count >= 3:
                logger.warning(f"任务 {task_id} 超过最大重试次数，放弃处理")
                failed += 1
                continue

            try:
                schedule_result = await schedule_task(task_id)
                if schedule_result.get("success"):
                    logger.info(f"待处理任务 {task_id} 已通过统一调度入口启动")
                    succeeded += 1
                else:
                    error = schedule_result.get("error", "未知错误")
                    logger.error(f"待处理任务 {task_id} 启动失败: {error}")
                    pending_task["retry_count"] = retry_count + 1
                    pending_task["last_error"] = error
                    pending_task["last_retry_at"] = datetime.now(UTC).isoformat()
                    still_pending.append(pending_task)
                    failed += 1
            except Exception as e:
                logger.error(f"待处理任务 {task_id} 启动失败: {e}")
                pending_task["retry_count"] = retry_count + 1
                pending_task["last_error"] = str(e)
                pending_task["last_retry_at"] = datetime.now(UTC).isoformat()
                still_pending.append(pending_task)
                failed += 1

        self._pending_tasks = still_pending

        return {
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "remaining": len(still_pending),
        }

    def get_pending_tasks_count(self) -> int:
        return len(self._pending_tasks)

    def clear_pending_tasks(self) -> int:
        count = len(self._pending_tasks)
        self._pending_tasks.clear()
        logger.info(f"已清空待处理任务队列，共 {count} 个任务")
        return count

    async def _notification_callback(
        self, event: str, message: str, details: dict = None, **kwargs
    ):
        logger.info(f"Watchdog 通知: {event} - {message}")

        try:
            container = get_global_container()

            if container.has("notification_service"):
                notification_service = container.get("notification_service")
                await notification_service.send_watchdog_notification(
                    event=event,
                    message=message,
                    details=details or {},
                    **kwargs,
                )

            if container.has("websocket_event_service"):
                event_service = container.get("websocket_event_service")
                if event == "task_blocked":
                    await event_service.send_task_blocked(
                        user_id=kwargs.get("user_id"),
                        taskId=kwargs.get("task_id"),
                        reason=message,
                        details=details or {},
                    )
                elif event == "task_idle_reminder":
                    await event_service.send_task_idle_reminder(
                        user_id=kwargs.get("user_id"),
                        taskId=kwargs.get("task_id"),
                        message=message,
                        idleCount=details.get("idle_count", 0) if details else 0,
                    )
                # 注意：project_paused 和 project_resumed 事件已废弃，不再发送 WebSocket 通知

        except Exception as e:
            logger.error(f"通知回调失败: {e}")

    def get_auto_execute_watchdog(self) -> AutoExecuteWatchdog | None:
        return self.auto_execute_watchdog

    def is_started(self) -> bool:
        return self._started


_watchdog_manager: WatchdogServiceManager | None = None


async def initialize_watchdog_service(session_factory: async_sessionmaker):
    global _watchdog_manager

    if _watchdog_manager is not None:
        logger.warning("Watchdog 服务已初始化")
        return

    _watchdog_manager = WatchdogServiceManager(session_factory)
    await _watchdog_manager.start()


async def shutdown_watchdog_service():
    global _watchdog_manager

    if _watchdog_manager is not None:
        await _watchdog_manager.stop()
        _watchdog_manager = None


def get_watchdog_manager() -> WatchdogServiceManager | None:
    return _watchdog_manager
