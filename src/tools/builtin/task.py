"""
任务管理工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- TaskTool：任务管理工具类

使用 tasks.service.TaskService（JSON 文件存储）进行任务 CRUD 和状态管理。
"""

import logging
import shutil
import subprocess
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
        self._message_queue: Any = None

    def _get_message_queue(self):
        """获取全局 MessageQueue 实例。

        通过 ServiceProvider 统一获取，兼容 ToolCore 注入和 sys 全局变量。

        Returns:
            MessageQueue 实例，获取失败时返回 None
        """
        if self._message_queue is not None:
            return self._message_queue
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
        mq = provider.get("message_queue")
        if mq is not None:
            self._message_queue = mq
        return self._message_queue

    def _get_execution_record_storage(self):
        """获取全局 ExecutionRecordStorage 实例。

        通过 ServiceProvider 统一获取。

        Returns:
            ExecutionRecordStorage 实例，获取失败时返回 None
        """
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
        return provider.get("execution_record_storage")

    @staticmethod
    def _calc_elapsed_seconds(task: TaskModel) -> float | None:
        """计算任务已耗时（秒）。"""
        if not task.started_at:
            return None
        from datetime import datetime
        started = datetime.fromisoformat(task.started_at)
        if task.completed_at:
            completed = datetime.fromisoformat(task.completed_at)
            return (completed - started).total_seconds()
        return (datetime.now() - started).total_seconds()

    @staticmethod
    def _format_elapsed(seconds: float | None) -> str:
        """将秒数格式化为可读字符串。"""
        if seconds is None:
            return "-"
        if seconds < 60:
            return f"{int(seconds)}s"
        minutes = int(seconds / 60)
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        remain_minutes = minutes % 60
        return f"{hours}h{remain_minutes}m"

    def _get_latest_activity(self, task: TaskModel) -> dict | None:
        """获取任务的最新一条执行活动摘要。"""
        storage = self._get_execution_record_storage()
        if not storage or not task.pipeline_run_id:
            return None
        records = storage.list_by_pipeline(task.pipeline_run_id)
        if not records:
            return None
        latest = records[-1]
        return {
            "iteration": latest.iteration,
            "action": latest.name or latest.type,
            "summary": (latest.content or "")[:100],
            "at": latest.created_at,
        }

    def _get_recent_activities(self, task: TaskModel, limit: int = 5) -> list[dict]:
        """获取任务最近 N 条执行活动摘要。"""
        storage = self._get_execution_record_storage()
        if not storage or not task.pipeline_run_id:
            return []
        records = storage.list_by_pipeline(task.pipeline_run_id)
        recent = records[-limit:] if len(records) > limit else records
        recent.reverse()
        return [
            {
                "iteration": r.iteration,
                "action": r.name or ("thinking" if r.type == "ai" else r.type),
                "action_type": r.type,
                "summary": (r.content or "")[:100],
                "at": r.created_at,
            }
            for r in recent
        ]

    def _get_task_service(self) -> TaskService:
        """获取共享的 TaskService 实例。

        通过 ServiceProvider 统一获取，支持显式注册、sys 全局变量和懒加载创建。

        Returns:
            TaskService 实例

        Raises:
            RuntimeError: TaskService 创建失败
        """
        if self._task_service is not None:
            return self._task_service
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
        service = provider.get_or_create("task_service", lambda: TaskService())
        if service is not None:
            self._task_service = service
            return self._task_service
        raise RuntimeError("任务服务初始化失败")

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义"""
        return Tool(
            name="task_manage",
            description=(
                "任务管理工具：用于查询和控制任务的生命周期。\n\n"
                "## 常用场景\n"
                "- **查看下级执行情况**：使用 list + parent_task_id 筛选某容器下的所有子任务，返回每个子任务的状态、最新执行动作和耗时\n"
                "- **查看全局进度**：使用 status 获取状态统计概览和最近任务列表\n"
                "- **查看单个任务详情**：使用 get + include_details=true 展开最近执行活动记录\n"
                "- **干预子任务执行**：使用 inject 向运行中/暂停的子任务注入新指令（如调整方向、补充要求）\n"
                "- **容器任务管理**：L1 使用 complete_container/fail_container 标记容器任务完成或失败\n\n"
                "## 支持的操作\n"
                "- get：查询单个任务详情（include_details=true 可展开执行活动）\n"
                "- list：列出任务列表（支持 parent_task_id 筛选子任务、status 按状态过滤）\n"
                "- status：全局状态概览（各状态统计 + 最近任务摘要）\n"
                "- update：更新任务状态\n"
                "- pause/resume/cancel/retry：任务生命周期控制\n"
                "- inject：向运行中或暂停的子任务注入指令\n"
                "- delete：删除任务\n"
                "- complete_container/fail_container：标记容器完成/失败（仅L1）\n\n"
                "## 权限\n"
                "- L1：可管理所属会话的所有任务\n"
                "- L2：只能管理自己提交的子任务"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "update", "list", "status", "pause", "resume", "cancel", "retry", "delete", "inject", "complete_container", "fail_container"],
                        "description": (
                            "操作类型，根据场景选择：\n"
                            "- get：查询单个任务详情，需要 task_id；加 include_details=true 可展开最近执行活动\n"
                            "- list：列出任务列表；传 parent_task_id 可查看某容器下所有子任务的执行情况（状态+最新动作+耗时）\n"
                            "- status：查看全局状态概览（各状态数量统计 + 最近任务摘要），适合快速了解整体进度\n"
                            "- update：更新任务状态字段\n"
                            "- pause：暂停运行中的任务\n"
                            "- resume：恢复暂停的任务\n"
                            "- cancel：取消任务\n"
                            "- retry：重试失败的任务（重置为pending重新执行）\n"
                            "- inject：向运行中或暂停的子任务注入新指令（如调整方向、补充要求），子任务下一轮会看到该消息\n"
                            "- delete：删除任务及关联资源\n"
                            "- complete_container：标记容器任务完成（仅L1），需所有子任务已到达终态\n"
                            "- fail_container：标记容器任务失败（仅L1）"
                        ),
                    },
                    "task_scope": {
                        "type": "string",
                        "enum": ["all", "container", "non_container"],
                        "description": "任务范围过滤：all-全部任务，container-容器任务，non_container-非容器任务",
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
                        "description": "注入的指令内容（inject操作时必填）。该消息会以user角色注入到子任务的下一轮对话中，子任务Agent会看到并据此调整执行方向。示例：'注意：需求有变更，请改用方案B'、'请优先处理登录模块'",
                    },
                    "container_reason": {
                        "type": "string",
                        "description": "容器操作原因（complete_container/fail_container操作时填写）",
                    },
                    "include_details": {
                        "type": "boolean",
                        "description": "是否包含详细信息（仅get操作生效）。设为true时返回 recent_activities（最近执行活动列表，含迭代轮次、动作名称、内容摘要、时间）和 elapsed_seconds（已耗时）",
                        "default": False,
                    },
                    "include_agent_calls": {
                        "type": "boolean",
                        "description": "是否只返回工具调用类型的活动记录（仅get操作生效，会自动启用详细信息）。设为true时返回 recent_activities 中只包含 action_type=tool 的记录（工具名+输入+输出），不含AI思考内容",
                        "default": False,
                    },
                    "parent_task_id": {
                        "type": "string",
                        "description": "父任务ID。list操作时传入可筛选该容器下的所有子任务，查看每个子任务的状态、最新执行动作和耗时；inject操作时L2需传入以验证权限",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "项目ID，用于筛选特定项目的任务",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "会话ID，用于筛选特定会话的任务",
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
            tags=["task", "management", "L1", "L2", "status", "control"],
            injected_params=["session_id", "user_id", "_session"],
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

        # ToolCore 通过 _SERVICE_INJECT_MAP 自动注入服务到 inputs，
        # 在此捕获并缓存到实例属性，供后续 _get_message_queue() 使用
        if inputs.get("_message_queue") and self._message_queue is None:
            self._message_queue = inputs["_message_queue"]

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
        elif action == "complete_container":
            return await self._complete_container(inputs, parent_agent_level)
        elif action == "fail_container":
            return await self._fail_container(inputs, parent_agent_level)
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

    def _get_all_tasks(self, limit: int = 5) -> list[TaskModel]:
        """获取全部任务列表。

        Args:
            limit: 返回数量限制

        Returns:
            任务模型列表（按创建时间倒序）
        """
        service = self._get_task_service()
        return service.list_all(limit=limit)

    def _task_to_dict(
        self, task: TaskModel, include_details: bool = False, include_agent_calls: bool = False
    ) -> dict[str, Any]:
        """将 TaskModel 转换为工具返回的字典格式。

        BUG-FIX-fix_20260417_task_manage_records: 新增 include_agent_calls 参数，
        之前该参数在 schema 中定义但从未被使用。现在传 include_agent_calls=true 时
        自动启用 include_details，并筛选只返回工具调用类型的活动记录。

        Args:
            task: 任务模型
            include_details: 是否包含详细活动信息
            include_agent_calls: 是否只返回工具调用类型的活动记录

        Returns:
            可序列化的任务字典
        """
        # 精简 metadata：去掉 evaluation_history（按需查看），只保留评估结果摘要
        metadata = dict(task.metadata) if task.metadata else {}
        eval_summary = None
        if "evaluation_history" in metadata:
            history = metadata.pop("evaluation_history")
            if history:
                last = history[-1]
                eval_summary = {
                    "passed": last.get("passed"),
                    "summary": last.get("summary", ""),
                    "attempt_count": len(history),
                }
        if eval_summary:
            metadata["evaluation_summary"] = eval_summary

        result = {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "parent_task_id": task.parent_task_id,
            "priority": task.priority.value if hasattr(task.priority, "value") else task.priority,
            "metadata": metadata,
            "created_at": task.created_at,
            "completed_at": task.completed_at,
            "error": task.error,
        }
        if include_details or include_agent_calls:
            result["elapsed_seconds"] = self._calc_elapsed_seconds(task)
            activities = self._get_recent_activities(task)
            if include_agent_calls and not include_details:
                activities = [a for a in activities if a.get("action_type") == "tool"]
            result["recent_activities"] = activities
        return result

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
            limit = inputs.get("limit", 5)

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
                    scope = task.metadata.get("task_scope", "non_container")
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
                    "elapsed_seconds": self._calc_elapsed_seconds(task),
                    "latest_activity": self._get_latest_activity(task),
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

            task_dict = self._task_to_dict(
                task,
                include_details=inputs.get("include_details", False),
                include_agent_calls=inputs.get("include_agent_calls", False),
            )
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
                if not service.can_transition(task_id, target_status):
                    valid = service.get_valid_transitions(task_id)
                    return create_failure_result(
                        error=f"非法状态转换: {task.status.value} -> {new_status}。当前状态可转换为: {valid}",
                        error_code="INVALID_TRANSITION",
                    )

                old_status = task.status.value
                service.force_transition(task_id, target_status)

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
            limit = inputs.get("limit", 5)

            # 默认只展示自己的子任务：未显式传 parent_task_id 时，
            # 用注入的 task_id（当前任务ID）作为 parent_task_id
            injected_task_id = inputs.get("task_id")
            if not user_parent_task_id:
                if parent_agent_level == 2 and inputs.get("parent_task_id"):
                    user_parent_task_id = inputs["parent_task_id"]
                elif injected_task_id:
                    user_parent_task_id = injected_task_id

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

                if user_parent_task_id:
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
            latest_actions = []
            elapsed_list = []
            for t in filtered:
                activity = self._get_latest_activity(t)
                latest_actions.append(activity["action"] if activity else "-")
                elapsed_list.append(self._format_elapsed(self._calc_elapsed_seconds(t)))

            return create_success_result(
                data={
                    "h": ["task_id", "title", "status", "priority", "target", "latest_action", "elapsed"],
                    "d": [
                        [task_ids[i], titles[i], statuses[i], priorities[i], target_names[i], latest_actions[i], elapsed_list[i]]
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
            service.force_transition(task.id, TaskStatus.PENDING)
            task.error = None
            service.save_task(task)

            # 发出 task.submitted 事件，触发 TaskWorker 重新执行
            execution_warning = None
            target_id = task.metadata.get("target_id", "")
            if target_id:
                try:
                    from infrastructure.service_provider import get_service_provider
                    provider = get_service_provider()
                    event_bus = provider.get("event_bus")
                    if event_bus is not None:
                        if hasattr(event_bus, 'has_subscribers') and not event_bus.has_subscribers("task.submitted"):
                            execution_warning = "后台执行器(TaskWorker)未启动，任务已重置为pending但不会自动执行"
                        else:
                            await event_bus.emit("task.submitted", {
                                "task_id": task.id,
                                "target_type": task.target_type or "agent",
                                "target_id": target_id,
                                "user_input": task.title,
                                "description": task.description,
                                "acceptance_criteria": task.metadata.get("acceptance_criteria", {}),
                                "workspace": task.metadata.get("workspace", ""),
                            })
                            logger.info("[TaskTool] retry 已发出 task.submitted 事件: task_id=%s", task_id)
                    else:
                        execution_warning = "EventBus 不可用，任务已重置为pending但不会自动执行"
                except Exception as emit_exc:
                    logger.warning("[TaskTool] retry 发出 task.submitted 失败: %s", emit_exc)
                    execution_warning = f"事件发送失败: {emit_exc}"

            result_data = {
                "task_id": task_id,
                "retried": True,
                "old_status": old_status,
                "new_status": TaskStatus.PENDING.value,
                "reason": reason,
            }
            if execution_warning:
                result_data["warning"] = execution_warning

            return create_success_result(
                data=result_data,
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

            # BUG-FIX-fix_20260417_task_manage_records: 系统已从 session 改为 pipeline，
            # 投递地址使用 task.pipeline_run_id（子管道的 pipeline_id）而非 session_id
            target_pipeline_id = task.pipeline_run_id
            if not target_pipeline_id:
                return create_failure_result(
                    error="任务尚未启动或 pipeline_run_id 未绑定，无法注入",
                    error_code="MISSING_PIPELINE_ID",
                )

            from infrastructure.message_queue import Message, create_message_id

            queue = self._get_message_queue()
            if not queue:
                return create_failure_result(
                    error="消息队列服务不可用，无法注入",
                    error_code="QUEUE_UNAVAILABLE",
                )

            msg = Message(
                id=create_message_id(),
                pipeline_id=target_pipeline_id,
                target_id=task_id,
                content=message,
                priority=100,
                metadata={
                    "source": "task_inject",
                    "injected_by": f"L{parent_agent_level}",
                    "task_id": task_id,
                },
            )

            success = await queue.push(msg)
            if not success:
                return create_failure_result(
                    error="消息队列已满，注入失败",
                    error_code="QUEUE_FULL",
                )

            logger.info(
                "[TaskTool] 指令注入成功 | task_id=%s | pipeline_id=%s | "
                "message_preview=%s...",
                task_id, target_pipeline_id, message[:50],
            )

            return create_success_result(
                data={
                    "task_id": task_id,
                    "injected": True,
                    "message_id": msg.id,
                    "target_pipeline_id": target_pipeline_id,
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

            if task.metadata.get("task_scope") == "container":
                return create_failure_result(
                    error=f"容器任务不允许直接删除（task_id={task_id}），请使用 complete_container 或 fail_container 管理",
                    error_code="CANNOT_DELETE_CONTAINER",
                )

            reason = inputs.get("reason", "用户请求删除")
            old_status = task.status.value
            task_title = task.title
            workspace = task.metadata.get("workspace")

            # 清理关联资源
            cleanup_results = await self._cleanup_task_resources(
                task_id=task_id,
                workspace=workspace,
            )

            # 从存储中删除
            service.delete_task(task_id)

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

    async def _complete_container(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """标记容器任务完成。

        仅限 L1 主 Agent 调用。将 PENDING 状态的容器标记为 COMPLETED。

        Args:
            inputs: 工具输入参数，需含 task_id
            parent_agent_level: 父 Agent 层级

        Returns:
            操作结果或错误
        """
        from datetime import datetime

        if parent_agent_level != 1:
            return create_failure_result(
                error="容器操作仅限 L1 主 Agent 执行",
                error_code="PERMISSION_DENIED",
            )

        task_id = inputs.get("task_id")
        if not task_id:
            return create_failure_result(
                error="complete_container 操作必须提供 task_id",
                error_code="MISSING_TASK_ID",
            )

        try:
            service = self._get_task_service()
        except RuntimeError as e:
            return create_failure_result(error=str(e), error_code="SERVICE_UNAVAILABLE")

        task = service.get_task(task_id)
        if task is None:
            return create_failure_result(
                error=f"任务不存在: {task_id}",
                error_code="TASK_NOT_FOUND",
            )

        subtasks = service.list_subtasks(task_id)
        if not subtasks:
            return create_failure_result(
                error=f"任务 {task_id} 不是容器任务（无子任务），不能使用容器操作",
                error_code="NOT_A_CONTAINER",
            )

        if task.status != TaskStatus.PENDING:
            return create_failure_result(
                error=f"容器当前状态为 {task.status.value}，只能操作 PENDING 状态的容器",
                error_code="INVALID_STATUS",
            )

        reason = inputs.get("container_reason", inputs.get("reason", ""))

        try:
            service.force_transition(task.id, TaskStatus.COMPLETED)
            task.completed_at = datetime.now().isoformat()
            service.save_task(task)
            logger.info("[TaskTool] 容器已完成: %s — %s", task_id, reason)
            return create_success_result(
                data={
                    "task_id": task.id,
                    "status": "completed",
                    "message": f"容器 {task_id} 已标记为完成",
                    "subtask_count": len(subtasks),
                    "completed_subtasks": sum(
                        1 for s in subtasks if s.status == TaskStatus.COMPLETED
                    ),
                },
                metadata={"action": "complete_container"},
            )
        except InvalidTransitionError as e:
            return create_failure_result(
                error=f"容器完成失败（状态转换不合法）: {e}",
                error_code="INVALID_TRANSITION",
            )
        except Exception as e:
            logger.error("[TaskTool] 容器完成失败: %s", e)
            return create_failure_result(
                error=f"容器完成失败: {str(e)}",
                error_code="CONTAINER_COMPLETE_FAILED",
            )

    async def _fail_container(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """标记容器任务失败。

        仅限 L1 主 Agent 调用。将 PENDING 状态的容器标记为 FAILED。

        Args:
            inputs: 工具输入参数，需含 task_id 和 container_reason
            parent_agent_level: 父 Agent 层级

        Returns:
            操作结果或错误
        """
        from datetime import datetime

        if parent_agent_level != 1:
            return create_failure_result(
                error="容器操作仅限 L1 主 Agent 执行",
                error_code="PERMISSION_DENIED",
            )

        task_id = inputs.get("task_id")
        if not task_id:
            return create_failure_result(
                error="fail_container 操作必须提供 task_id",
                error_code="MISSING_TASK_ID",
            )

        reason = inputs.get("container_reason", inputs.get("reason", ""))
        if not reason:
            return create_failure_result(
                error="fail_container 操作必须提供 container_reason 说明失败原因",
                error_code="MISSING_REASON",
            )

        try:
            service = self._get_task_service()
        except RuntimeError as e:
            return create_failure_result(error=str(e), error_code="SERVICE_UNAVAILABLE")

        task = service.get_task(task_id)
        if task is None:
            return create_failure_result(
                error=f"任务不存在: {task_id}",
                error_code="TASK_NOT_FOUND",
            )

        subtasks = service.list_subtasks(task_id)
        if not subtasks:
            return create_failure_result(
                error=f"任务 {task_id} 不是容器任务（无子任务），不能使用容器操作",
                error_code="NOT_A_CONTAINER",
            )

        if task.status != TaskStatus.PENDING:
            return create_failure_result(
                error=f"容器当前状态为 {task.status.value}，只能操作 PENDING 状态的容器",
                error_code="INVALID_STATUS",
            )

        try:
            service.force_transition(task.id, TaskStatus.FAILED)
            task.completed_at = datetime.now().isoformat()
            task.metadata = task.metadata or {}
            task.metadata["container_reason"] = reason
            service.save_task(task)
            logger.info("[TaskTool] 容器已失败: %s — %s", task_id, reason)
            return create_success_result(
                data={
                    "task_id": task.id,
                    "status": "failed",
                    "message": f"容器 {task_id} 已标记为失败",
                    "reason": reason,
                    "subtask_count": len(subtasks),
                },
                metadata={"action": "fail_container"},
            )
        except InvalidTransitionError as e:
            return create_failure_result(
                error=f"容器失败操作失败（状态转换不合法）: {e}",
                error_code="INVALID_TRANSITION",
            )
        except Exception as e:
            logger.error("[TaskTool] 容器失败操作失败: %s", e)
            return create_failure_result(
                error=f"容器失败操作失败: {str(e)}",
                error_code="CONTAINER_FAIL_FAILED",
            )

    async def _cleanup_task_resources(
        self,
        task_id: str,
        workspace: str | None,
    ) -> dict[str, Any]:
        """清理任务相关的资源（容器和工作空间）。

        容器清理委托给 IsolationManager，不再直接操作 Docker SDK。

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

            manager = await get_isolation_manager()
            destroyed = await manager.destroy_environment(task_id)
            cleanup_results["container_destroyed"] = destroyed
            if destroyed:
                logger.info("[TaskTool] 已通过 IsolationManager 销毁环境: %s", task_id)
        except Exception as e:
            cleanup_results["errors"].append(f"清理隔离环境失败: {str(e)}")
            logger.warning("[TaskTool] 清理隔离环境失败: %s, 错误: %s", task_id, e)

        # 优先使用 lifecycle 进行工作空间清理
        lifecycle_cleaned = False
        try:
            from infrastructure.service_provider import get_service_provider
            provider = get_service_provider()
            lifecycle = provider.get("workspace_lifecycle_manager")
            if lifecycle:
                lifecycle.restore_ws_meta(task_id)
                cleanup_result = lifecycle.cleanup_workspace(task_id)
                if cleanup_result:
                    lifecycle_cleaned = True
                    cleanup_results["workspace_cleaned"] = True
                    logger.info("[TaskTool] 已通过 lifecycle 清理工作空间: %s", task_id)
        except Exception as e:
            logger.debug("[TaskTool] lifecycle 清理不可用，回退到原有逻辑: %s", e)

        if not lifecycle_cleaned and workspace:
            try:
                from isolation.workspace import get_workspace_config_root

                workspace_path = Path(workspace)
                ws_root = get_workspace_config_root()

                if not workspace_path.is_absolute():
                    workspace_path = Path(ws_root) / workspace

                ws_root_resolved = Path(ws_root).resolve()
                ws_path_resolved = workspace_path.resolve()

                if not ws_path_resolved.is_relative_to(ws_root_resolved):
                    logger.warning(
                        "[TaskTool] 拒绝删除工作空间（不在配置根目录下）: %s (root=%s)",
                        ws_path_resolved, ws_root_resolved,
                    )
                    cleanup_results["errors"].append(
                        f"安全拦截：路径 {ws_path_resolved} 不在工作空间根目录 {ws_root_resolved} 下，已跳过删除"
                    )
                elif workspace_path.exists():
                    git_path = workspace_path / ".git"
                    if git_path.is_file():
                        self._remove_worktree(workspace_path, cleanup_results)
                    elif git_path.is_dir():
                        shutil.rmtree(str(workspace_path))
                        cleanup_results["workspace_cleaned"] = True
                        logger.info("[TaskTool] 已清理工作空间: %s", workspace_path)
                    else:
                        shutil.rmtree(str(workspace_path))
                        cleanup_results["workspace_cleaned"] = True
                        logger.info("[TaskTool] 已清理普通目录: %s", workspace_path)
                else:
                    logger.debug("[TaskTool] 工作空间不存在: %s", workspace_path)
            except Exception as e:
                cleanup_results["errors"].append(f"清理工作空间失败: {str(e)}")
                logger.warning("[TaskTool] 清理工作空间失败: %s, 错误: %s", workspace, e)

        return cleanup_results

    def _remove_worktree(
        self,
        workspace_path: Path,
        cleanup_results: dict[str, Any],
    ) -> None:
        """移除 git worktree 并清理对应分支。

        worktree 的 .git 是一个文件（指向主仓库 .git/worktrees/xxx），
        需要通过 git worktree remove 命令正确清理，而非直接 shutil.rmtree。

        Args:
            workspace_path: worktree 的工作空间路径
            cleanup_results: 清理结果字典，用于记录错误信息
        """
        try:
            # 读取 .git 文件内容，定位主仓库路径
            git_file_content = (workspace_path / ".git").read_text(encoding="utf-8").strip()
            # 格式为 "gitdir: /path/to/main-repo/.git/worktrees/xxx"
            if git_file_content.startswith("gitdir: "):
                worktree_gitdir = Path(git_file_content[len("gitdir: "):])
                # 主仓库根目录: .git/worktrees/xxx 的上上级
                main_repo = worktree_gitdir.parent.parent.parent
            else:
                main_repo = workspace_path.parent

            # 在主仓库中执行 git worktree remove --force
            subprocess.run(
                ["git", "worktree", "remove", str(workspace_path), "--force"],
                cwd=str(main_repo),
                capture_output=True,
                text=True,
                check=True,
            )
            logger.info("[TaskTool] 已通过 git worktree remove 清理 worktree: %s", workspace_path)
            cleanup_results["workspace_cleaned"] = True
        except subprocess.CalledProcessError as e:
            cleanup_results["errors"].append(
                f"git worktree remove 失败: {e.stderr.strip() if e.stderr else str(e)}"
            )
            logger.warning(
                "[TaskTool] git worktree remove 失败: %s, stderr: %s",
                workspace_path,
                e.stderr,
            )
        except Exception as e:
            cleanup_results["errors"].append(f"清理 worktree 失败: {str(e)}")
            logger.warning("[TaskTool] 清理 worktree 失败: %s, 错误: %s", workspace_path, e)
