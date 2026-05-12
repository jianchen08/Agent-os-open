"""
任务监控器组件

负责监控任务执行状态、检查项目进度、处理准备任务等核心监控功能。
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.event_bus import EventType, ExecutionEvent, get_event_bus
from src.core.states import ExecutionStatus
from src.db.models import Task
from src.db.session_manager import managed_session
from src.tasks.timer_manager import get_timer_manager
from src.tasks.watchdog.components.failure_handler import FailureReason

logger = logging.getLogger(__name__)


class TaskMonitor:
    """
    任务监控器

    核心职责：
    1. 定期扫描开启自动执行的长期任务（根任务）
    2. 检测任务完成状态
    3. 处理准备任务和根任务创建
    4. 监控任务健康状态

    Attributes:
        check_interval: 检查间隔（秒）
        task_timeout: 任务超时时间（秒）
        stuck_threshold: 卡住阈值（秒）
    """

    # 默认配置
    DEFAULT_CHECK_INTERVAL = 30  # 检查间隔（秒）
    DEFAULT_TASK_TIMEOUT = 3600  # 任务超时时间（秒），默认 1 小时
    DEFAULT_STUCK_THRESHOLD = 600  # 卡住阈值（秒），超过此时间无输出则认为卡住

    def __init__(
        self,
        check_interval: int = DEFAULT_CHECK_INTERVAL,
        task_timeout: int = DEFAULT_TASK_TIMEOUT,
        stuck_threshold: int = DEFAULT_STUCK_THRESHOLD,
        task_manager_callback: Callable | None = None,
        notification_callback: Callable | None = None,
        evaluation_reminder_service: Any | None = None,
        trigger_component: Any | None = None,
        timeout_component: Any | None = None,
        failure_component: Any | None = None,
        project_controller: Any | None = None,
        timer_timeout_callback: Callable[[str], None] | None = None,
    ):
        """
        初始化任务监控器

        Args:
            check_interval: 检查间隔（秒）
            task_timeout: 任务超时时间（秒）
            stuck_threshold: 卡住阈值（秒）
            task_manager_callback: 任务管理器回调（用于启动任务）
            notification_callback: 通知回调函数
            evaluation_reminder_service: 评估提醒服务实例
            trigger_component: 任务触发器组件
            timeout_component: 超时处理器组件
            failure_component: 失败处理器组件
            project_controller: 项目控制器组件
            timer_timeout_callback: 计时器超时回调函数
        """
        self.check_interval = check_interval
        self.task_timeout = task_timeout
        self.stuck_threshold = stuck_threshold
        self.task_manager_callback = task_manager_callback
        self.notification_callback = notification_callback
        self.evaluation_reminder_service = evaluation_reminder_service

        # 组件依赖
        self.trigger_component = trigger_component
        self.timeout_component = timeout_component
        self.failure_component = failure_component
        self.project_controller = project_controller

        # TimerManager 集成
        self._timer_manager = get_timer_manager()
        self._timer_timeout_callback = timer_timeout_callback

        # 运行状态
        self._running = False
        self._task: asyncio.Task | None = None
        self._pending_tasks_callback: Callable | None = None

        # 心跳记录 {project_id: last_heartbeat_time}（保留用于向后兼容）
        self._heartbeats: dict[str, float] = {}

        # 事件总线
        self._event_bus = get_event_bus()

    def set_components(
        self,
        trigger_component: Any,
        timeout_component: Any,
        failure_component: Any,
        project_controller: Any,
    ):
        """
        设置组件依赖

        Args:
            trigger_component: 任务触发器组件
            timeout_component: 超时处理器组件
            failure_component: 失败处理器组件
            project_controller: 项目控制器组件
        """
        self.trigger_component = trigger_component
        self.timeout_component = timeout_component
        self.failure_component = failure_component
        self.project_controller = project_controller

    def set_pending_tasks_callback(self, callback: Callable | None):
        """
        设置待处理任务回调

        Args:
            callback: 回调函数
        """
        self._pending_tasks_callback = callback

    async def start_monitoring(self) -> None:
        """启动监控循环"""
        if self._running:
            logger.warning("任务监控器已在运行")
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(
            f"任务监控器已启动，"
            f"检查间隔: {self.check_interval}s，"
            f"任务超时: {self.task_timeout}s，"
            f"卡住阈值: {self.stuck_threshold}s"
        )

    async def stop_monitoring(self) -> None:
        """停止监控循环"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._heartbeats.clear()
        logger.info("任务监控器已停止")

    async def _monitor_loop(self) -> None:
        """监控循环"""
        while self._running:
            try:
                await self.check_projects()
            except Exception as e:
                logger.exception(f"检查项目时出错: {e}")

            # 如果设置了处理待处理任务的回调，调用它
            if self._pending_tasks_callback:
                try:
                    await self._pending_tasks_callback()
                except Exception as e:
                    logger.error(f"处理待处理任务时出错: {e}")

            # 检查评估提醒
            if self.evaluation_reminder_service:
                try:
                    reminded_tasks = await self.evaluation_reminder_service.check_and_remind()
                    if reminded_tasks:
                        logger.info(f"已发送 {len(reminded_tasks)} 个评估提醒")
                except Exception as e:
                    logger.error(f"检查评估提醒时出错: {e}")

            await asyncio.sleep(self.check_interval)

    async def check_projects(self) -> dict[str, Any]:
        """
        检查所有开启自动执行的长期任务（根任务）

        Returns:
            检查结果统计
        """
        async with managed_session() as session:
            # 1. 检查是否有待处理的长期任务准备任务（需要创建根任务）
            await self._check_pending_preparation_tasks(session)

            # 2. 查询所有开启自动执行且状态为 running 或 planning 的根任务
            from sqlalchemy import String, cast

            query = select(Task).where(
                and_(
                    Task.parent_task_id.is_(None),  # 根任务
                    cast(Task.task_metadata["auto_execute"], String) == "true",
                    Task.status.in_(["running", "planning"]),
                )
            )
            result = await session.execute(query)
            root_tasks = result.scalars().all()

            if not root_tasks:
                return {"checked": 0, "triggered": 0, "projects": []}

            logger.info(f"检查 {len(root_tasks)} 个自动执行项目")

            triggered_count = 0
            project_results = []

            for root_task in root_tasks:
                try:
                    result = await self._check_project(session, root_task)
                    project_results.append(result)

                    if result.get("task_triggered"):
                        triggered_count += 1
                except Exception as e:
                    logger.error(f"检查项目 {root_task.id} 时出错: {e}")
                    project_results.append(
                        {
                            "project_id": root_task.id,
                            "error": str(e),
                        }
                    )

            return {
                "checked": len(root_tasks),
                "triggered": triggered_count,
                "projects": project_results,
            }

    async def _check_pending_preparation_tasks(self, session: AsyncSession) -> None:
        """
        检查待处理的长期任务准备任务，为它们创建根任务

        Args:
            session: 数据库会话
        """
        from sqlalchemy import String, cast

        query = select(Task).where(
            and_(
                Task.parent_task_id.is_(None),  # 还没有父任务
                cast(Task.task_metadata["is_long_term_preparation"], String) == "true",
                Task.status == "pending",  # 待执行状态
            )
        )
        result = await session.execute(query)
        prep_tasks = result.scalars().all()

        for prep_task in prep_tasks:
            try:
                logger.info(f"检测到长期任务准备任务 {prep_task.id}，创建根任务")
                await self._create_root_task_for_preparation(session, prep_task)
            except Exception as e:
                logger.error(
                    f"为准备任务 {prep_task.id} 创建根任务失败: {e}", exc_info=True
                )

    async def _create_root_task_for_preparation(
        self,
        session: AsyncSession,
        prep_task: Task,
    ) -> None:
        """
        为准备任务创建根任务

        Args:
            session: 数据库会话
            prep_task: 准备任务对象
        """
        from src.utils.id_encoder import generate_nested_id
        from src.utils.sequence_manager import get_next_sequence

        # 从准备任务的 metadata 中获取原始目标
        task_metadata = prep_task.task_metadata or {}
        original_goal = task_metadata.get("original_goal", prep_task.goal)

        # 生成根任务 ID
        parent_execution_record_id = prep_task.execution_record_id
        if parent_execution_record_id:
            # 如果准备任务有执行记录ID，基于它生成根任务ID
            # 将 exec-xxx-1 转换为 exec-xxx（去掉最后的序列号）
            parts = parent_execution_record_id.rsplit("-", 1)
            if len(parts) == 2:
                parent_execution_record_id = parts[0]

        root_sequence = await get_next_sequence(parent_execution_record_id)
        root_execution_record_id = generate_nested_id(
            parent_id=parent_execution_record_id,
            sequence=root_sequence,
            prefix="exec",
        )
        # 从执行记录ID生成任务ID（支持 exec- 和 thread- 前缀）
        if root_execution_record_id.startswith("exec-"):
            root_task_id = root_execution_record_id.replace("exec-", "task-", 1)
        elif root_execution_record_id.startswith("thread-"):
            root_task_id = root_execution_record_id.replace("thread-", "task-", 1)
        else:
            root_task_id = root_execution_record_id

        logger.info(f"[Watchdog] 生成根任务ID | root_task_id={root_task_id}")

        # 创建根任务
        now = datetime.now(UTC)

        root_task_data = {
            "id": root_task_id,
            "title": (
                original_goal.get("title")
                if isinstance(original_goal, dict)
                else str(original_goal)
            ),
            "description": (
                original_goal.get("description")
                if isinstance(original_goal, dict)
                else None
            ),
            "status": "planning",
            "parent_task_id": None,  # 根任务
            "session_id": prep_task.session_id,
            "priority": prep_task.priority,
            "user_id": prep_task.user_id,
            "goal": original_goal,
            "evaluation_metric_ids": [],  # 初始为空，准备任务完成后更新
            "execution_record_id": root_execution_record_id,
            "target_type": prep_task.target_type,
            "target_id": prep_task.target_id,
            "target_name": prep_task.target_name,
            "total_criteria": 0,
            "passed_criteria": 0,
            "failed_criteria": 0,
            "progress_percent": 0.0,
            "retry_count": 0,
            "max_retries": prep_task.max_retries,
            "task_metadata": {
                "task_type": "long_term",
                "task_scope": "long_term",
                "auto_execute": True,  # 启用自动执行
                "created_by_watchdog": True,
                "preparation_task_id": prep_task.id,
                "submitted_at": now.isoformat(),
            },
        }

        # 创建根任务
        from src.db.models import Task as TaskModel

        root_task = TaskModel(**root_task_data)
        session.add(root_task)

        # 更新准备任务的 parent_task_id
        prep_task.parent_task_id = root_task_id
        prep_task.task_metadata = {
            **(prep_task.task_metadata or {}),
            "is_preparation_task": True,
            "long_term_task_id": root_task_id,
        }

        await session.commit()

        logger.info(
            f"[Watchdog] 根任务已创建 | "
            f"root_task_id={root_task_id} | "
            f"prep_task_id={prep_task.id}"
        )

    async def _check_project(
        self,
        session: AsyncSession,
        root_task: Task,
    ) -> dict[str, Any]:
        """
        检查单个长期任务

        Args:
            session: 数据库会话
            root_task: 根任务对象

        Returns:
            检查结果
        """
        project_id = root_task.id

        # 获取当前待执行的子任务
        current_task_result = await self._get_current_task(session, project_id)

        if not current_task_result:
            # 没有待执行任务，检查是否所有任务都已完成
            return await self._check_project_completion(session, root_task)

        current_task = current_task_result
        task_status = current_task.status
        task_id = current_task.id

        # 检查任务状态
        if task_status == "completed":
            # 检查是否是准备任务
            task_metadata = current_task.task_metadata or {}
            is_preparation_task = task_metadata.get("is_preparation_task", False)

            if is_preparation_task:
                # 准备任务完成，更新根任务的评估指标
                logger.info(
                    f"项目 {project_id} 的准备任务 {task_id} 已完成，更新根任务评估指标"
                )
                await self._update_root_task_metrics(session, root_task, current_task)

            # 当前任务已完成，触发下一个任务
            logger.info(
                f"项目 {project_id} 的当前任务 {task_id} 已完成，触发下一个任务"
            )
            return await self.trigger_component.trigger_next_task(project_id)

        elif task_status == "failed":
            # 任务失败，检查是否需要重试（支持差异化异常处理）
            failure_reason = self._determine_failure_reason(current_task)
            progress_info = self._extract_progress_info(current_task)
            return await self.failure_component.handle_failed_task(
                session, root_task, current_task, failure_reason, progress_info
            )

        elif task_status == "blocked":
            # 任务阻塞，需要用户介入
            logger.warning(f"项目 {project_id} 的任务 {task_id} 已阻塞，停止自动执行")
            await self.project_controller.pause_project(project_id, "任务阻塞")
            return {
                "project_id": project_id,
                "task_id": task_id,
                "status": "blocked",
                "action": "paused",
            }

        elif task_status == "pending":
            # 任务待执行，自动启动任务
            return await self._handle_pending_task(project_id, task_id, current_task)

        elif task_status == ExecutionStatus.RUNNING.value:
            # 任务执行中，检查超时和卡住
            await self.timeout_component.check_task_health(
                session, root_task, current_task
            )
            return {
                "project_id": project_id,
                "task_id": task_id,
                "status": task_status,
                "action": "monitored",
            }

        else:
            logger.warning(f"未知任务状态: {task_status}")
            return {
                "project_id": project_id,
                "task_id": task_id,
                "status": task_status,
                "action": "unknown",
            }

    async def _handle_pending_task(
        self,
        project_id: str,
        task_id: str,
        current_task: Task,
    ) -> dict[str, Any]:
        """
        处理待执行任务

        Args:
            project_id: 项目 ID
            task_id: 任务 ID
            current_task: 当前任务对象

        Returns:
            处理结果
        """
        logger.info(
            f"项目 {project_id} 的任务 {task_id} 状态为 pending，自动启动任务"
        )

        # 调用任务管理器回调启动任务
        if self.task_manager_callback:
            try:
                result = await self.task_manager_callback(
                    task_id=task_id,
                    project_id=project_id,
                    task=current_task,
                )

                # 检查回调返回结果
                if isinstance(result, dict):
                    status = result.get("status", "unknown")
                    if status == "success":
                        logger.info(f"已触发任务 {task_id} 的启动回调")
                        # 创建计时器
                        await self._create_task_timer(task_id, project_id)
                        return {
                            "project_id": project_id,
                            "task_id": task_id,
                            "status": "pending",
                            "action": "started",
                        }
                    elif status == "deferred":
                        # 服务未就绪，任务已加入待处理队列
                        logger.info(
                            f"任务 {task_id} 已加入待处理队列，等待执行服务就绪"
                        )
                        return {
                            "project_id": project_id,
                            "task_id": task_id,
                            "status": "pending",
                            "action": "deferred",
                            "message": result.get("message", "任务已加入待处理队列"),
                        }
                    elif status == "failed_but_queued":
                        # 启动失败但已加入队列
                        logger.warning(
                            f"任务 {task_id} 启动失败但已加入待处理队列: {result.get('error')}"
                        )
                        return {
                            "project_id": project_id,
                            "task_id": task_id,
                            "status": "pending",
                            "action": "failed_but_queued",
                            "error": result.get("error"),
                        }
                    else:
                        logger.warning(f"任务 {task_id} 回调返回未知状态: {status}")
                        return {
                            "project_id": project_id,
                            "task_id": task_id,
                            "status": "pending",
                            "action": "unknown_status",
                            "callback_result": result,
                        }
                else:
                    # 回调没有返回结果，假设成功
                    logger.info(f"已触发任务 {task_id} 的启动回调")
                    # 创建计时器
                    await self._create_task_timer(task_id, project_id)
                    return {
                        "project_id": project_id,
                        "task_id": task_id,
                        "status": "pending",
                        "action": "started",
                    }

            except Exception as e:
                logger.error(f"启动任务回调失败: {e}")
                return {
                    "project_id": project_id,
                    "task_id": task_id,
                    "status": "pending",
                    "action": "start_failed",
                    "error": str(e),
                }
        else:
            logger.warning("任务管理器回调未注册，无法自动启动任务")
            return {
                "project_id": project_id,
                "task_id": task_id,
                "status": "pending",
                "action": "no_callback",
            }

    async def _create_task_timer(
        self,
        task_id: str,
        root_task_id: str | None = None,
    ) -> None:
        """
        为任务创建计时器

        Args:
            task_id: 任务 ID
            root_task_id: 根任务 ID
        """
        try:
            await self._timer_manager.create_timer(
                task_id=task_id,
                timeout=float(self._timer_manager.task_max_duration),
                callback=self._timer_timeout_callback,
                root_task_id=root_task_id,
            )
            logger.info(
                f"已为任务 {task_id} 创建计时器，"
                f"超时时间: {self._timer_manager.task_max_duration}s"
            )
        except ValueError as e:
            # 计时器已存在，重置
            logger.warning(f"计时器已存在，重置: {e}")
            await self._timer_manager.reset_timer(task_id)

    async def _get_current_task(
        self,
        session: AsyncSession,
        project_id: str,
    ) -> Task | None:
        """
        获取项目当前待执行的子任务

        Args:
            session: 数据库会话
            project_id: 项目 ID（根任务 ID）

        Returns:
            任务对象，如果没有则返回 None
        """
        # 查询项目中状态不是 completed 的最早任务
        query = (
            select(Task)
            .where(
                and_(
                    Task.parent_task_id == project_id,
                    Task.status.in_([
                        ExecutionStatus.PENDING.value,
                        ExecutionStatus.RUNNING.value,
                        ExecutionStatus.FAILED.value,
                        ExecutionStatus.BLOCKED.value,
                    ]),
                )
            )
            .order_by(Task.created_at)
            .limit(1)
        )

        result = await session.execute(query)
        return result.scalar_one_or_none()

    async def _check_project_completion(
        self,
        session: AsyncSession,
        root_task: Task,
    ) -> dict[str, Any]:
        """
        检查项目是否完成

        Args:
            session: 数据库会话
            root_task: 根任务对象

        Returns:
            检查结果
        """
        project_id = root_task.id

        # 查询所有子任务
        query = select(Task).where(Task.parent_task_id == project_id)
        result = await session.execute(query)
        subtasks = result.scalars().all()

        # 检查是否所有子任务都已完成
        all_completed = all(t.status == "completed" for t in subtasks)

        if all_completed and len(subtasks) > 0:
            # 所有任务都已完成，标记项目为完成
            await self._mark_project_completed(session, project_id)
            return {
                "project_id": project_id,
                "action": "project_completed",
            }
        else:
            # 还有未完成的任务或没有子任务
            return {
                "project_id": project_id,
                "action": "waiting_tasks",
            }

    async def _mark_project_completed(
        self,
        session: AsyncSession,
        project_id: str,
    ) -> None:
        """
        标记项目为准备完成

        核心原则：执行器不处理状态转换
        - 只更新 task_metadata，不设置任务状态
        - 任务状态变更由 task_evaluate 工具触发
        - 所有子任务完成后，根任务需要通过 task_evaluate 工具完成

        Args:
            session: 数据库会话
            project_id: 项目 ID
        """
        # 更新根任务的 task_metadata
        result = await session.execute(select(Task).where(Task.id == project_id))
        root_task = result.scalar_one_or_none()

        if root_task:
            task_metadata = root_task.task_metadata or {}
            task_metadata["auto_execute"] = False
            task_metadata["all_subtasks_completed"] = True
            task_metadata["subtasks_completed_at"] = datetime.now().isoformat()

            await session.execute(
                update(Task)
                .where(Task.id == project_id)
                .values(
                    task_metadata=task_metadata,
                    updated_at=datetime.now(),
                )
            )
            await session.commit()
            logger.info(
                f"项目 {project_id} 所有子任务已完成，等待 task_evaluate 工具完成根任务"
            )

    async def _update_root_task_metrics(
        self,
        session: AsyncSession,
        root_task: Task,
        preparation_task: Task,
    ) -> None:
        """
        从准备任务的输出中提取评估指标，更新根任务

        Args:
            session: 数据库会话
            root_task: 根任务对象
            preparation_task: 准备任务对象
        """
        try:
            # 从准备任务的输出中提取评估指标
            # 准备任务的输出应该包含 root_task_evaluation_metrics 字段
            task_metadata = preparation_task.task_metadata or {}

            # 尝试从多个可能的位置获取评估指标
            evaluation_metrics = None

            # 1. 从 task_metadata 中获取
            if "root_task_evaluation_metrics" in task_metadata:
                evaluation_metrics = task_metadata["root_task_evaluation_metrics"]
                logger.info(
                    f"从准备任务 metadata 中提取到评估指标: {evaluation_metrics}"
                )

            # 2. 从准备任务的输出中获取（如果有 output 字段）
            if not evaluation_metrics and hasattr(preparation_task, "output"):
                output = preparation_task.output
                if (
                    isinstance(output, dict)
                    and "root_task_evaluation_metrics" in output
                ):
                    evaluation_metrics = output["root_task_evaluation_metrics"]
                    logger.info(
                        f"从准备任务 output 中提取到评估指标: {evaluation_metrics}"
                    )

            # 3. 从子任务中聚合评估指标
            if not evaluation_metrics:
                # 查询所有子任务（排除准备任务）
                query = select(Task).where(
                    and_(
                        Task.parent_task_id == root_task.id,
                        Task.id != preparation_task.id,
                    )
                )
                result = await session.execute(query)
                subtasks = result.scalars().all()

                # 聚合所有子任务的评估指标
                all_metrics = set()
                for subtask in subtasks:
                    if subtask.evaluation_metric_ids:
                        all_metrics.update(subtask.evaluation_metric_ids)

                if all_metrics:
                    evaluation_metrics = list(all_metrics)
                    logger.info(
                        f"从 {len(subtasks)} 个子任务中聚合评估指标: {evaluation_metrics}"
                    )

            # 如果找到了评估指标，更新根任务
            if evaluation_metrics:
                # 更新根任务的 evaluation_metric_ids
                await session.execute(
                    update(Task)
                    .where(Task.id == root_task.id)
                    .values(
                        evaluation_metric_ids=evaluation_metrics,
                        total_criteria=len(evaluation_metrics),
                        updated_at=datetime.now(),
                    )
                )
                await session.commit()

                logger.info(
                    f"根任务 {root_task.id} 的评估指标已更新: {evaluation_metrics}"
                )
            else:
                logger.warning(f"未能从准备任务 {preparation_task.id} 中提取评估指标")

        except Exception as e:
            logger.error(f"更新根任务评估指标失败: {e}", exc_info=True)

    def _determine_failure_reason(self, task: Task) -> "FailureReason":
        """
        根据任务元数据判断失败原因

        FEATURE-EXCEPTION-HANDLING: 失败原因判断
        判断逻辑:
          - 检查 task_metadata 中的 failure_reason 字段
          - 检查依赖任务状态
          - 检查资源配置
          - 检查进度信息

        Args:
            task: 任务对象

        Returns:
            失败原因枚举值
        """
        from src.tasks.watchdog.components.failure_handler import FailureReason

        task_metadata = task.task_metadata or {}

        explicit_reason = task_metadata.get("failure_reason")
        if explicit_reason:
            try:
                return FailureReason(explicit_reason)
            except ValueError:
                pass

        if task_metadata.get("dependency_failed"):
            return FailureReason.DEPENDENCY_FAILED

        if task_metadata.get("config_error"):
            return FailureReason.CONFIG_ERROR

        if task_metadata.get("resource_insufficient"):
            return FailureReason.RESOURCE_INSUFFICIENT

        if task_metadata.get("timeout_occurred"):
            if task_metadata.get("has_progress"):
                return FailureReason.TIMEOUT_WITH_PROGRESS
            return FailureReason.TIMEOUT_NO_PROGRESS

        if task_metadata.get("partial_success"):
            return FailureReason.PARTIAL_SUCCESS

        return FailureReason.UNKNOWN

    def _extract_progress_info(self, task: Task) -> dict[str, Any] | None:
        """
        从任务元数据中提取进度信息

        FEATURE-EXCEPTION-HANDLING: 进度信息提取
        用于超时有进展和部分成功场景

        Args:
            task: 任务对象

        Returns:
            进度信息字典，如果没有则返回 None
        """
        task_metadata = task.task_metadata or {}
        return task_metadata.get("progress_info") or task_metadata.get("saved_progress")

    def _update_heartbeat(self, project_id: str) -> None:
        """
        更新项目心跳

        Args:
            project_id: 项目 ID
        """
        import time

        self._heartbeats[project_id] = time.time()

    def get_heartbeat_age(self, project_id: str) -> float | None:
        """
        获取项目心跳年龄

        Args:
            project_id: 项目 ID

        Returns:
            心跳年龄（秒），如果不存在返回 None
        """
        import time

        heartbeat = self._heartbeats.get(project_id)
        if heartbeat is None:
            return None
        return time.time() - heartbeat

    def subscribe_events(self) -> None:
        """
        订阅事件（事件驱动改造）

        只订阅 task.completed 和 task.cancelled 事件。
        task.submitted 事件由 TaskOrchestrator 处理。
        长期任务准备任务的检测通过定期扫描实现。
        """
        # 不再订阅 task.submitted，由 TaskOrchestrator 处理
        self._event_bus.subscribe_simple("task.completed", self._on_task_completed)
        self._event_bus.subscribe_simple("task.cancelled", self._on_task_cancelled)

    async def _on_task_completed(self, event: ExecutionEvent) -> None:
        """
        处理任务完成事件

        Args:
            event: 任务完成事件
        """
        data = event.data
        task_id = data.get("task_id")

        async with managed_session() as session:
            task = await session.get(Task, task_id)
            if not task:
                return

            task_metadata = task.task_metadata or {}

            # 如果是准备任务，更新根任务评估指标
            if task_metadata.get("is_preparation_task"):
                root_task_id = task.parent_task_id
                if root_task_id:
                    root_task = await session.get(Task, root_task_id)
                    if root_task:
                        logger.info(
                            f"准备任务 {task_id} 完成，更新根任务 {root_task_id} 评估指标"
                        )
                        await self._update_root_task_metrics(session, root_task, task)

            # 如果有父任务，触发下一个任务
            if task.parent_task_id:
                next_task = await self._get_current_task(session, task.parent_task_id)
                if next_task and next_task.status == "pending":
                    logger.info(f"任务 {task_id} 完成，触发下一个任务 {next_task.id}")
                    # 发布任务提交事件（触发执行）
                    await self._event_bus.publish(
                        ExecutionEvent(
                            event_type=EventType.TASK_SUBMITTED,
                            session_id=next_task.session_id,
                            data={
                                "task_id": next_task.id,
                                "target_type": next_task.target_type,
                                "target_id": next_task.target_id,
                                "priority": next_task.priority,
                                "agent_level": 3,  # 默认 L3
                                "parent_task_id": next_task.parent_task_id,
                                "session_id": next_task.session_id,
                                "metadata": next_task.task_metadata or {},
                            },
                        )
                    )

    async def _on_task_cancelled(self, event: ExecutionEvent) -> None:
        """
        处理任务取消事件

        Args:
            event: 任务取消事件
        """
        data = event.data
        task_id = data.get("task_id")

        async with managed_session() as session:
            task = await session.get(Task, task_id)
            if not task:
                return

            # 如果是根任务，停止自动执行
            if task.parent_task_id is None:
                task_metadata = task.task_metadata or {}
                if task_metadata.get("auto_execute"):
                    task_metadata["auto_execute"] = False
                    task.task_metadata = task_metadata
                    await session.commit()
                    logger.info(f"根任务 {task_id} 已停止自动执行")
