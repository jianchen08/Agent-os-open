"""
任务执行回调服务

提供显式依赖注入的任务执行回调，消除闭包隐式依赖问题。

职责：
- 接收任务提交后的执行请求
- 启动后台任务执行
- 管理 pending tasks 防止内存泄漏
- 支持 agent 和 workflow 两种目标类型
"""

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.task_runner import TaskRunner
from src.core.event_bus import EventBusBase, get_event_bus

if TYPE_CHECKING:
    from src.agents.task_runner import TaskRunner
    from src.tasks.services.state_service import TaskStateService

logger = logging.getLogger(__name__)


class TaskExecutionCallbackService:
    """
    任务执行回调服务 - 显式依赖注入

    替代原有的闭包实现，提供：
    1. 显式依赖注入（session, task_executor, event_bus）
    2. 可测试性（依赖可 mock）
    3. 统一的回调接口

    使用方式：
        from src.tasks.services.state_service import TaskStateService
        state_service = TaskStateService(session)
        callback_service = TaskExecutionCallbackService(
            session=db_session,
            state_service=state_service,
            event_bus=event_bus,
        )

        # 作为回调使用
        result = await callback_service.execute(task_id, target_type, target_id, goal)

        # 或作为 Callable 使用
        result = await callback_service(task_id, target_type, target_id, goal)
    """

    def __init__(
        self,
        session: AsyncSession,
        state_service: "TaskStateService | None" = None,
        task_executor: "TaskRunner | None" = None,
        event_bus: EventBusBase | None = None,
        user_id: str | None = None,
        thread_id: str | None = None,
    ):
        """
        初始化任务执行回调服务

        Args:
            session: 数据库会话（显式注入）
            state_service: 任务状态服务（必需，用于 TaskExecutor）
            task_executor: 任务执行器（可选，默认创建新实例）
            event_bus: 事件总线（可选，默认使用全局实例）
            user_id: 用户 ID（可选，用于 workflow 执行）
            thread_id: 线程 ID（可选，用于 workflow 执行）
        """
        self.session = session

        if task_executor:
            self._task_executor = task_executor
        else:
            if state_service is None:
                from src.tasks.services.state_service import TaskStateService
                state_service = TaskStateService(session)
            self._task_executor = TaskRunner(state_service=state_service)

        self._event_bus = event_bus
        self.user_id = user_id
        self.thread_id = thread_id

        self._pending_tasks: set[asyncio.Task] = set()

    @property
    def event_bus(self) -> EventBusBase:
        """获取事件总线实例（延迟初始化）"""
        if self._event_bus is None:
            self._event_bus = get_event_bus()
        return self._event_bus

    async def execute(
        self,
        task_id: str,
        target_type: str,
        target_id: str,
        goal: str | dict | None = None,
    ) -> dict[str, Any]:
        """
        执行任务回调

        根据目标类型执行不同的处理逻辑：
        - agent: 创建后台异步任务执行
        - workflow: 同步执行工作流

        Args:
            task_id: 任务 ID
            target_type: 目标类型（agent/workflow）
            target_id: 目标 ID
            goal: 任务目标（可选）

        Returns:
            执行结果字典，包含：
            - success: bool - 是否成功
            - task_submitted: bool - 任务是否已提交
            - message: str - 执行消息
            - error: str - 错误信息（如果失败）
        """
        logger.info(
            f"[TaskExecutionCallbackService] 任务执行回调被触发 | "
            f"task_id={task_id} | target_type={target_type} | target_id={target_id}"
        )

        try:
            if target_type == "workflow":
                return await self._execute_workflow(task_id, target_id, goal)
            else:
                return await self._execute_agent(task_id)

        except Exception as e:
            logger.exception(
                f"[TaskExecutionCallbackService] 任务执行回调失败 | "
                f"task_id={task_id} | error={str(e)}"
            )
            return {
                "success": False,
                "error": str(e),
                "task_submitted": False,
                "task_id": task_id,
                "message": f"任务提交失败: {str(e)}",
            }

    async def _execute_agent(self, task_id: str) -> dict[str, Any]:
        """
        执行 Agent 任务（后台异步）

        Args:
            task_id: 任务 ID

        Returns:
            执行结果字典
        """
        # 创建后台异步任务
        task = asyncio.create_task(
            self._task_executor.execute_task(task_id, self.session),
            name=f"task_executor_{task_id[:8]}",
        )

        # 添加到追踪集合
        self._pending_tasks.add(task)

        # 添加完成回调
        task.add_done_callback(self._create_task_done_callback(task_id))

        logger.info(
            f"[TaskExecutionCallbackService] 任务已提交后台执行 | "
            f"task_id={task_id} | pending_count={len(self._pending_tasks)}"
        )

        return {
            "success": True,
            "task_submitted": True,
            "task_id": task_id,
            "message": "任务已成功提交并开始后台执行",
        }

    async def _execute_workflow(
        self,
        task_id: str,
        workflow_id: str,
        goal: dict | None,
    ) -> dict[str, Any]:
        """
        执行工作流（同步执行）

        Args:
            task_id: 任务 ID
            workflow_id: 工作流 ID
            goal: 任务目标

        Returns:
            执行结果字典
        """
        from src.services.workflow_service import WorkflowService

        logger.info(
            f"[TaskExecutionCallbackService] 开始执行工作流 | "
            f"task_id={task_id} | workflow_id={workflow_id}"
        )

        try:
            workflow_service = WorkflowService(db=self.session)

            # 准备工作流输入
            workflow_inputs = {
                "task_id": task_id,
                "goal": goal,
                "user_id": self.user_id,
                "session_id": self.thread_id,
            }

            # 为 resource_generation 工作流准备 resource_requirement 输入
            if workflow_id == "resource_generation" and goal:
                workflow_inputs["resource_requirement"] = {
                    "name": goal.get("title", "未知资源"),
                    "description": goal.get("description", ""),
                    "capabilities": [],
                    "context": {
                        "task_id": task_id,
                        "session_id": self.thread_id,
                    },
                }

            # 执行工作流
            result = await workflow_service.execute_workflow(
                workflow_id=workflow_id,
                inputs=workflow_inputs,
                timeout=300,  # 5分钟超时
            )

            # 记录执行结果
            if result.get("success") is True:
                logger.info(
                    f"[TaskExecutionCallbackService] 工作流完成 | "
                    f"task_id={task_id} | workflow_id={workflow_id}"
                )
            elif result.get("success") is False:
                error_msg = result.get("error", "未知错误")
                logger.error(
                    f"[TaskExecutionCallbackService] 工作流失败 | "
                    f"task_id={task_id} | workflow_id={workflow_id} | error={error_msg}"
                )
            else:
                logger.warning(
                    f"[TaskExecutionCallbackService] 工作流未完成 | "
                    f"task_id={task_id} | workflow_id={workflow_id}"
                )

            return {
                "success": result.get("success", False),
                "task_submitted": True,
                "task_id": task_id,
                "message": result.get("message", "工作流执行完成"),
                "output": result.get("output"),
                "error": result.get("error"),
            }

        except Exception as e:
            logger.exception(
                f"[TaskExecutionCallbackService] 工作流执行异常 | "
                f"task_id={task_id} | workflow_id={workflow_id} | error={str(e)}"
            )
            return {
                "success": False,
                "task_submitted": False,
                "task_id": task_id,
                "error": str(e),
                "message": f"工作流执行异常: {str(e)}",
            }

    def _create_task_done_callback(self, task_id: str) -> Callable:
        """
        创建任务完成回调

        Args:
            task_id: 任务 ID

        Returns:
            回调函数
        """

        def task_done_callback(task: asyncio.Task) -> None:
            """任务完成时的回调"""
            # 从追踪集合中移除
            self._pending_tasks.discard(task)

            # 处理异常
            try:
                exc = task.exception()
                if exc:
                    logger.error(
                        f"[TaskExecutionCallbackService] 任务执行异常 | "
                        f"task_id={task_id} | error={exc}"
                    )
                else:
                    logger.info(
                        f"[TaskExecutionCallbackService] 任务执行完成 | task_id={task_id}"
                    )
            except asyncio.CancelledError:
                logger.warning(
                    f"[TaskExecutionCallbackService] 任务被取消 | task_id={task_id}"
                )

        return task_done_callback

    async def cleanup_tasks(self) -> None:
        """
        清理已完成的任务

        移除已完成的任务，防止内存泄漏
        """
        if not self._pending_tasks:
            return

        # 收集已完成的任务
        done = {task for task in self._pending_tasks if task.done()}
        # 从集合中移除
        self._pending_tasks.difference_update(done)

        if done:
            logger.debug(
                f"[TaskExecutionCallbackService] 清理已完成任务 | count={len(done)}"
            )

    async def cleanup(self) -> None:
        """
        清理所有后台任务

        取消所有待处理的后台任务，释放资源
        """
        # 取消所有待处理任务
        if self._pending_tasks:
            for task in list(self._pending_tasks):
                if not task.done():
                    task.cancel()
            # 等待所有任务完成或被取消
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            self._pending_tasks.clear()

        logger.debug(
            "[TaskExecutionCallbackService] 任务执行回调服务资源已清理"
        )

    async def __call__(
        self,
        task_id: str,
        target_type: str,
        target_id: str,
        goal: str | dict | None = None,
    ) -> dict[str, Any]:
        """
        支持 Callable 接口

        允许服务实例作为回调函数使用

        Args:
            task_id: 任务 ID
            target_type: 目标类型
            target_id: 目标 ID
            goal: 任务目标

        Returns:
            执行结果字典
        """
        return await self.execute(task_id, target_type, target_id, goal)
