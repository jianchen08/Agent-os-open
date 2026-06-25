"""
任务续执行服务

负责 Agent 多轮对话的自动续执行触发。

核心职责：
1. 检测需要续执行的任务
2. 触发任务续执行
3. 健康监控（超时、卡死检测）
4. 续执行次数统计

核心原则：
- 续执行次数可通过配置限制，默认不限制（0 表示不限制）
- 通过超时检测和卡死检测控制异常情况
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import get_settings
from src.core.event_bus import EventType, ExecutionEvent, get_event_bus
from src.core.states import ExecutionStatus
from src.db.models import Task
from src.db.session_manager import managed_session

logger = logging.getLogger(__name__)


class TaskContinuationService:
    """
    任务续执行服务

    负责 Agent 多轮对话的自动续执行触发。

    核心职责：
    1. 检测需要续执行的任务
    2. 触发任务续执行
    3. 健康监控（超时、卡死检测）
    4. 续执行次数统计

    核心原则：
    - 续执行次数可通过配置限制，默认不限制（0 表示不限制）
    - 通过超时检测和卡死检测控制异常情况
    """

    def __init__(
        self,
        task_timeout: int | None = None,
        stuck_threshold: int | None = None,
        stagnant_threshold: int | None = None,
        max_continuations: int | None = None,
        continuation_callback: Any | None = None,
    ):
        """
        初始化续执行服务

        Args:
            task_timeout: 任务超时时间（秒），默认从配置读取
            stuck_threshold: 卡住阈值（秒），默认从配置读取
            stagnant_threshold: 停滞阈值（秒），默认从配置读取
            max_continuations: 最大续执行次数，0 表示不限制，默认从配置读取
            continuation_callback: 续执行回调函数
        """
        settings = get_settings()
        self.task_timeout = task_timeout or settings.task_continuation_timeout
        self.stuck_threshold = stuck_threshold or settings.task_stuck_threshold
        self.stagnant_threshold = stagnant_threshold or settings.task_stagnant_threshold
        self.max_continuations = (
            max_continuations
            if max_continuations is not None
            else settings.task_max_continuations
        )
        self.continuation_callback = continuation_callback

    async def check_and_trigger(self) -> list[dict[str, Any]]:
        """
        检查需要续执行的任务并触发

        扫描所有 running 状态的任务，检查是否需要续执行。

        Returns:
            已触发续执行的任务列表
        """
        async with managed_session() as session:
            # 查询所有 running 状态的任务
            query = select(Task).where(
                Task.status == ExecutionStatus.RUNNING.value,
            )
            result = await session.execute(query)
            tasks = result.scalars().all()

            triggered = []
            for task in tasks:
                try:
                    # 检查健康状态
                    health = await self.check_health(task)

                    if health["action"] == "trigger_continuation":
                        # 触发续执行
                        trigger_result = await self.trigger_continuation(
                            task_id=task.id,
                            session_id=task.session_id,
                        )
                        triggered.append(trigger_result)
                    elif health["action"] == "block_task":
                        # 任务异常，进入 blocked 状态
                        await self._block_task(session, task, health["issues"])
                        triggered.append({
                            "task_id": task.id,
                            "action": "blocked",
                            "reason": health["issues"],
                        })
                except Exception as e:
                    logger.error(
                        f"[TaskContinuationService] 处理任务 {task.id} 失败: {e}"
                    )

            return triggered

    async def trigger_continuation(
        self,
        task_id: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        触发任务续执行

        Args:
            task_id: 任务 ID
            session_id: 会话 ID

        Returns:
            触发结果
        """
        async with managed_session() as session:
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

            now = datetime.now(UTC)

            # 更新续执行次数
            continuation_count = (task.continuation_count or 0) + 1

            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    continuation_count=continuation_count,
                    last_continuation_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

            logger.info(
                f"[TaskContinuationService] 触发续执行 | "
                f"task_id={task_id} | "
                f"continuation_count={continuation_count}"
            )

            # 发布续执行事件
            event_bus = get_event_bus()
            await event_bus.publish(
                ExecutionEvent(
                    event_type=EventType.CUSTOM,
                    session_id=session_id,
                    data={
                        "custom_event_type": "task.continuation_triggered",
                        "task_id": task_id,
                        "session_id": session_id,
                        "continuation_count": continuation_count,
                    },
                )
            )

            # 调用续执行回调
            if self.continuation_callback:
                try:
                    await self.continuation_callback(
                        task_id=task_id,
                        session_id=session_id,
                    )
                except Exception as e:
                    logger.error(f"[TaskContinuationService] 续执行回调失败: {e}")

            return {
                "task_id": task_id,
                "action": "continuation_triggered",
                "continuation_count": continuation_count,
                "triggered_at": now.isoformat(),
            }

    async def check_health(self, task: Task) -> dict[str, Any]:
        """
        检查任务健康状态

        检查项：
        1. 续执行次数是否达到上限
        2. 总执行时间是否超时
        3. 是否长时间无更新（停滞）
        4. 是否需要续执行

        Args:
            task: 任务对象

        Returns:
            健康状态，包含：
            - is_healthy: 是否健康
            - issues: 问题列表
            - action: 建议动作（trigger_continuation / block_task / none）
        """
        issues = []
        action = "none"
        is_healthy = True

        now = datetime.now(UTC)
        updated_at = task.updated_at or task.started_at or task.created_at

        if not updated_at:
            return {
                "is_healthy": True,
                "issues": [],
                "action": "trigger_continuation",
            }

        # 转换为 UTC 时间
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)

        idle_seconds = (now - updated_at).total_seconds()

        # 检查续执行次数是否达到上限（0 表示不限制）
        continuation_count = task.continuation_count or 0
        if self.max_continuations > 0 and continuation_count >= self.max_continuations:
            issues.append(
                f"续执行次数达到上限: {continuation_count} >= {self.max_continuations}"
            )
            is_healthy = False
            action = "block_task"
            logger.warning(
                f"[TaskContinuationService] 续执行次数达到上限 | "
                f"task_id={task.id} | "
                f"continuation_count={continuation_count}"
            )

        # 检查超时
        elif idle_seconds > self.task_timeout:
            issues.append(f"任务超时: {idle_seconds:.0f}秒 > {self.task_timeout}秒")
            is_healthy = False
            action = "block_task"
            logger.warning(
                f"[TaskContinuationService] 任务超时 | "
                f"task_id={task.id} | "
                f"idle_seconds={idle_seconds:.0f}"
            )

        # 检查停滞
        elif idle_seconds > self.stagnant_threshold:
            issues.append(f"任务停滞: {idle_seconds:.0f}秒无更新")
            is_healthy = False
            action = "trigger_continuation"
            logger.warning(
                f"[TaskContinuationService] 任务停滞 | "
                f"task_id={task.id} | "
                f"idle_seconds={idle_seconds:.0f}"
            )

        # 检查卡死（基于续执行次数和最后续执行时间）
        elif task.last_continuation_at:
            last_continuation = task.last_continuation_at
            if last_continuation.tzinfo is None:
                last_continuation = last_continuation.replace(tzinfo=UTC)

            continuation_idle = (now - last_continuation).total_seconds()

            if continuation_idle > self.stuck_threshold:
                issues.append(f"续执行后无响应: {continuation_idle:.0f}秒")
                is_healthy = False
                action = "block_task"
                logger.warning(
                    f"[TaskContinuationService] 任务卡死 | "
                    f"task_id={task.id} | "
                    f"continuation_idle={continuation_idle:.0f}"
                )

        # 正常情况，触发续执行
        else:
            action = "trigger_continuation"

        return {
            "is_healthy": is_healthy,
            "issues": issues,
            "action": action,
            "idle_seconds": idle_seconds,
            "continuation_count": continuation_count,
        }

    async def _block_task(
        self,
        session: AsyncSession,
        task: Task,
        issues: list[str],
    ) -> None:
        """
        将任务标记为阻塞状态

        Args:
            session: 数据库会话
            task: 任务对象
            issues: 问题列表
        """
        now = datetime.now(UTC)

        # 更新任务状态
        await session.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                status=ExecutionStatus.BLOCKED.value,
                updated_at=now,
            )
        )
        await session.commit()

        # 发布事件
        event_bus = get_event_bus()
        await event_bus.publish(
            ExecutionEvent(
                event_type=EventType.CUSTOM,
                session_id=None,
                data={
                    "custom_event_type": "task.blocked",
                    "task_id": task.id,
                    "reason": "续执行健康检查失败",
                    "issues": issues,
                },
            )
        )

        logger.warning(
            f"[TaskContinuationService] 任务已阻塞 | "
            f"task_id={task.id} | "
            f"issues={issues}"
        )

    async def mark_pending_continuation(
        self,
        task_id: str,
    ) -> dict[str, Any]:
        """
        标记任务为待续执行状态

        在 Agent 一轮对话结束但未调用 task_evaluate 时调用。

        Args:
            task_id: 任务 ID

        Returns:
            标记结果
        """
        async with managed_session() as session:
            result = await session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return {"error": "任务不存在", "error_code": "TASK_NOT_FOUND"}

            now = datetime.now(UTC)

            # 更新续执行计数
            continuation_count = (task.continuation_count or 0) + 1

            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    continuation_count=continuation_count,
                    last_continuation_at=now,
                    updated_at=now,
                )
            )
            await session.commit()

            logger.info(
                f"[TaskContinuationService] 标记待续执行 | "
                f"task_id={task_id} | "
                f"continuation_count={continuation_count}"
            )

            return {
                "task_id": task_id,
                "continuation_count": continuation_count,
                "marked_at": now.isoformat(),
            }
