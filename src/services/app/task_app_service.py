"""
任务应用服务

封装任务相关的业务逻辑，协调任务服务和看门狗服务。

核心职责：
1. 启动任务执行
2. 处理任务审批
3. 触发任务续执行
4. 获取阻塞任务列表

设计原则：
- 路由只做请求解析和响应格式化
- 所有业务逻辑在此服务中处理
- 协调 TaskService、TaskApprovalService、TaskContinuationService
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Task
from src.services.task_service import TaskService
from src.tasks.services.approval_service import TaskApprovalService
from src.tasks.services.continuation_service import TaskContinuationService

logger = logging.getLogger(__name__)


class TaskAppService:
    """
    任务应用服务

    封装任务相关的业务逻辑，协调任务服务和看门狗服务。

    核心职责：
    1. 启动任务执行
    2. 处理任务审批
    3. 触发任务续执行
    4. 获取阻塞任务列表
    """

    def __init__(self, session: AsyncSession):
        """
        初始化任务应用服务

        Args:
            session: 数据库会话
        """
        self.session = session
        self.task_service = TaskService(session)
        self.approval_service = TaskApprovalService()
        self.continuation_service = TaskContinuationService()

    async def start_task_execution(
        self,
        task_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """
        手动启动任务执行

        通过看门狗服务手动触发任务执行，适用于状态为 pending 的任务。

        Args:
            task_id: 任务 ID
            user_id: 用户 ID

        Returns:
            任务启动响应

        Raises:
            HTTPException: 任务不存在、状态不允许或服务不可用
        """
        from fastapi import HTTPException, status

        try:
            # 验证任务存在且属于当前用户
            result = await self.session.execute(
                select(Task).where(Task.id == task_id, Task.user_id == user_id)
            )
            task = result.scalar_one_or_none()

            if not task:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在或无权访问"
                )

            # 检查任务状态
            if task.status not in ["pending", "failed"]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"任务状态为 {task.status}，只有 pending 或 failed 状态的任务可以启动",
                )

            # 确定项目 ID（如果任务有父任务，使用父任务 ID；否则使用自身 ID）
            project_id = task.parent_task_id if task.parent_task_id else task_id

            # 获取看门狗服务管理器
            from src.services.watchdog_service import get_watchdog_manager

            watchdog_manager = get_watchdog_manager()
            if not watchdog_manager or not watchdog_manager.is_started():
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="看门狗服务未启动",
                )

            # 获取自动执行看门狗
            auto_execute_watchdog = watchdog_manager.get_auto_execute_watchdog()
            if not auto_execute_watchdog:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="自动执行看门狗服务未启动",
                )

            # 手动触发任务执行
            trigger_result = await auto_execute_watchdog.manual_trigger(project_id)

            # 根据触发结果返回响应
            if trigger_result.get("task_triggered"):
                return {
                    "success": True,
                    "message": "任务已启动",
                    "task_id": trigger_result.get("task_id"),
                    "project_id": project_id,
                    "status": "running",
                }
            elif trigger_result.get("reason") == "no_next_task":
                return {
                    "success": False,
                    "message": "项目中没有待执行的任务",
                    "task_id": task_id,
                    "project_id": project_id,
                    "status": task.status,
                }
            else:
                error = trigger_result.get("error", "未知错误")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"启动任务失败: {error}",
                )

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(
                f"[start_task_execution] 启动任务失败 | task_id={task_id} | error={e}"
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"启动任务失败: {str(e)}",
            )

    async def get_blocked_tasks(
        self,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        获取待审核任务列表

        返回所有状态为 blocked 的任务，需要人工审批。

        Args:
            user_id: 用户 ID
            limit: 返回数量限制

        Returns:
            待审核任务列表
        """
        tasks = await self.approval_service.get_blocked_tasks(
            user_id=user_id, limit=limit
        )
        return tasks

    async def process_task_approval(
        self,
        task_id: str,
        action: str,
        reason: str | None,
        user_id: str,
    ) -> dict[str, Any]:
        """
        处理任务审批

        对阻塞状态的任务进行审批决策。

        审批动作：
        - manual_verify: 手动验证通过，任务完成
        - adjust_criteria: 调整标准后重试，重置重试次数
        - cancel_task: 取消任务
        - force_complete: 强制完成

        Args:
            task_id: 任务 ID
            action: 审批动作
            reason: 审批原因/备注
            user_id: 用户 ID

        Returns:
            审批结果

        Raises:
            HTTPException: 审批失败
        """
        from fastapi import HTTPException, status

        result = await self.approval_service.process_decision(
            task_id=task_id,
            action=action,
            reason=reason,
            user_id=user_id,
        )

        if "error" in result:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result["error"],
            )

        return result

    async def trigger_task_continuation(
        self,
        task_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """
        手动触发任务续执行

        当 Agent 一轮对话结束但任务未完成时，可手动触发续执行。

        Args:
            task_id: 任务 ID
            user_id: 用户 ID

        Returns:
            续执行结果

        Raises:
            HTTPException: 任务不存在或状态不允许
        """
        from fastapi import HTTPException, status

        # 验证任务存在且属于当前用户
        result = await self.session.execute(
            select(Task).where(Task.id == task_id, Task.user_id == user_id)
        )
        task = result.scalar_one_or_none()

        if not task:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="任务不存在或无权访问",
            )

        # 检查任务状态
        if task.status != "running":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"任务状态为 {task.status}，只有 running 状态的任务可以续执行",
            )

        result = await self.continuation_service.trigger_continuation(
            task_id=task_id,
            session_id=task.session_id,
        )

        if "error" in result:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=result["error"],
            )

        return result
