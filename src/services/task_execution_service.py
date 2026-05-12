"""
任务执行服务

提供任务执行相关的操作，包括：
- 后台任务提交
- 任务执行状态管理
- 执行中的任务跟踪

核心原则：
- 状态转换通过 TaskStateService 进行
- 不直接设置任务状态
"""

import asyncio
import concurrent.futures
import logging
import threading
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Task
from src.db.repositories.task_repo import TaskRepository
from src.db.session_manager import independent_transaction

logger = logging.getLogger(__name__)

_background_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="task_bg_"
)

_executing_tasks: set[str] = set()
_executing_lock = threading.Lock()


def _run_task_in_thread(task_id: str):
    """
    在独立线程中运行任务，创建新的事件循环

    这个函数在独立线程中执行，确保：
    1. 拥有独立的事件循环
    2. 不受 API 请求生命周期影响
    3. 可以可靠地执行长时间运行的任务
    4. 使用全局集合防止重复执行

    注意：TaskExecutor 内部使用 SessionManager 管理会话，
    因此这里不需要传递会话参数。

    Args:
        task_id: 任务 ID
    """
    thread_id = threading.current_thread().ident
    thread_name = threading.current_thread().name

    # 检查任务是否已在执行中
    with _executing_lock:
        if task_id in _executing_tasks:
            logger.warning(
                "[后台任务] 任务已在执行中，跳过重复执行 | task_id=%s | thread=%s",
                task_id,
                thread_name,
            )
            return
        _executing_tasks.add(task_id)

    logger.info(
        "[后台任务] 线程启动 | task_id=%s | thread_id=%s | thread_name=%s",
        task_id,
        thread_id,
        thread_name,
    )

    # 在新线程中创建新的事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # 在新的事件循环中运行异步任务
        loop.run_until_complete(_execute_task_async(task_id))
    except Exception as e:
        logger.error(
            "[后台任务] 线程执行异常 | task_id=%s | error=%s",
            task_id,
            str(e),
            exc_info=True,
        )
    finally:
        # 清理执行状态
        with _executing_lock:
            _executing_tasks.discard(task_id)

        # 清理事件循环
        try:
            loop.close()
        except Exception as e:
            logger.warning(
                "[后台任务] 关闭事件循环失败 | task_id=%s | error=%s",
                task_id,
                str(e),
            )
        logger.info(
            "[后台任务] 线程结束 | task_id=%s | thread_id=%s",
            task_id,
            thread_id,
        )


async def _execute_task_async(task_id: str):
    """
    实际的异步执行函数

    使用 TaskExecutor 执行任务，从调用链传入会话。

    Args:
        task_id: 任务 ID
    """
    from src.agents.task_runner import TaskRunner
    from src.db.session_manager import managed_session
    from src.tasks.services.state_service import TaskStateService

    try:
        logger.info("[后台任务] 准备执行任务 | task_id=%s", task_id)

        async with managed_session() as session:
            state_service = TaskStateService(session)
            executor = TaskRunner(state_service=state_service)
            result = await executor.execute_task_with_result(task_id, session)

        if result.get("success"):
            logger.info("[后台任务] 执行成功 | task_id=%s", task_id)
        else:
            logger.error(
                "[后台任务] 执行失败 | task_id=%s | error=%s",
                task_id,
                result.get("error"),
            )
    except Exception as e:
        logger.error(
            "[后台任务] 执行异常 | task_id=%s | error=%s",
            task_id,
            str(e),
            exc_info=True,
        )


class TaskExecutionService:
    """
    任务执行服务（单例模式）

    负责所有任务执行相关的操作，包括：
    - 后台任务提交和管理
    - 执行状态跟踪
    - 超时任务检查
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """
        单例模式实现

        Returns:
            TaskExecutionService 单例实例
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def get_instance(cls) -> "TaskExecutionService":
        """
        获取单例实例

        Returns:
            TaskExecutionService 单例实例
        """
        return cls()

    async def execute_task(
        self,
        task_id: str,
        session: AsyncSession,
        project_id: str | None = None,
        auto_triggered: bool = False,
    ) -> dict[str, Any]:
        """
        执行任务

        这是统一的任务执行入口，用于看门狗和其他自动执行机制调用。

        核心原则：
        - 状态转换通过 TaskStateService 进行
        - 不直接设置任务状态

        Args:
            task_id: 任务 ID
            session: 数据库会话
            project_id: 项目 ID（可选）
            auto_triggered: 是否自动触发（来自看门狗）

        Returns:
            执行结果字典
        """
        logger.info(
            f"[TaskExecutionService] 执行任务 | task_id={task_id} | "
            f"auto_triggered={auto_triggered}"
        )

        try:
            task_repo = TaskRepository(session)
            task = await task_repo.get(task_id)

            if not task:
                logger.error(f"[TaskExecutionService] 任务不存在 | task_id={task_id}")
                return {
                    "success": False,
                    "task_id": task_id,
                    "error": "任务不存在",
                }

            if task.status not in ["pending", "failed"]:
                logger.warning(
                    f"[TaskExecutionService] 任务状态不允许执行 | "
                    f"task_id={task_id} | status={task.status}"
                )
                return {
                    "success": False,
                    "task_id": task_id,
                    "error": f"任务状态为 {task.status}，不允许执行",
                    "status": task.status,
                }

            from src.tasks.services.state_service import TaskStateService

            state_service = TaskStateService(session)
            await state_service.transition(
                task_id=task_id,
                to_status="running",
                reason="任务开始执行",
                source="task_execution_service",
            )

            logger.info(
                f"[TaskExecutionService] 任务状态已通过状态服务更新为 running | "
                f"task_id={task_id}"
            )

            await self._submit_background_task(task_id)

            return {
                "success": True,
                "task_id": task_id,
                "status": "running",
                "message": "任务已启动执行",
            }

        except Exception as e:
            logger.error(
                f"[TaskExecutionService] 执行任务失败 | task_id={task_id} | error={str(e)}",
                exc_info=True,
            )
            return {
                "success": False,
                "task_id": task_id,
                "error": str(e),
            }

    async def _submit_background_task(self, task_id: str) -> None:
        """
        提交后台任务

        检查任务是否已在执行中，如果没有则提交到线程池。

        Args:
            task_id: 任务 ID
        """
        with _executing_lock:
            is_executing = task_id in _executing_tasks

        if is_executing:
            logger.warning(
                f"[TaskExecutionService] 任务已在执行中，跳过重复提交 | task_id={task_id}"
            )
            return

        logger.info(
            f"[TaskExecutionService] 检测到根任务，准备后台执行 | task_id={task_id}"
        )

        try:
            # 提交到线程池执行（非阻塞）
            future = _background_executor.submit(_run_task_in_thread, task_id)
            logger.info(
                f"[TaskExecutionService] 已提交后台任务到线程池 | task_id={task_id} | "
                f"future={future}"
            )
        except Exception as e:
            logger.error(
                f"[TaskExecutionService] 创建后台任务失败 | task_id={task_id} | error={str(e)}",
                exc_info=True,
            )

    def get_executing_tasks(self) -> set[str]:
        """
        获取正在执行的任务 ID 集合

        Returns:
            正在执行的任务 ID 集合
        """
        with _executing_lock:
            return _executing_tasks.copy()

    async def check_timeout_tasks(self) -> dict[str, Any]:
        """
        检查超时任务

        使用独立事务执行，不影响调用者的事务。

        核心原则：
        - 状态转换通过 TaskStateService 进行
        - 不直接设置任务状态

        Returns:
            处理结果字典
        """
        from datetime import timedelta

        from src.tasks.services.state_service import TaskStateService

        try:
            async with independent_transaction() as session:
                timeout_threshold = datetime.now(UTC) - timedelta(hours=24)

                stmt = select(Task).where(
                    Task.status.in_(["pending", "running"]),
                    Task.created_at < timeout_threshold,
                )
                result = await session.execute(stmt)
                timeout_tasks = result.scalars().all()

                state_service = TaskStateService(session)
                updated_count = 0
                for task in timeout_tasks:
                    await state_service.transition(
                        task_id=task.id,
                        to_status="failed",
                        reason="任务执行超时",
                        source="task_execution_service.timeout_check",
                    )
                    updated_count += 1

                return {
                    "status": "success",
                    "message": f"检查完成，处理了 {updated_count} 个超时任务",
                    "timeout_tasks_count": updated_count,
                    "checked_at": datetime.now(UTC).isoformat(),
                }

        except Exception as e:
            logger.error(f"检查超时任务失败: {str(e)}", exc_info=True)
            return {
                "status": "error",
                "message": f"检查超时任务失败: {str(e)}",
                "checked_at": datetime.now(UTC).isoformat(),
            }
