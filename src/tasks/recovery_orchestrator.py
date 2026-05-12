"""
任务恢复编排器

负责服务启动时的任务恢复：
1. 恢复 pending 任务 - 重新发布到调度队列
2. 恢复 in_progress 任务 - 重新触发执行
3. 恢复依赖等待状态 - 恢复到 TaskOrchestrator

设计文档: docs/design/task-recovery-mechanism.md
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.core.event_bus.base import EventBusBase
from src.core.event_bus.types import EventType, ExecutionEvent
from src.db.models import Task

logger = logging.getLogger(__name__)


@dataclass
class RecoveryConfig:
    """
    恢复配置

    Attributes:
        enabled: 是否启用恢复
        lookback_window: 恢复时间窗口（秒），只恢复此时间内的任务
        restore_pending: 是否恢复 pending 任务
        restore_in_progress: 是否恢复 in_progress 任务
        restore_dependency_waiting: 是否恢复依赖等待状态
        batch_size: 恢复批次大小
        recovery_delay_ms: 恢复间隔（毫秒），避免启动时负载过高
    """

    enabled: bool = True
    lookback_window: int = 7200
    restore_pending: bool = True
    restore_in_progress: bool = True
    restore_dependency_waiting: bool = True
    batch_size: int = 100
    recovery_delay_ms: int = 100


@dataclass
class RecoveryResult:
    """
    恢复结果

    Attributes:
        pending_restored: 恢复的 pending 任务数量
        in_progress_restored: 恢复的 in_progress 任务数量
        dependency_waiting_restored: 恢复的依赖等待任务数量
        failed: 恢复失败的任务数量
        errors: 错误信息列表
        duration_ms: 恢复耗时（毫秒）
    """

    pending_restored: int = 0
    in_progress_restored: int = 0
    dependency_waiting_restored: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def total_restored(self) -> int:
        """总恢复数量"""
        return (
            self.pending_restored
            + self.in_progress_restored
            + self.dependency_waiting_restored
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "pending_restored": self.pending_restored,
            "in_progress_restored": self.in_progress_restored,
            "dependency_waiting_restored": self.dependency_waiting_restored,
            "failed": self.failed,
            "total_restored": self.total_restored(),
            "errors": self.errors,
            "duration_ms": self.duration_ms,
        }


class TaskRecoveryOrchestrator:
    """
    任务恢复编排器

    负责服务启动时的任务恢复：
    1. 恢复 pending 任务 - 重新发布到调度队列
    2. 恢复 in_progress 任务 - 重新触发执行
    3. 恢复依赖等待状态 - 恢复到 TaskOrchestrator

    Example:
        >>> orchestrator = TaskRecoveryOrchestrator(event_bus, session_factory)
        >>> result = await orchestrator.restore()
        >>> print(f"恢复了 {result.total_restored()} 个任务")
    """

    def __init__(
        self,
        event_bus: EventBusBase,
        session_factory: async_sessionmaker,
        config: RecoveryConfig | None = None,
    ):
        """
        初始化恢复编排器

        Args:
            event_bus: 事件总线
            session_factory: 数据库会话工厂
            config: 恢复配置
        """
        self.event_bus = event_bus
        self.session_factory = session_factory
        self.config = config or RecoveryConfig()

        logger.info(
            f"[TaskRecoveryOrchestrator] 初始化完成 | "
            f"enabled={self.config.enabled} | "
            f"lookback_window={self.config.lookback_window}s"
        )

    async def restore(self) -> RecoveryResult:
        """
        执行恢复流程

        恢复顺序：
        1. 先恢复依赖等待状态（确保依赖关系正确）
        2. 再恢复 pending 任务
        3. 最后恢复 in_progress 任务

        Returns:
            恢复结果统计
        """
        if not self.config.enabled:
            logger.info("[TaskRecoveryOrchestrator] 恢复已禁用，跳过")
            return RecoveryResult()

        start_time = time.time()
        result = RecoveryResult()

        logger.info("[TaskRecoveryOrchestrator] 开始执行恢复流程")

        try:
            if self.config.restore_dependency_waiting:
                result.dependency_waiting_restored = (
                    await self._restore_dependency_waiting()
                )

            if self.config.restore_pending:
                result.pending_restored = await self._restore_pending_tasks()

            if self.config.restore_in_progress:
                result.in_progress_restored = await self._restore_in_progress_tasks()

        except Exception as e:
            result.failed += 1
            result.errors.append(f"恢复流程异常: {str(e)}")
            logger.error(f"[TaskRecoveryOrchestrator] 恢复流程异常: {e}", exc_info=True)

        result.duration_ms = int((time.time() - start_time) * 1000)

        logger.info(
            f"[TaskRecoveryOrchestrator] 恢复完成 | "
            f"pending={result.pending_restored} | "
            f"in_progress={result.in_progress_restored} | "
            f"dependency_waiting={result.dependency_waiting_restored} | "
            f"failed={result.failed} | "
            f"duration={result.duration_ms}ms"
        )

        return result

    async def _restore_pending_tasks(self) -> int:
        """
        恢复 pending 状态任务

        查询条件：
        - status = 'pending'
        - created_at > now - lookback_window

        Returns:
            恢复的任务数量
        """
        lookback_time = datetime.now(UTC) - timedelta(seconds=self.config.lookback_window)
        restored = 0

        async with self.session_factory() as session:
            result = await session.execute(
                select(Task).where(
                    Task.status == "pending",
                    Task.created_at >= lookback_time,
                ).limit(self.config.batch_size)
            )
            tasks = result.scalars().all()

            logger.info(
                f"[TaskRecoveryOrchestrator] 发现 {len(tasks)} 个 pending 任务待恢复"
            )

            for task in tasks:
                try:
                    metadata = task.task_metadata or {}

                    if metadata.get("recovery", {}).get("recovered_at"):
                        logger.debug(
                            f"[TaskRecoveryOrchestrator] 跳过已恢复任务 | task_id={task.id}"
                        )
                        continue

                    await self.event_bus.publish(
                        ExecutionEvent(
                            event_type=EventType.TASK_SUBMITTED,
                            session_id=f"task_{task.id}",
                            data={
                                "task_id": task.id,
                                "target_type": task.target_type,
                                "target_id": task.target_id,
                                "dependencies": task.dependencies or [],
                                "source": "recovery",
                            },
                        )
                    )

                    await self._mark_recovered(session, task.id, "pending")
                    restored += 1

                    logger.info(
                        f"[TaskRecoveryOrchestrator] 已恢复 pending 任务 | task_id={task.id}"
                    )

                    if self.config.recovery_delay_ms > 0:
                        await asyncio.sleep(self.config.recovery_delay_ms / 1000)

                except Exception as e:
                    logger.error(
                        f"[TaskRecoveryOrchestrator] 恢复 pending 任务失败 | "
                        f"task_id={task.id} | error={e}"
                    )

        return restored

    async def _restore_in_progress_tasks(self) -> int:
        """
        恢复 in_progress 状态任务

        查询条件：
        - status = 'in_progress'
        - updated_at > now - lookback_window

        Returns:
            恢复的任务数量
        """
        lookback_time = datetime.now(UTC) - timedelta(seconds=self.config.lookback_window)
        restored = 0

        async with self.session_factory() as session:
            result = await session.execute(
                select(Task).where(
                    Task.status == "in_progress",
                    Task.updated_at >= lookback_time,
                ).limit(self.config.batch_size)
            )
            tasks = result.scalars().all()

            logger.info(
                f"[TaskRecoveryOrchestrator] 发现 {len(tasks)} 个 in_progress 任务待恢复"
            )

            for task in tasks:
                try:
                    metadata = task.task_metadata or {}

                    if metadata.get("recovery", {}).get("recovered_at"):
                        logger.debug(
                            f"[TaskRecoveryOrchestrator] 跳过已恢复任务 | task_id={task.id}"
                        )
                        continue

                    await self.event_bus.publish(
                        ExecutionEvent(
                            event_type=EventType.TASK_EXECUTION_REQUESTED,
                            session_id=f"task_{task.id}",
                            data={
                                "task_id": task.id,
                                "source": "recovery",
                            },
                        )
                    )

                    await self._mark_recovered(session, task.id, "in_progress")
                    restored += 1

                    logger.info(
                        f"[TaskRecoveryOrchestrator] 已恢复 in_progress 任务 | task_id={task.id}"
                    )

                    if self.config.recovery_delay_ms > 0:
                        await asyncio.sleep(self.config.recovery_delay_ms / 1000)

                except Exception as e:
                    logger.error(
                        f"[TaskRecoveryOrchestrator] 恢复 in_progress 任务失败 | "
                        f"task_id={task.id} | error={e}"
                    )

        return restored

    async def _restore_dependency_waiting(self) -> int:
        """
        恢复依赖等待状态

        从 task_metadata.waiting_for_dependencies 读取等待状态
        恢复到 TaskOrchestrator._pending_tasks

        Returns:
            恢复的等待任务数量
        """
        try:
            from src.orchestration.task_orchestrator import (
                DependencyResolution,
                _orchestrator,
            )

            if _orchestrator is None:
                logger.warning(
                    "[TaskRecoveryOrchestrator] TaskOrchestrator 未初始化，跳过依赖等待恢复"
                )
                return 0

            restored = 0

            async with self.session_factory() as session:
                result = await session.execute(
                    select(Task).where(
                        Task.status.in_(["pending", "in_progress"]),
                    ).limit(self.config.batch_size)
                )
                tasks = result.scalars().all()

                for task in tasks:
                    metadata = task.task_metadata or {}
                    waiting_state = metadata.get("waiting_for_dependencies")

                    if not waiting_state:
                        continue

                    resolution = DependencyResolution(
                        task_id=task.id,
                        pending_dependencies=waiting_state.get("pending_dependencies", []),
                        completed_dependencies=waiting_state.get(
                            "completed_dependencies", []
                        ),
                        failed_dependencies=waiting_state.get("failed_dependencies", []),
                        is_executable=False,
                        block_reason=waiting_state.get("blocked_reason"),
                    )

                    _orchestrator._pending_tasks[task.id] = resolution
                    restored += 1

                    logger.info(
                        f"[TaskRecoveryOrchestrator] 已恢复依赖等待状态 | task_id={task.id}"
                    )

            return restored

        except ImportError:
            logger.warning(
                "[TaskRecoveryOrchestrator] 无法导入 TaskOrchestrator，跳过依赖等待恢复"
            )
            return 0
        except Exception as e:
            logger.error(
                f"[TaskRecoveryOrchestrator] 恢复依赖等待状态失败: {e}", exc_info=True
            )
            return 0

    async def _mark_recovered(
        self,
        session: Any,
        task_id: str,
        original_status: str,
    ) -> None:
        """
        标记任务已恢复

        Args:
            session: 数据库会话
            task_id: 任务 ID
            original_status: 原始状态
        """
        now = datetime.now(UTC).isoformat()

        task = await session.get(Task, task_id)
        if task:
            metadata = task.task_metadata or {}
            recovery_info = metadata.get("recovery", {})
            recovery_count = recovery_info.get("recovery_count", 0)

            metadata["recovery"] = {
                "recovered_at": now,
                "recovery_count": recovery_count + 1,
                "original_status": original_status,
            }
            task.task_metadata = metadata
            await session.flush()


async def restore_tasks_on_startup(
    event_bus: EventBusBase,
    session_factory: async_sessionmaker,
    config: RecoveryConfig | None = None,
) -> RecoveryResult:
    """
    服务启动时执行任务恢复

    Args:
        event_bus: 事件总线
        session_factory: 数据库会话工厂
        config: 恢复配置

    Returns:
        恢复结果
    """
    orchestrator = TaskRecoveryOrchestrator(
        event_bus=event_bus,
        session_factory=session_factory,
        config=config,
    )
    return await orchestrator.restore()
