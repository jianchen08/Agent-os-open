"""
任务编排器

负责任务的依赖解析和可执行性验证：
- 订阅 task.submitted 事件
- 解析任务依赖关系
- 验证任务可执行性
- 发布 task.ready_for_scheduling 事件
- 持久化依赖等待状态（支持服务重启恢复）

与 TaskSchedulerService 的职责分离：
- TaskOrchestrator: 负责依赖解析和可执行性检查（编排层）
- TaskSchedulerService: 负责任务调度和执行触发（调度层）
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.event_bus.base import EventBusBase
from src.core.event_bus.types import EventFilter, EventType, ExecutionEvent
from src.db.models import Task
from src.db.session_manager import managed_session
from src.tasks.dependency_validator import DependencyValidator

logger = logging.getLogger(__name__)


@dataclass
class DependencyResolution:
    """
    依赖解析结果

    Attributes:
        task_id: 任务 ID
        is_resolved: 依赖是否已解析（所有依赖都已完成）
        pending_dependencies: 未完成的依赖任务 ID 列表
        completed_dependencies: 已完成的依赖任务 ID 列表
        failed_dependencies: 失败的依赖任务 ID 列表
        is_executable: 任务是否可执行
        block_reason: 阻塞原因（如果有）
    """

    task_id: str
    is_resolved: bool = False
    pending_dependencies: list[str] = field(default_factory=list)
    completed_dependencies: list[str] = field(default_factory=list)
    failed_dependencies: list[str] = field(default_factory=list)
    is_executable: bool = False
    block_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "task_id": self.task_id,
            "is_resolved": self.is_resolved,
            "pending_dependencies": self.pending_dependencies,
            "completed_dependencies": self.completed_dependencies,
            "failed_dependencies": self.failed_dependencies,
            "is_executable": self.is_executable,
            "block_reason": self.block_reason,
        }


class TaskOrchestrator:
    """
    任务编排器

    职责：
    1. 订阅 task.submitted 事件
    2. 解析任务依赖关系
    3. 验证任务可执行性
    4. 发布 task.ready_for_scheduling 事件

    工作流程：
    1. 收到 task.submitted 事件
    2. 验证依赖关系合法性（循环依赖、存在性等）
    3. 检查依赖任务状态
    4. 如果所有依赖已完成，发布 task.ready_for_scheduling
    5. 如果有依赖未完成，记录等待状态
    """

    def __init__(self, event_bus: EventBusBase):
        """
        初始化任务编排器

        Args:
            event_bus: 事件总线实例
        """
        self.event_bus = event_bus
        self.running = False
        self._subscription_ids: list[str] = []

        # 等待依赖的任务缓存
        # key: task_id, value: DependencyResolution
        self._pending_tasks: dict[str, DependencyResolution] = {}

        logger.info("[TaskOrchestrator] 任务编排器已创建")

    async def start(self) -> None:
        """启动任务编排器"""
        if self.running:
            logger.warning("[TaskOrchestrator] 编排器已在运行")
            return

        logger.info("[TaskOrchestrator] 启动任务编排器")
        self.running = True

        # 订阅任务提交事件
        sub_id = self.event_bus.subscribe(
            self._on_task_submitted,
            filter=EventFilter(event_types=[EventType.TASK_SUBMITTED]),
        )
        self._subscription_ids.append(sub_id)
        logger.info("[TaskOrchestrator] 已订阅 task.submitted 事件")

        # 订阅任务完成事件（用于处理等待中的任务）
        sub_id = self.event_bus.subscribe(
            self._on_task_completed,
            filter=EventFilter(custom_event_types=["task.completed"]),
        )
        self._subscription_ids.append(sub_id)

        # 订阅任务失败事件
        sub_id = self.event_bus.subscribe(
            self._on_task_failed,
            filter=EventFilter(custom_event_types=["task.failed"]),
        )
        self._subscription_ids.append(sub_id)

        logger.info(
            f"[TaskOrchestrator] 编排器已启动 | 订阅数={len(self._subscription_ids)}"
        )

    async def stop(self) -> None:
        """停止任务编排器"""
        if not self.running:
            return

        logger.info("[TaskOrchestrator] 停止任务编排器")
        self.running = False

        # 取消所有订阅
        for sub_id in self._subscription_ids:
            try:
                await self.event_bus.unsubscribe(sub_id)
            except Exception as e:
                logger.warning(
                    f"[TaskOrchestrator] 取消订阅失败 | sub_id={sub_id} | error={e}"
                )

        self._subscription_ids.clear()
        self._pending_tasks.clear()
        logger.info("[TaskOrchestrator] 编排器已停止")

    async def _on_task_submitted(self, event: ExecutionEvent) -> None:
        """
        处理任务提交事件

        Args:
            event: 任务提交事件
        """
        task_id = event.data.get("task_id")
        if not task_id:
            logger.warning("[TaskOrchestrator] 收到无效的任务提交事件 | 缺少 task_id")
            return

        dependencies = event.data.get("dependencies", [])
        parent_task_id = event.data.get("parent_task_id")
        target_type = event.data.get("target_type")
        target_id = event.data.get("target_id")

        logger.info(
            f"[TaskOrchestrator] 任务已提交 | task_id={task_id} | "
            f"dependencies={dependencies} | target_type={target_type}"
        )

        # BUG-FIX-fix_20260226_async_session: 修复 async_generator 错误
        # 问题根因: get_async_session() 是异步生成器，不能直接用于 async with
        # 修复方案: 使用 managed_session() 替代
        async with managed_session() as session:
            resolution = await self._resolve_dependencies(
                task_id=task_id,
                dependencies=dependencies,
                parent_task_id=parent_task_id,
                session=session,
            )

        if resolution.is_executable:
            # 任务可执行，发布 ready_for_scheduling 事件
            logger.info(
                f"[TaskOrchestrator] 任务依赖已满足，发布调度就绪事件 | task_id={task_id}"
            )
            await self._publish_ready_for_scheduling(
                task_id=task_id,
                target_type=target_type,
                target_id=target_id,
                resolution=resolution,
                event=event,
            )
        else:
            # 任务不可执行，记录等待状态并持久化
            logger.info(
                f"[TaskOrchestrator] 任务依赖未满足，等待依赖完成 | task_id={task_id} | "
                f"pending={resolution.pending_dependencies} | "
                f"reason={resolution.block_reason}"
            )
            self._pending_tasks[task_id] = resolution
            await self._persist_waiting_state(task_id, resolution)

    async def _resolve_dependencies(
        self,
        task_id: str,
        dependencies: list[str],
        parent_task_id: str | None,
        session: AsyncSession,
    ) -> DependencyResolution:
        """
        解析任务依赖关系

        Args:
            task_id: 任务 ID
            dependencies: 依赖任务 ID 列表
            parent_task_id: 父任务 ID
            session: 数据库会话

        Returns:
            依赖解析结果
        """
        # 如果没有依赖，直接返回可执行
        if not dependencies:
            return DependencyResolution(
                task_id=task_id,
                is_resolved=True,
                is_executable=True,
            )

        # 验证依赖关系合法性
        validator = DependencyValidator(session)
        validation_result = await validator.validate(
            task_id, dependencies, parent_task_id
        )

        if not validation_result.is_valid:
            logger.error(
                f"[TaskOrchestrator] 依赖验证失败 | task_id={task_id} | "
                f"errors={validation_result.errors}"
            )
            return DependencyResolution(
                task_id=task_id,
                is_resolved=False,
                is_executable=False,
                block_reason=f"依赖验证失败: {validation_result.errors}",
            )

        # 查询依赖任务状态
        result = await session.execute(
            select(Task.id, Task.status).where(Task.id.in_(dependencies))
        )
        dep_tasks = {row[0]: row[1] for row in result.fetchall()}

        # 分类依赖任务
        pending_deps = []
        completed_deps = []
        failed_deps = []

        for dep_id in dependencies:
            dep_status = dep_tasks.get(dep_id)
            if dep_status is None:
                # 依赖任务不存在（理论上已被验证器捕获）
                pending_deps.append(dep_id)
            elif dep_status == "completed":
                completed_deps.append(dep_id)
            elif dep_status in ("failed", "blocked", "cancelled"):
                failed_deps.append(dep_id)
            else:
                # pending, running 等状态
                pending_deps.append(dep_id)

        # 判断是否可执行
        is_resolved = len(pending_deps) == 0 and len(failed_deps) == 0
        is_executable = is_resolved

        block_reason = None
        if failed_deps:
            block_reason = f"依赖任务失败: {failed_deps}"
            is_executable = False
        elif pending_deps:
            block_reason = f"等待依赖任务完成: {pending_deps}"

        return DependencyResolution(
            task_id=task_id,
            is_resolved=is_resolved,
            pending_dependencies=pending_deps,
            completed_dependencies=completed_deps,
            failed_dependencies=failed_deps,
            is_executable=is_executable,
            block_reason=block_reason,
        )

    async def _publish_ready_for_scheduling(
        self,
        task_id: str,
        target_type: str | None,
        target_id: str | None,
        resolution: DependencyResolution,
        event: ExecutionEvent,
    ) -> None:
        """
        发布任务调度就绪事件

        Args:
            task_id: 任务 ID
            target_type: 目标类型
            target_id: 目标 ID
            resolution: 依赖解析结果
            event: 原始事件
        """
        try:
            ready_event = ExecutionEvent(
                event_type=EventType.TASK_READY_FOR_SCHEDULING,
                session_id=event.session_id,
                data={
                    "task_id": task_id,
                    "target_type": target_type,
                    "target_id": target_id,
                    "dependencies": resolution.completed_dependencies,
                    "resolution": resolution.to_dict(),
                    "source": "TaskOrchestrator",
                    "original_event_id": event.event_id,
                },
            )
            await self.event_bus.publish(ready_event)
            logger.info(
                f"[TaskOrchestrator] 任务调度就绪事件已发布 | task_id={task_id}"
            )
        except Exception as e:
            logger.error(
                f"[TaskOrchestrator] 发布调度就绪事件失败 | task_id={task_id} | error={e}",
                exc_info=True,
            )

    async def _on_task_completed(self, event: ExecutionEvent) -> None:
        """
        处理任务完成事件

        检查等待中的任务是否可以开始执行。

        Args:
            event: 任务完成事件
        """
        completed_task_id = event.data.get("task_id")
        if not completed_task_id:
            return

        logger.info(
            f"[TaskOrchestrator] 任务完成 | task_id={completed_task_id} | "
            f"检查等待中的任务"
        )

        # 检查等待中的任务
        tasks_to_check = list(self._pending_tasks.items())
        for task_id, resolution in tasks_to_check:
            if completed_task_id in resolution.pending_dependencies:
                logger.info(
                    f"[TaskOrchestrator] 重新检查任务依赖 | task_id={task_id} | "
                    f"completed_dep={completed_task_id}"
                )

                # BUG-FIX-fix_20260226_async_session: 修复 async_generator 错误
                async with managed_session() as session:
                    result = await session.execute(
                        select(Task).where(Task.id == task_id)
                    )
                    task = result.scalar_one_or_none()

                    if task:
                        new_resolution = await self._resolve_dependencies(
                            task_id=task_id,
                            dependencies=task.dependencies or [],
                            parent_task_id=task.parent_task_id,
                            session=session,
                        )

                        if new_resolution.is_executable:
                            # 任务可以执行了
                            del self._pending_tasks[task_id]
                            await self._clear_waiting_state(task_id)
                            await self._publish_ready_for_scheduling(
                                task_id=task_id,
                                target_type=task.target_type,
                                target_id=task.target_id,
                                resolution=new_resolution,
                                event=event,
                            )
                        else:
                            # 更新等待状态
                            self._pending_tasks[task_id] = new_resolution
                            await self._persist_waiting_state(task_id, new_resolution)

    async def _on_task_failed(self, event: ExecutionEvent) -> None:
        """
        处理任务失败事件

        标记依赖此任务的等待中任务为阻塞状态。

        Args:
            event: 任务失败事件
        """
        failed_task_id = event.data.get("task_id")
        if not failed_task_id:
            return

        logger.warning(
            f"[TaskOrchestrator] 任务失败 | task_id={failed_task_id} | "
            f"检查依赖此任务的任务"
        )

        # 检查等待中的任务
        tasks_to_block = []
        for task_id, resolution in self._pending_tasks.items():
            if failed_task_id in resolution.pending_dependencies:
                tasks_to_block.append(task_id)

        # 标记阻塞
        for task_id in tasks_to_block:
            resolution = self._pending_tasks[task_id]
            resolution.failed_dependencies.append(failed_task_id)
            resolution.pending_dependencies.remove(failed_task_id)
            resolution.is_executable = False
            resolution.block_reason = f"依赖任务失败: {resolution.failed_dependencies}"

            logger.warning(
                f"[TaskOrchestrator] 任务被阻塞 | task_id={task_id} | "
                f"failed_dep={failed_task_id}"
            )
            await self._persist_waiting_state(task_id, resolution)

    async def _persist_waiting_state(
        self,
        task_id: str,
        resolution: DependencyResolution,
    ) -> None:
        """
        持久化依赖等待状态到数据库

        将等待状态保存到 task_metadata.waiting_for_dependencies，
        支持服务重启后恢复。

        Args:
            task_id: 任务 ID
            resolution: 依赖解析结果
        """
        try:
            async with managed_session() as session:
                task = await session.get(Task, task_id)
                if task:
                    metadata = task.task_metadata or {}
                    metadata["waiting_for_dependencies"] = {
                        "pending_dependencies": resolution.pending_dependencies,
                        "completed_dependencies": resolution.completed_dependencies,
                        "failed_dependencies": resolution.failed_dependencies,
                        "blocked_reason": resolution.block_reason,
                        "started_waiting_at": datetime.now(UTC).isoformat(),
                    }
                    task.task_metadata = metadata
                    await session.commit()
                    logger.debug(
                        f"[TaskOrchestrator] 依赖等待状态已持久化 | task_id={task_id}"
                    )
        except Exception as e:
            logger.error(
                f"[TaskOrchestrator] 持久化依赖等待状态失败 | task_id={task_id} | error={e}"
            )

    async def _clear_waiting_state(self, task_id: str) -> None:
        """
        清除依赖等待状态

        当任务开始执行时，清除持久化的等待状态。

        Args:
            task_id: 任务 ID
        """
        try:
            async with managed_session() as session:
                task = await session.get(Task, task_id)
                if task:
                    metadata = task.task_metadata or {}
                    if "waiting_for_dependencies" in metadata:
                        del metadata["waiting_for_dependencies"]
                        task.task_metadata = metadata
                        await session.commit()
                        logger.debug(
                            f"[TaskOrchestrator] 依赖等待状态已清除 | task_id={task_id}"
                        )
        except Exception as e:
            logger.error(
                f"[TaskOrchestrator] 清除依赖等待状态失败 | task_id={task_id} | error={e}"
            )

    def get_pending_tasks(self) -> dict[str, dict[str, Any]]:
        """
        获取等待中的任务信息

        Returns:
            等待中的任务字典
        """
        return {
            task_id: resolution.to_dict()
            for task_id, resolution in self._pending_tasks.items()
        }

    def get_statistics(self) -> dict[str, Any]:
        """
        获取编排器统计信息

        Returns:
            统计信息字典
        """
        return {
            "running": self.running,
            "subscription_count": len(self._subscription_ids),
            "pending_tasks_count": len(self._pending_tasks),
            "pending_tasks": self.get_pending_tasks(),
        }


# 全局编排器实例
_orchestrator: TaskOrchestrator | None = None


async def get_task_orchestrator(event_bus: EventBusBase) -> TaskOrchestrator:
    """
    获取任务编排器实例

    Args:
        event_bus: 事件总线实例

    Returns:
        任务编排器实例
    """
    global _orchestrator

    if _orchestrator is None:
        _orchestrator = TaskOrchestrator(event_bus)
        await _orchestrator.start()

    return _orchestrator


async def stop_task_orchestrator() -> None:
    """停止任务编排器"""
    global _orchestrator

    if _orchestrator:
        await _orchestrator.stop()
        _orchestrator = None
