"""
任务管理工具

提供任务的创建、查询、更新功能
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.results import ToolExecutionResult
from src.db.models import Task
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class TaskTool:
    """
    任务管理工具

    提供：
    - 创建任务
    - 查询任务
    - 更新任务状态
    - 列出任务列表
    """

    def __init__(self, session: AsyncSession):
        """
        初始化任务工具

        Args:
            session: 数据库会话
        """
        self.session = session

    @staticmethod
    def get_tool_definition() -> Tool:
        """
        获取工具定义

        Returns:
            工具定义
        """
        from src.tools.types import ToolLevel

        return Tool(
            name="task_manage",
            description="任务管理工具：L1 Agent专用，用于查询和更新任务状态。支持操作：get(查询任务详情)、list(列出任务列表)、update(更新任务状态)、status(查看任务状态概览)。注意：仅限L1使用，L2/L3不可用；不包含创建功能(使用task_submit)和删除功能。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "update", "list", "status"],
                        "description": "操作类型：get-查询单个任务详情，list-列出任务列表，update-更新任务状态，status-查看任务状态概览",
                    },
                    "task_scope": {
                        "type": "string",
                        "enum": ["all", "long_term", "short_term"],
                        "description": "任务范围过滤：all-全部任务，long_term-长期任务，short_term-短期任务",
                        "default": "all",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "任务ID，get/update操作时必填",
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "running",
                            "completed",
                            "failed",
                            "blocked",
                        ],
                        "description": "任务状态：pending-待处理，running-执行中，completed-已完成，failed-失败，blocked-阻塞",
                    },
                    "include_details": {
                        "type": "boolean",
                        "description": "是否包含详细信息，默认为false",
                        "default": False,
                    },
                    "include_agent_calls": {
                        "type": "boolean",
                        "description": "是否包含Agent调用记录，默认为false",
                        "default": False,
                    },
                    "parent_task_id": {
                        "type": "string",
                        "description": "父任务ID，用于筛选子任务",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目ID，用于筛选特定项目的任务",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "优先级，范围0-10，数字越大优先级越高，默认为5",
                        "default": 5,
                        "minimum": 0,
                        "maximum": 10,
                    },
                    "due_date": {
                        "type": "string",
                        "description": "截止日期，ISO 8601格式，如2024-01-01T00:00:00",
                        "format": "date-time",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "任务标签列表，用于分类和筛选",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "用户ID，用于数据隔离",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "任务的元数据，可存储额外信息",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回数量限制，默认为50，最大100",
                        "default": 50,
                        "maximum": 100,
                    },
                },
                "required": ["action"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.SYSTEM,
            requires_approval=False,
            dangerous_operations=[],
            tags=["task", "management", "L1", "status"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        执行工具

        Args:
            inputs: 输入参数

        Returns:
            执行结果
        """
        action = inputs.get("action")
        parent_agent_level = inputs.get("parent_agent_level", 1)

        # 层级权限验证：只有L1能使用此工具
        if parent_agent_level != 1:
            return create_failure_result(
                error=f"只有L1 Agent能使用task_manage工具，当前层级：L{parent_agent_level}",
                error_code="INSUFFICIENT_PERMISSION",
            )

        if action == "get":
            return await self._get_task(inputs)
        elif action == "update":
            return await self._update_task(inputs)
        elif action == "list":
            return await self._list_tasks(inputs)
        elif action == "status":
            return await self._get_task_status(inputs)
        else:
            return create_failure_result(
                error=f"不支持的操作: {action}",
                error_code="INVALID_ACTION",
            )

    async def _get_task_status(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        获取任务状态概览

        Args:
            inputs: 输入参数

        Returns:
            任务状态概览
        """
        try:
            task_scope = inputs.get("task_scope", "all")
            project_id = inputs.get("project_id")
            limit = inputs.get("limit", 50)

            # 构建查询
            query = select(Task).limit(limit)

            # 任务范围过滤
            if task_scope != "all":
                # 假设metadata中存储了task_scope信息
                query = query.where(
                    Task.task_metadata.op("->>")("task_scope") == task_scope
                )

            if project_id:
                query = query.where(
                    Task.task_metadata.op("->>")("project_id") == project_id
                )

            # 执行查询
            result = await self.session.execute(query)
            tasks = result.scalars().all()

            # 统计信息
            status_counts = {}
            for task in tasks:
                status = task.status
                status_counts[status] = status_counts.get(status, 0) + 1

            # 最近任务
            recent_tasks = [
                {
                    "task_id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "task_scope": (
                        task.task_metadata.get("task_scope")
                        if task.task_metadata
                        else None
                    ),
                    "created_at": task.created_at.isoformat(),
                    "updated_at": (
                        task.updated_at.isoformat() if task.updated_at else None
                    ),
                }
                for task in tasks[:10]  # 最近10个
            ]

            return create_success_result(
                data={
                    "summary": {
                        "total_tasks": len(tasks),
                        "status_counts": status_counts,
                        "task_scope": task_scope,
                    },
                    "recent_tasks": recent_tasks,
                },
                metadata={"action": "get_task_status"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"获取任务状态失败: {str(e)}",
                error_code="STATUS_FAILED",
            )

    async def _get_task(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        获取任务

        Args:
            inputs: 输入参数

        Returns:
            任务信息
        """
        try:
            task_id = inputs.get("task_id")
            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            result = await self.session.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()

            if not task:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            return create_success_result(
                data={
                    "task_id": task.id,
                    "title": task.title,
                    "description": task.description,
                    "status": task.status,
                    "parent_task_id": task.parent_task_id,
                    "metadata": task.task_metadata,
                    "created_at": task.created_at.isoformat(),
                    "updated_at": (
                        task.updated_at.isoformat() if task.updated_at else None
                    ),
                },
                metadata={"action": "get_task"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"获取任务失败: {str(e)}",
                error_code="GET_FAILED",
            )

    async def _update_task(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        更新任务

        Args:
            inputs: 输入参数

        Returns:
            更新结果
        """
        try:
            task_id = inputs.get("task_id")
            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            # 构建更新数据
            update_data = {"updated_at": datetime.now()}

            if "status" in inputs:
                update_data["status"] = inputs["status"]

            if "description" in inputs:
                update_data["description"] = inputs["description"]

            if "metadata" in inputs:
                update_data["metadata"] = inputs["metadata"]

            # 执行更新
            await self.session.execute(
                update(Task).where(Task.id == task_id).values(**update_data)
            )

            return create_success_result(
                data={"task_id": task_id, "updated": True},
                metadata={"action": "update_task"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"更新任务失败: {str(e)}",
                error_code="UPDATE_FAILED",
            )

    async def _list_tasks(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        列出任务列表

        Args:
            inputs: 输入参数

        Returns:
            任务列表
        """
        try:
            # 获取过滤条件
            status = inputs.get("status")
            parent_task_id = inputs.get("parent_task_id")
            limit = inputs.get("limit", 50)

            # 构建查询
            query = select(Task).limit(limit)

            if status:
                query = query.where(Task.status == status)

            if parent_task_id:
                query = query.where(Task.parent_task_id == parent_task_id)

            # 执行查询
            result = await self.session.execute(query)
            tasks = result.scalars().all()

            # 转换为字典列表
            task_list = [
                {
                    "task_id": task.id,
                    "title": task.title,
                    "description": task.description,
                    "status": task.status,
                    "parent_task_id": task.parent_task_id,
                    "created_at": task.created_at.isoformat(),
                    "updated_at": (
                        task.updated_at.isoformat() if task.updated_at else None
                    ),
                }
                for task in tasks
            ]

            return create_success_result(
                data={
                    "tasks": task_list,
                    "count": len(task_list),
                },
                metadata={"action": "list_tasks"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"列出任务失败: {str(e)}",
                error_code="LIST_FAILED",
            )
