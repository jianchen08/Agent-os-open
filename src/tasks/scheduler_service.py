"""
任务调度服务

管理带依赖关系的任务调度：
- 监听任务提交事件
- 检查依赖是否满足
- 触发可执行的任务
- 处理失败和重试

事件驱动改造：
- 订阅 task.submitted 事件，触发任务执行
- 订阅 task.completed/task.failed 事件，处理依赖任务
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.event_bus.base import EventBusBase
from src.core.event_bus.types import EventFilter, EventType, ExecutionEvent
from src.db.models import Task
from src.db.session_manager import managed_session
from src.tasks.dependency_validator import DependencyValidator

logger = logging.getLogger(__name__)


class TaskSchedulerService:
    """
    任务调度服务

    职责：
    1. 监听任务完成事件
    2. 检查依赖任务是否可以执行
    3. 触发可执行任务的执行
    4. 处理失败和重试逻辑
    """

    def __init__(self, event_bus: EventBusBase):
        """
        初始化调度服务

        Args:
            event_bus: 事件总线
        """
        self.event_bus = event_bus
        self.running = False
        self._subscription_ids = []

    async def start(self):
        """启动调度服务"""
        if self.running:
            logger.warning("[TaskScheduler] 调度服务已在运行")
            return

        logger.info("[TaskScheduler] 启动调度服务")
        self.running = True

        # 订阅任务提交事件（事件驱动改造）
        sub_id = self.event_bus.subscribe(
            self._on_task_submitted,
            filter=EventFilter(event_types=[EventType.TASK_SUBMITTED])
        )
        self._subscription_ids.append(sub_id)
        logger.info("[TaskScheduler] 已订阅 task.submitted 事件")

        # 订阅任务完成事件
        sub_id = self.event_bus.subscribe(self._on_task_completed)
        self._subscription_ids.append(sub_id)

        # 订阅任务失败事件
        sub_id = self.event_bus.subscribe(self._on_task_failed)
        self._subscription_ids.append(sub_id)

        logger.info(
            f"[TaskScheduler] 调度服务已启动 | 订阅数={len(self._subscription_ids)}"
        )

    async def stop(self):
        """停止调度服务"""
        if not self.running:
            return

        logger.info("[TaskScheduler] 停止调度服务")
        self.running = False

        # 取消订阅
        for sub_id in self._subscription_ids:
            try:
                await self.event_bus.unsubscribe(sub_id)
            except Exception as e:
                logger.warning(
                    f"[TaskScheduler] 取消订阅失败 | sub_id={sub_id} | error={e}"
                )

        self._subscription_ids.clear()
        logger.info("[TaskScheduler] 调度服务已停止")

    async def _on_task_submitted(self, event: ExecutionEvent):
        """
        任务提交事件处理（事件驱动改造）

        当任务提交后，检查依赖是否满足，满足则触发执行。

        Args:
            event: 任务提交事件
        """
        task_id = event.data.get("task_id")
        if not task_id:
            logger.warning("[TaskScheduler] 收到无效的任务提交事件 | 缺少 task_id")
            return

        dependencies = event.data.get("dependencies", [])
        target_type = event.data.get("target_type")
        target_id = event.data.get("target_id")

        logger.info(
            f"[TaskScheduler] 任务已提交 | task_id={task_id} | "
            f"dependencies={dependencies} | target_type={target_type} | target_id={target_id}"
        )

        # BUG-FIX-fix_20260226_async_session: 修复 async_generator 错误
        # 问题根因: get_async_session() 是异步生成器，手动调用 __anext__ 容易出错
        # 修复方案: 使用 managed_session() 上下文管理器
        async with managed_session() as session:
            can_execute = await self._can_execute_task_by_id(task_id, session)

        if can_execute:
            logger.info(f"[TaskScheduler] 任务依赖已满足，触发执行 | task_id={task_id}")
            await self._publish_execution_request(task_id)
        else:
            logger.info(f"[TaskScheduler] 任务依赖未满足，等待依赖完成 | task_id={task_id}")

    async def _publish_execution_request(self, task_id: str):
        """
        发布任务执行请求事件（事件驱动改造）

        Args:
            task_id: 任务 ID
        """
        try:
            await self.event_bus.publish(
                ExecutionEvent(
                    event_type=EventType.TASK_EXECUTION_REQUESTED,
                    session_id=f"task_{task_id}",
                    data={
                        "task_id": task_id,
                        "source": "TaskSchedulerService",
                        "requested_at": asyncio.get_event_loop().time(),
                    },
                )
            )
            logger.info(
                f"[TaskScheduler] 任务执行请求已发布 | task_id={task_id}"
            )
        except Exception as e:
            logger.error(
                f"[TaskScheduler] 发布执行请求失败 | task_id={task_id} | error={str(e)}",
                exc_info=True,
            )

    async def _can_execute_task_by_id(
        self, task_id: str, session: AsyncSession
    ) -> bool:
        """
        根据任务 ID 检查任务是否可以执行

        Args:
            task_id: 任务 ID
            session: 数据库会话

        Returns:
            是否可以执行
        """
        # 查询任务
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()

        if not task:
            logger.warning(f"[TaskScheduler] 任务不存在 | task_id={task_id}")
            return False

        return await self._can_execute_task(task, session)

    async def _on_task_completed(self, event: ExecutionEvent):
        """
        任务完成事件处理

        Args:
            event: 任务完成事件
        """
        # 检查是否是任务完成事件
        custom_event_type = event.data.get("custom_event_type")
        if custom_event_type != "task.completed":
            return

        task_id = event.data.get("task_id")
        if not task_id:
            logger.warning("[TaskScheduler] 收到无效的任务完成事件 | 缺少 task_id")
            return

        logger.info(f"[TaskScheduler] 任务完成 | task_id={task_id}")

        # 检查是否有子任务可以开始执行
        await self._schedule_dependent_tasks(task_id)

    async def _on_task_failed(self, event: ExecutionEvent):
        """
        任务失败事件处理

        Args:
            event: 任务失败事件
        """
        # 检查是否是任务失败事件
        custom_event_type = event.data.get("custom_event_type")
        if custom_event_type != "task.failed":
            return

        task_id = event.data.get("task_id")
        if not task_id:
            logger.warning("[TaskScheduler] 收到无效的任务失败事件 | 缺少 task_id")
            return

        error = event.data.get("error", "Unknown error")
        logger.warning(f"[TaskScheduler] 任务失败 | task_id={task_id} | error={error}")

        # 检查是否有依赖此任务的其他任务
        await self._handle_dependent_tasks_failure(task_id)

    async def _schedule_dependent_tasks(self, completed_task_id: str):
        """
        调度依赖任务

        当一个任务完成后，检查是否有其他任务依赖此任务，
        如果依赖都已满足，则触发执行。

        Args:
            completed_task_id: 已完成的任务 ID
        """
        # BUG-FIX-fix_20260226_async_session: 修复 async_generator 错误
        async with managed_session() as session:
            try:
                # 查找所有依赖此任务的任务
                result = await session.execute(
                    select(Task).where(
                        Task.parent_task_id.isnot(None),  # 只考虑子任务
                        Task.status == "pending",  # 只考虑待执行的任务
                    )
                )
                dependent_tasks = result.scalars().all()

                if not dependent_tasks:
                    logger.debug(
                        f"[TaskScheduler] 没有找到依赖任务 | completed_task_id={completed_task_id}"
                    )
                    return

                logger.info(
                    f"[TaskScheduler] 找到 {len(dependent_tasks)} 个待检查的依赖任务"
                )

                # 检查每个依赖任务是否可以执行
                for task in dependent_tasks:
                    if await self._can_execute_task(task, session):
                        logger.info(f"[TaskScheduler] 触发任务执行 | task_id={task.id}")
                        await self._execute_task(task.id)
                    else:
                        logger.debug(
                            f"[TaskScheduler] 任务依赖未满足 | task_id={task.id}"
                        )

            except Exception as e:
                logger.error(
                    f"[TaskScheduler] 调度依赖任务失败 | error={str(e)}",
                    exc_info=True,
                )

    async def _can_execute_task(self, task: Task, session: AsyncSession) -> bool:
        """
        检查任务是否可以执行

        Args:
            task: 任务对象
            session: 数据库会话

        Returns:
            是否可以执行
        """
        # 检查依赖关系
        if not task.dependencies:
            # 无依赖，可以执行
            return True

        # 查询所有依赖任务的状态
        dep_ids = task.dependencies or []
        if not dep_ids:
            return True

        result = await session.execute(
            select(Task.id, Task.status).where(Task.id.in_(dep_ids))
        )
        dep_tasks = {row[0]: row[1] for row in result.fetchall()}

        # 检查是否所有依赖都已完成
        for dep_id in dep_ids:
            dep_status = dep_tasks.get(dep_id)
            if dep_status != "completed":
                # 依赖未完成，不能执行
                logger.debug(
                    f"[TaskScheduler] 依赖未完成 | task_id={task.id} | "
                    f"dep_id={dep_id} | dep_status={dep_status}"
                )
                return False

        # 所有依赖都已完成，可以执行
        return True

    async def _execute_task(self, task_id: str):
        """
        执行任务

        使用统一的执行入口 TaskService，避免重复执行问题。
        通过触发事件让 TaskService 执行实际的任务。

        Args:
            task_id: 任务 ID
        """
        try:
            logger.info(
                f"[TaskScheduler] 请求执行任务 | task_id={task_id} | "
                f"使用统一执行入口"
            )

            # 发布任务执行请求事件
            # 由订阅者（如 watchdog 或 TaskService）实际执行
            from src.core.event_bus.types import EventType

            await self.event_bus.publish(
                ExecutionEvent(
                    event_type=EventType.CUSTOM,
                    session_id=task_id,  # 使用 task_id 作为 session_id
                    data={
                        "custom_event_type": "task.execution_requested",
                        "task_id": task_id,
                        "source": "TaskSchedulerService",
                        "requested_at": asyncio.get_event_loop().time(),
                    },
                )
            )

            logger.info(
                f"[TaskScheduler] 任务执行请求已发布 | task_id={task_id}"
            )

        except Exception as e:
            logger.error(
                f"[TaskScheduler] 请求任务执行失败 | task_id={task_id} | error={str(e)}",
                exc_info=True,
            )

    async def _handle_dependent_tasks_failure(self, failed_task_id: str):
        """
        处理依赖任务失败

        当一个任务失败时，标记所有依赖此任务的任务为失败。

        Args:
            failed_task_id: 失败的任务 ID
        """
        # BUG-FIX-fix_20260226_async_session: 修复 async_generator 错误
        async with managed_session() as session:
            try:
                # 查找所有依赖此任务的任务
                result = await session.execute(
                    select(Task).where(
                        Task.parent_task_id.isnot(None),
                        Task.status == "pending",
                    )
                )
                dependent_tasks = result.scalars().all()

                for task in dependent_tasks:
                    if failed_task_id in (task.dependencies or []):
                        # 标记为失败
                        task.status = "blocked"
                        task.task_metadata = task.task_metadata or {}
                        task.task_metadata["block_reason"] = (
                            f"依赖任务失败: {failed_task_id}"
                        )

                        logger.warning(
                            f"[TaskScheduler] 任务被阻塞 | task_id={task.id} | "
                            f"reason=依赖任务失败 {failed_task_id}"
                        )

                await session.commit()

            except Exception as e:
                await session.rollback()
                logger.error(
                    f"[TaskScheduler] 处理依赖任务失败失败 | error={str(e)}",
                    exc_info=True,
                )

    async def validate_and_schedule(
        self,
        task_id: str,
        dependencies: list[str],
        parent_task_id: str | None = None,
    ) -> bool:
        """
        验证依赖并尝试调度任务

        Args:
            task_id: 任务 ID
            dependencies: 依赖任务 ID 列表
            parent_task_id: 父任务 ID

        Returns:
            是否已触发执行
        """
        # BUG-FIX-fix_20260226_async_session: 修复 async_generator 错误
        async with managed_session() as session:
            # 验证依赖关系
            validator = DependencyValidator(session)
            validation_result = await validator.validate(
                task_id, dependencies, parent_task_id
            )

            if not validation_result.is_valid:
                logger.error(
                    f"[TaskScheduler] 依赖验证失败 | task_id={task_id} | "
                    f"errors={validation_result.errors}"
                )
                return False

            # 检查是否可以立即执行
            if not dependencies:
                # 无依赖，立即执行
                await self._execute_task(task_id)
                return True

            # 有依赖，等待依赖完成后再执行
            return False


# 全局调度服务实例
_scheduler_service: TaskSchedulerService | None = None


async def get_scheduler_service(event_bus: EventBusBase) -> TaskSchedulerService:
    """
    获取调度服务实例

    Args:
        event_bus: 事件总线

    Returns:
        调度服务实例
    """
    global _scheduler_service

    if _scheduler_service is None:
        _scheduler_service = TaskSchedulerService(event_bus)
        await _scheduler_service.start()

    return _scheduler_service
