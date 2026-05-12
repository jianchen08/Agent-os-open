"""
任务执行服务

提供统一的任务执行入口，避免重复执行问题。
这是系统中唯一的任务执行服务，所有任务执行请求都应通过此服务。
"""

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from src.core.constants import TaskPriority
from src.db.connection import get_async_session

logger = logging.getLogger(__name__)


class ExecutionService:
    """
    任务执行服务

    职责：
    1. 提供统一的任务执行入口
    2. 防止任务重复执行（通过 TaskService 中的 _executing_tasks 集合）
    3. 处理看门狗自动触发的任务执行
    4. 处理事件总线触发的任务执行

    设计原则：
    - 所有任务执行都必须通过此服务
    - 不直接执行，而是委托给 TaskService
    - 保持与 TaskService 的防重复执行机制一致
    """

    def __init__(self, session_factory: async_sessionmaker = None):
        """
        初始化执行服务

        Args:
            session_factory: 数据库会话工厂，默认为 get_async_session
        """
        self.session_factory = session_factory or get_async_session

    async def start_task(
        self,
        task_id: str,
        project_id: str | None = None,
        auto_triggered: bool = False,
    ) -> dict[str, Any]:
        """
        启动任务执行

        这是看门狗和其他自动执行机制调用的入口。
        实际执行委托给 TaskService，利用其防重复执行机制。

        Args:
            task_id: 任务 ID
            project_id: 项目 ID（可选）
            auto_triggered: 是否自动触发（来自看门狗）

        Returns:
            执行结果字典
        """
        logger.info(
            f"[ExecutionService] 启动任务 | task_id={task_id} | "
            f"auto_triggered={auto_triggered}"
        )

        try:
            # 获取任务信息
            async with self.session_factory() as session:
                from src.db.repositories.task_repo import TaskRepository

                task_repo = TaskRepository(session)
                task = await task_repo.get(task_id)

                if not task:
                    logger.error(f"[ExecutionService] 任务不存在 | task_id={task_id}")
                    return {
                        "success": False,
                        "task_id": task_id,
                        "error": "任务不存在",
                    }

                # 检查任务状态
                if task.status not in ["pending", "failed"]:
                    logger.warning(
                        f"[ExecutionService] 任务状态不允许执行 | "
                        f"task_id={task_id} | status={task.status}"
                    )
                    return {
                        "success": False,
                        "task_id": task_id,
                        "error": f"任务状态为 {task.status}，不允许执行",
                        "status": task.status,
                    }

                # 更新任务状态为执行中
                task.status = "running"
                task.updated_at = datetime.now(UTC)
                await session.commit()

                logger.info(
                    f"[ExecutionService] 任务状态已更新为 running | "
                    f"task_id={task_id}"
                )

            # 使用 TaskService 的线程池执行机制
            # 创建任务数据（模拟 API 创建任务的格式）
            {
                "title": task.title,
                "description": task.description,
                "goal": task.goal,
                "evaluation_metric_ids": task.evaluation_metric_ids or [],
                "agent_id": task.target_id,
                "priority": TaskPriority.to_str(task.priority),
                "parent_task_id": task.parent_task_id,
            }

            # 使用 TaskService 执行
            # 注意：这里我们不调用 create_task，而是直接触发执行
            # 因为任务已经存在，只需要执行它
            await self._trigger_task_execution(task_id)

            return {
                "success": True,
                "task_id": task_id,
                "status": "running",
                "message": "任务已启动执行",
            }

        except Exception as e:
            logger.error(
                f"[ExecutionService] 启动任务失败 | task_id={task_id} | error={str(e)}",
                exc_info=True,
            )
            return {
                "success": False,
                "task_id": task_id,
                "error": str(e),
            }

    async def _trigger_task_execution(self, task_id: str):
        """
        触发任务执行

        使用 TaskService 的后台执行机制。

        Args:
            task_id: 任务 ID
        """
        from src.services.task_service import _background_executor, _run_task_in_thread

        # 提交到线程池执行（与 TaskService.create_task 使用相同的机制）
        future = _background_executor.submit(_run_task_in_thread, task_id)

        logger.info(
            f"[ExecutionService] 已提交任务到线程池 | task_id={task_id} | "
            f"future={future}"
        )


# 全局执行服务实例
_execution_service: ExecutionService | None = None


def get_execution_service() -> ExecutionService:
    """
    获取全局执行服务实例

    Returns:
        执行服务实例
    """
    global _execution_service

    if _execution_service is None:
        _execution_service = ExecutionService()

    return _execution_service


def set_execution_service(service: ExecutionService):
    """
    设置全局执行服务实例

    Args:
        service: 执行服务实例
    """
    global _execution_service
    _execution_service = service
