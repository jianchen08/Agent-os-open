"""
任务管理工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- TaskTool：任务管理工具类

使用 tasks.service.TaskService（JSON 文件存储）进行任务 CRUD 和状态管理。
"""

import logging
import shutil
from pathlib import Path
from typing import Any

from core.results import ToolExecutionResult
from tasks.service import TaskService
from tasks.state_machine import InvalidTransitionError
from tasks.types import TaskModel, TaskStatus
from tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class TaskTool:
    """任务管理工具。

    提供：
    - 查询任务（get、list、status）
    - 状态更新（update）
    - 任务控制（pause、resume、cancel、retry）
    - 删除任务（delete）
    - 注入指令（inject）

    权限规则：
    - L1：可管理所属 session_id 的所有任务
    - L2：只能管理自己提交的子任务
    """

    def __init__(self) -> None:
        """初始化任务管理工具。"""
        self._task_service: TaskService | None = None

    def _get_task_service(self) -> TaskService:
        """获取共享的 TaskService 实例。

        获取优先级：
        1. 缓存的实例（已被外部注入）
        2. sys._agent_os_task_service（CLI 设置的全局共享实例）
        3. 创建新实例（降级兜底）

        Returns:
            TaskService 实例

        Raises:
            RuntimeError: TaskService 创建失败
        """
        if self._task_service is not None:
            return self._task_service
        try:
            import sys
            global_ts = getattr(sys, "_agent_os_task_service", None)
            if global_ts is not None:
                self._task_service = global_ts
                return self._task_service
            self._task_service = TaskService()
            return self._task_service
        except Exception as e:
            logger.error("[TaskTool] TaskService 创建失败: %s", e)
            raise RuntimeError(f"任务服务初始化失败: {e}") from e

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="task_manage",
            description="任务管理工具：用于查询和控制任务状态。支持操作：get(查询详情)、list(列出列表)、update(更新状态)、status(状态概览)、pause(暂停)、resume(继续)、cancel(取消)、retry(重试)、delete(删除)、inject(向运行中的子任务注入指令)。权限：L1可管理所属会话的所有任务，L2只能管理自己提交的子任务。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "update", "list", "status", "pause", "resume", "cancel", "retry", "delete", "inject"],
                        "description": "操作类型：get-查询单个任务详情，list-列出任务列表，update-更新任务状态，status-查看任务状态概览，pause-暂停任务，resume-继续执行，cancel-取消任务，retry-重试任务，delete-删除任务，inject-向运行中的子任务注入指令",
                    },
                    "task_scope": {
                        "type": "string",
                        "enum": ["all", "long_term", "short_term"],
                        "description": "任务范围过滤：all-全部任务，long_term-长期任务，short_term-短期任务",
                        "default": "all",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "任务ID，get/update/pause/resume/cancel/retry/delete/inject操作时必填",
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "scheduled",
                            "running",
                            "evaluating",
                            "suspended",
                            "blocked",
                            "completed",
                            "failed",
                            "cancelled",
                            "timeout",
                        ],
                        "description": "任务状态（update操作时使用）",
                    },
                    "reason": {
                        "type": "string",
                        "description": "操作原因说明（pause/cancel/retry时可选）",
                    },
                    "message": {
                        "type": "string",
                        "description": "注入的指令内容（inject操作时必填）",
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
                    "session_id": {
                        "type": "string",
                        "description": "会话ID，用于筛选特定会话的任务",
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
            tags=["task", "management", "L1", "L2", "status", "control"],
            injected_params=["session_id", "user_id", "task_id", "_session"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行任务管理操作。

        Args:
            inputs: 工具输入参数，必须包含 action 字段

        Returns:
            工具执行结果
        """
        action = inputs.get("action")
        parent_agent_level = inputs.get("parent_agent_level", 1)

        try:
            self._get_task_service()
        except RuntimeError as e:
            return create_failure_result(
                error=str(e),
                error_code="SERVICE_UNAVAILABLE",
            )

        if action == "get":
            return await self._get_task(inputs, parent_agent_level)
        elif action == "update":
            return await self._update_task(inputs, parent_agent_level)
        elif action == "list":
            return await self._list_tasks(inputs, parent_agent_level)
        elif action == "status":
            return await self._get_task_status(inputs, parent_agent_level)
        elif action == "pause":
            return await self._pause_task(inputs, parent_agent_level)
        elif action == "resume":
            return await self._resume_task(inputs, parent_agent_level)
        elif action == "cancel":
            return await self._cancel_task(inputs, parent_agent_level)
        elif action == "retry":
            return await self._retry_task(inputs, parent_agent_level)
        elif action == "delete":
            return await self._delete_task(inputs, parent_agent_level)
        elif action == "inject":
            return await self._inject_task(inputs, parent_agent_level)
        else:
            return create_failure_result(
                error=f"不支持的操作: {action}",
                error_code="INVALID_ACTION",
            )

    @staticmethod
    def _check_permission(
        task: TaskModel,
        parent_agent_level: int,
        inputs: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """检查任务操作权限。

        Args:
            task: 任务模型
            parent_agent_level: 父 Agent 层级
            inputs: 工具输入参数

        Returns:
            (是否有权限, 错误消息)
        """
        if parent_agent_level == 1:
            session_id = inputs.get("session_id")
            if session_id and task.metadata.get("session_id") != session_id:
                return False, (
                    f"任务不属于当前会话：task.session_id={task.metadata.get('session_id')}，"
                    f"当前 session_id={session_id}"
                )
            return True, None
        elif parent_agent_level == 2:
            parent_task_id = inputs.get("parent_task_id")
            if parent_task_id:
                if task.parent_task_id == parent_task_id:
                    return True, None
                return False, (
                    f"L2 只能管理自己提交的子任务：task.parent_task_id={task.parent_task_id}，"
                    f"当前 parent_task_id={parent_task_id}"
                )
            return False, "L2 缺少 parent_task_id 参数，无法验证权限"
        else:
            return False, f"只有 L1 和 L2 Agent 能使用 task_manage 工具，当前层级：L{parent_agent_level}"

    def _get_all_tasks(self, limit: int = 50) -> list[TaskModel]:
        """获取全部任务列表。

        Args:
            limit: 返回数量限制

        Returns:
            任务模型列表（按创建时间倒序）
        """
        service = self._get_task_service()
        all_tasks = list(service._storage._tasks.values())
        all_tasks.sort(key=lambda t: t.created_at, reverse=True)
        return all_tasks[:limit]

    def _task_to_dict(self, task: TaskModel) -> dict[str, Any]:
        """将 TaskModel 转换为工具返回的字典格式。

        Args:
            task: 任务模型

        Returns:
            可序列化的任务字典
        """
        return {
            "task_id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status.value,
            "parent_task_id": task.parent_task_id,
            "target_type": task.target_type,
            "priority": task.priority.value if hasattr(task.priority, "value") else task.priority,
            "metadata": task.metadata,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "started_at": task.started_at,
            "completed_at": task.completed_at,
            "error": task.error,
            "result": task.result,
        }

    async def _get_task_status(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """获取任务状态概览。

        Args:
            inputs: 工具输入参数
            parent_agent_level: 父 Agent 层级

        Returns:
            包含状态统计和最近任务的执行结果
        """
        try:
            task_scope = inputs.get("task_scope", "all")
            project_id = inputs.get("project_id")
            limit = inputs.get("limit", 50)

            tasks = self._get_all_tasks(limit)

            # 按权限和条件过滤
            filtered_tasks = []
            for task in tasks:
                if parent_agent_level == 1:
                    session_id = inputs.get("session_id")
                    if session_id and task.metadata.get("session_id") != session_id:
                        continue
                elif parent_agent_level == 2:
                    parent_task_id = inputs.get("parent_task_id")
                    if parent_task_id and task.parent_task_id != parent_task_id:
                        continue

                if task_scope != "all":
                    scope = task.metadata.get("task_scope", "short_term")
                    if scope != task_scope:
                        continue

                if project_id:
                    meta_project = task.metadata.get("project_id")
                    if meta_project != project_id:
                        continue

                filtered_tasks.append(task)

            # 统计各状态数量
            status_counts: dict[str, int] = {}
            for task in filtered_tasks:
                status = task.status.value
                status_counts[status] = status_counts.get(status, 0) + 1

            # 最近 10 条任务
            recent_tasks = [
                {
                    "task_id": task.id,
                    "title": task.title,
                    "status": task.status.value,
                    "task_scope": task.metadata.get("task_scope"),
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                }
                for task in filtered_tasks[:10]
            ]

            return create_success_result(
                data={
                    "summary": {
                        "total_tasks": len(filtered_tasks),
                        "status_counts": status_counts,
                        "task_scope": task_scope,
                        "agent_level": f"L{parent_agent_level}",
                    },
                    "recent_tasks": recent_tasks,
                    "hint": "任务正在后台执行中，请勿频繁调用此工具查看状态，任务完成后会自动更新。",
                },
                metadata={"action": "get_task_status"},
            )

        except Exception as e:
            logger.error("[TaskTool] 获取任务状态失败: %s", e)
            return create_failure_result(
                error=f"获取任务状态失败: {str(e)}",
                error_code="STATUS_FAILED",
            )

    async def _get_task(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """查询单个任务详情。

        Args:
            inputs: 工具输入参数
            parent_agent_level: 父 Agent 层级

        Returns:
            任务详情或错误结果
        """
        try:
            task_id = inputs.get("task_id")
            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            service = self._get_task_service()
            task = service.get_task(task_id)

            if not task:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            has_permission, error_msg = self._check_permission(
                task, parent_agent_level, inputs
            )
            if not has_permission:
                return create_failure_result(
                    error=error_msg,
                    error_code="INSUFFICIENT_PERMISSION",
                )

            task_dict = self._task_to_dict(task)
            task_dict["hint"] = "任务正在后台执行中，请勿频繁调用此工具查看状态，任务完成后会自动更新。"

            return create_success_result(
                data=task_dict,
                metadata={"action": "get_task"},
            )

        except Exception as e:
            logger.error("[TaskTool] 获取任务失败: %s", e)
            return create_failure_result(
                error=f"获取任务失败: {str(e)}",
                error_code="GET_FAILED",
            )

    async def _update_task(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """更新任务状态。

        Args:
            inputs: 工具输入参数
            parent_agent_level: 父 Agent 层级

        Returns:
            更新结果或错误
        """
        try:
            task_id = inputs.get("task_id")
            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            service = self._get_task_service()
            task = service.get_task(task_id)

            if not task:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            has_permission, error_msg = self._check_permission(
                task, parent_agent_level, inputs
            )
            if not has_permission:
                return create_failure_result(
                    error=error_msg,
                    error_code="INSUFFICIENT_PERMISSION",
                )

            new_status = inputs.get("status")
            if new_status:
                if new_status == "completed":
                    return create_failure_result(
                        error="任务完成只能通过评估系统（task_evaluate）完成，禁止通过 task_manage 强制标记完成",
                        error_code="FORBIDDEN_COMPLETE",
                    )

                try:
                    target_status = TaskStatus(new_status)
                except ValueError:
                    return create_failure_result(
                        error=f"无效的任务状态: {new_status}",
                        error_code="INVALID_STATUS",
                    )

                # 检查状态转换合法性
                if not service._state_machine.can_transition(task.status, target_status):
                    valid = [s.value for s in service._state_machine.TRANSITIONS.get(task.status, [])]
                    return create_failure_result(
                        error=f"非法状态转换: {task.status.value} -> {new_status}。当前状态可转换为: {valid}",
                        error_code="INVALID_TRANSITION",
                    )

                old_status = task.status.value
                service._state_machine.transition(task, target_status)
                service._storage.save(task)

                return create_success_result(
                    data={
                        "task_id": task_id,
                        "updated": True,
                        "old_status": old_status,
                        "new_status": new_status,
                    },
                    metadata={"action": "update_task"},
                )

            return create_failure_result(
                error="未指定要更新的字段",
                error_code="NOTHING_TO_UPDATE",
            )

        except InvalidTransitionError as e:
            return create_failure_result(
                error=f"状态转换失败: {e}",
                error_code="INVALID_TRANSITION",
            )
        except Exception as e:
            logger.error("[TaskTool] 更新任务失败: %s", e)
            return create_failure_result(
                error=f"更新任务失败: {str(e)}",
                error_code="UPDATE_FAILED",
            )

    async def _list_tasks(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """列出任务列表。

        Args:
            inputs: 工具输入参数
            parent_agent_level: 父 Agent 层级

        Returns:
            任务列表或错误结果
        """
        try:
            status_filter = inputs.get("status")
            session_id = inputs.get("session_id")
            user_parent_task_id = inputs.get("parent_task_id")
            limit = inputs.get("limit", 50)

            tasks = self._get_all_tasks(limit)

            # 过滤
            filtered = []
            for task in tasks:
                if status_filter and task.status.value != status_filter:
                    continue

                if parent_agent_level == 1 and session_id:
                    if task.metadata.get("session_id") != session_id:
                        continue
                elif parent_agent_level == 2 and inputs.get("parent_task_id"):
                    if task.parent_task_id != inputs["parent_task_id"]:
                        continue

                if user_parent_task_id and parent_agent_level == 1:
                    if task.parent_task_id != user_parent_task_id:
                        continue

                filtered.append(task)

            task_ids = [t.id for t in filtered]
            titles = [t.title for t in filtered]
            statuses = [t.status.value for t in filtered]
            priorities = [
                t.priority.value if hasattr(t.priority, "value") else t.priority
                for t in filtered
            ]
            target_names = [t.metadata.get("target_name", "") for t in filtered]

            return create_success_result(
                data={
                    "h": ["task_id", "title", "status", "priority", "target"],
                    "d": [
                        [task_ids[i], titles[i], statuses[i], priorities[i], target_names[i]]
                        for i in range(len(task_ids))
                    ],
                    "c": len(task_ids),
                    "agent_level": f"L{parent_agent_level}",
                    "hint": "任务正在后台执行中，请勿频繁调用此工具查看状态，任务完成后会自动更新。",
                },
                metadata={"action": "list_tasks"},
            )

        except Exception as e:
            logger.error("[TaskTool] 列出任务失败: %s", e)
            return create_failure_result(
                error=f"列出任务失败: {str(e)}",
                error_code="LIST_FAILED",
            )

    async def _pause_task(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """暂停任务（running -> paused）。

        Args:
            inputs: 工具输入参数
            parent_agent_level: 父 Agent 层级

        Returns:
            暂停结果或错误
        """
        try:
            task_id = inputs.get("task_id")
            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            service = self._get_task_service()
            task = service.get_task(task_id)

            if not task:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            has_permission, error_msg = self._check_permission(
                task, parent_agent_level, inputs
            )
            if not has_permission:
                return create_failure_result(
                    error=error_msg,
                    error_code="INSUFFICIENT_PERMISSION",
                )

            if task.status != TaskStatus.RUNNING:
                return create_failure_result(
                    error=f"只有运行中的任务才能暂停，当前状态: {task.status.value}",
                    error_code="INVALID_STATUS",
                )

            reason = inputs.get("reason", "用户请求暂停")
            service.pause_task(task_id)

            return create_success_result(
                data={
                    "task_id": task_id,
                    "paused": True,
                    "old_status": TaskStatus.RUNNING.value,
                    "new_status": TaskStatus.PAUSED.value,
                    "reason": reason,
                },
                metadata={"action": "pause_task"},
            )

        except InvalidTransitionError as e:
            return create_failure_result(
                error=f"暂停失败（状态转换不合法）: {e}",
                error_code="INVALID_TRANSITION",
            )
        except Exception as e:
            logger.error("[TaskTool] 暂停任务失败: %s", e)
            return create_failure_result(
                error=f"暂停任务失败: {str(e)}",
                error_code="PAUSE_FAILED",
            )

    async def _resume_task(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """恢复任务（paused -> running）。

        Args:
            inputs: 工具输入参数
            parent_agent_level: 父 Agent 层级

        Returns:
            恢复结果或错误
        """
        try:
            task_id = inputs.get("task_id")
            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            service = self._get_task_service()
            task = service.get_task(task_id)

            if not task:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            has_permission, error_msg = self._check_permission(
                task, parent_agent_level, inputs
            )
            if not has_permission:
                return create_failure_result(
                    error=error_msg,
                    error_code="INSUFFICIENT_PERMISSION",
                )

            if task.status != TaskStatus.PAUSED:
                return create_failure_result(
                    error=f"只有暂停状态的任务才能恢复，当前状态: {task.status.value}",
                    error_code="INVALID_STATUS",
                )

            reason = inputs.get("reason", "用户请求继续执行")
            service.resume_task(task_id)

            return create_success_result(
                data={
                    "task_id": task_id,
                    "resumed": True,
                    "old_status": TaskStatus.PAUSED.value,
                    "new_status": TaskStatus.RUNNING.value,
                    "reason": reason,
                },
                metadata={"action": "resume_task"},
            )

        except InvalidTransitionError as e:
            return create_failure_result(
                error=f"恢复失败（状态转换不合法）: {e}",
                error_code="INVALID_TRANSITION",
            )
        except Exception as e:
            logger.error("[TaskTool] 恢复任务失败: %s", e)
            return create_failure_result(
                error=f"恢复任务失败: {str(e)}",
                error_code="RESUME_FAILED",
            )

    async def _cancel_task(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """取消任务。

        将任务状态设为 failed 并记录取消原因。
        注意：TaskStatus 中没有 cancelled 状态，使用 failed 替代。

        Args:
            inputs: 工具输入参数
            parent_agent_level: 父 Agent 层级

        Returns:
            取消结果或错误
        """
        try:
            task_id = inputs.get("task_id")
            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            service = self._get_task_service()
            task = service.get_task(task_id)

            if not task:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            has_permission, error_msg = self._check_permission(
                task, parent_agent_level, inputs
            )
            if not has_permission:
                return create_failure_result(
                    error=error_msg,
                    error_code="INSUFFICIENT_PERMISSION",
                )

            # 只有非终态任务可以取消
            cancellable_statuses = {
                TaskStatus.PENDING, TaskStatus.RUNNING,
                TaskStatus.PAUSED, TaskStatus.EVALUATING,
            }
            if task.status not in cancellable_statuses:
                return create_failure_result(
                    error=f"当前状态无法取消: {task.status.value}",
                    error_code="INVALID_STATUS",
                )

            reason = inputs.get("reason", "用户请求取消")
            old_status = task.status.value

            # 通过 fail_task 设置为 failed 状态
            service.fail_task(task_id, error=f"已取消: {reason}")

            return create_success_result(
                data={
                    "task_id": task_id,
                    "cancelled": True,
                    "old_status": old_status,
                    "new_status": TaskStatus.FAILED.value,
                    "reason": reason,
                },
                metadata={"action": "cancel_task"},
            )

        except InvalidTransitionError as e:
            return create_failure_result(
                error=f"取消失败（状态转换不合法）: {e}",
                error_code="INVALID_TRANSITION",
            )
        except Exception as e:
            logger.error("[TaskTool] 取消任务失败: %s", e)
            return create_failure_result(
                error=f"取消任务失败: {str(e)}",
                error_code="CANCEL_FAILED",
            )

    async def _retry_task(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """重试任务（failed -> pending）。

        Args:
            inputs: 工具输入参数
            parent_agent_level: 父 Agent 层级

        Returns:
            重试结果或错误
        """
        try:
            task_id = inputs.get("task_id")
            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            service = self._get_task_service()
            task = service.get_task(task_id)

            if not task:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            has_permission, error_msg = self._check_permission(
                task, parent_agent_level, inputs
            )
            if not has_permission:
                return create_failure_result(
                    error=error_msg,
                    error_code="INSUFFICIENT_PERMISSION",
                )

            if task.status != TaskStatus.FAILED:
                return create_failure_result(
                    error=f"只有失败的任务才能重试，当前状态: {task.status.value}",
                    error_code="INVALID_STATUS",
                )

            reason = inputs.get("reason", "用户请求重试")
            old_status = task.status.value

            # 利用状态机从 failed -> pending
            service._transition_with_callback(task, TaskStatus.PENDING)
            task.error = None
            service._storage.save(task)

            return create_success_result(
                data={
                    "task_id": task_id,
                    "retried": True,
                    "old_status": old_status,
                    "new_status": TaskStatus.PENDING.value,
                    "reason": reason,
                },
                metadata={"action": "retry_task"},
            )

        except InvalidTransitionError as e:
            return create_failure_result(
                error=f"重试失败（状态转换不合法）: {e}",
                error_code="INVALID_TRANSITION",
            )
        except Exception as e:
            logger.error("[TaskTool] 重试任务失败: %s", e)
            return create_failure_result(
                error=f"重试任务失败: {str(e)}",
                error_code="RETRY_FAILED",
            )

    async def _inject_task(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """向运行中的子任务注入指令。

        Args:
            inputs: 工具输入参数
            parent_agent_level: 父 Agent 层级

        Returns:
            注入结果或错误
        """
        try:
            task_id = inputs.get("task_id")
            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            message = inputs.get("message")
            if not message:
                return create_failure_result(
                    error="注入内容不能为空",
                    error_code="MISSING_MESSAGE",
                )

            service = self._get_task_service()
            task = service.get_task(task_id)

            if not task:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            has_permission, error_msg = self._check_permission(
                task, parent_agent_level, inputs
            )
            if not has_permission:
                return create_failure_result(
                    error=error_msg,
                    error_code="INSUFFICIENT_PERMISSION",
                )

            if task.status not in [TaskStatus.RUNNING, TaskStatus.PAUSED]:
                return create_failure_result(
                    error=f"只能向运行中或暂停的任务注入指令，当前状态: {task.status.value}",
                    error_code="INVALID_STATUS",
                )

            target_session_id = task.metadata.get("session_id")
            target_execution_id = task.execution_record_id

            if not target_session_id:
                return create_failure_result(
                    error="任务缺少 session_id，无法注入",
                    error_code="MISSING_SESSION_ID",
                )

            from triggers.message_queue import (
                TriggerMessage,
                create_message_id,
                get_trigger_message_queue,
            )

            queue = get_trigger_message_queue()
            trigger_message = TriggerMessage(
                id=create_message_id(),
                session_id=target_session_id,
                execution_id=target_execution_id or "",
                content=message,
                priority=100,
                metadata={
                    "source": "task_inject",
                    "injected_by": f"L{parent_agent_level}",
                    "task_id": task_id,
                },
            )

            success = queue.push(trigger_message)
            if not success:
                return create_failure_result(
                    error="消息队列已满，注入失败",
                    error_code="QUEUE_FULL",
                )

            logger.info(
                "[TaskTool] 指令注入成功 | task_id=%s | session_id=%s | "
                "execution_id=%s | message_preview=%s...",
                task_id, target_session_id, target_execution_id, message[:50],
            )

            return create_success_result(
                data={
                    "task_id": task_id,
                    "injected": True,
                    "message_id": trigger_message.id,
                    "target_session_id": target_session_id,
                    "target_execution_id": target_execution_id,
                    "message_preview": message[:100],
                },
                metadata={"action": "inject_task"},
            )

        except Exception as e:
            logger.error("[TaskTool] 注入指令失败: %s", e)
            return create_failure_result(
                error=f"注入指令失败: {str(e)}",
                error_code="INJECT_FAILED",
            )

    async def _delete_task(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """删除任务。

        从存储中移除任务，并尝试清理容器和工作空间。

        Args:
            inputs: 工具输入参数
            parent_agent_level: 父 Agent 层级

        Returns:
            删除结果或错误
        """
        try:
            task_id = inputs.get("task_id")
            if not task_id:
                return create_failure_result(
                    error="任务 ID 不能为空",
                    error_code="MISSING_TASK_ID",
                )

            service = self._get_task_service()
            task = service.get_task(task_id)

            if not task:
                return create_failure_result(
                    error=f"任务不存在: {task_id}",
                    error_code="TASK_NOT_FOUND",
                )

            has_permission, error_msg = self._check_permission(
                task, parent_agent_level, inputs
            )
            if not has_permission:
                return create_failure_result(
                    error=error_msg,
                    error_code="INSUFFICIENT_PERMISSION",
                )

            reason = inputs.get("reason", "用户请求删除")
            old_status = task.status.value
            task_title = task.title
            workspace = task.metadata.get("workspace")
            isolation_level = task.metadata.get("isolation_level")

            # 清理关联资源
            cleanup_results = await self._cleanup_task_resources(
                task_id=task_id,
                workspace=workspace,
                isolation_level=isolation_level,
            )

            # 从存储中删除
            service._storage.delete(task_id)

            return create_success_result(
                data={
                    "task_id": task_id,
                    "deleted": True,
                    "old_status": old_status,
                    "title": task_title,
                    "reason": reason,
                    "cleanup": cleanup_results,
                },
                metadata={"action": "delete_task"},
            )

        except Exception as e:
            logger.error("[TaskTool] 删除任务失败: %s", e)
            return create_failure_result(
                error=f"删除任务失败: {str(e)}",
                error_code="DELETE_FAILED",
            )

    async def _cleanup_task_resources(
        self,
        task_id: str,
        workspace: str | None,
        isolation_level: str | None,
    ) -> dict[str, Any]:
        """清理任务相关的资源（容器和工作空间）。

        Args:
            task_id: 任务 ID
            workspace: 工作空间路径
            isolation_level: 隔离级别

        Returns:
            清理结果字典
        """
        cleanup_results: dict[str, Any] = {
            "container_destroyed": False,
            "workspace_cleaned": False,
            "errors": [],
        }

        try:
            from isolation.manager import get_isolation_manager

            await get_isolation_manager()

            container_name = f"cua-{task_id}"
            try:
                from docker.errors import NotFound
                import docker

                client = docker.from_env()
                try:
                    container = client.containers.get(container_name)
                    container.stop(timeout=5)
                    container.remove()
                    cleanup_results["container_destroyed"] = True
                    logger.info("[TaskTool] 已销毁容器: %s", container_name)
                except NotFound:
                    logger.debug("[TaskTool] 容器不存在: %s", container_name)
                except Exception as e:
                    cleanup_results["errors"].append(f"销毁容器失败: {str(e)}")
                    logger.warning("[TaskTool] 销毁容器失败: %s, 错误: %s", container_name, e)
                finally:
                    client.close()
            except ImportError:
                logger.debug("[TaskTool] Docker SDK 未安装，跳过容器清理")
            except Exception as e:
                cleanup_results["errors"].append(f"Docker 操作失败: {str(e)}")
                logger.warning("[TaskTool] Docker 操作失败: %s", e)

        except Exception as e:
            cleanup_results["errors"].append(f"获取隔离管理器失败: {str(e)}")
            logger.warning("[TaskTool] 获取隔离管理器失败: %s", e)

        if workspace:
            try:
                workspace_path = Path(workspace)
                if not workspace_path.is_absolute():
                    workspace_config_root = ".ai_workspaces"
                    workspace_path = Path(workspace_config_root) / workspace

                if workspace_path.exists():
                    shutil.rmtree(str(workspace_path))
                    cleanup_results["workspace_cleaned"] = True
                    logger.info("[TaskTool] 已清理工作空间: %s", workspace_path)
                else:
                    logger.debug("[TaskTool] 工作空间不存在: %s", workspace_path)
            except Exception as e:
                cleanup_results["errors"].append(f"清理工作空间失败: {str(e)}")
                logger.warning("[TaskTool] 清理工作空间失败: %s, 错误: %s", workspace, e)

        return cleanup_results
