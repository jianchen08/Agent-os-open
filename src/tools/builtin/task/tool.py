"""
任务管理工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- TaskTool：任务管理工具类

使用 tasks.service.TaskService（JSON 文件存储）进行任务 CRUD 和状态管理。
"""

import logging
from typing import Any

from core.results import ToolExecutionResult
from tools.builtin.base import BuiltinTool
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


class TaskTool(BuiltinTool):
    """任务管理工具。

    提供：
    - 查询任务（get、list、status）
    - 状态更新（update）
    - 任务控制（pause、resume、cancel、retry）
    - 删除任务（delete）
    - 注入指令（inject）

    权限规则：
    - L1：默认只显示自己提交的任务；传 show_all=true 可递归查看当前会话所有任务（含子任务的子任务）
    - L2：只能管理自己提交的子任务
    """

    def __init__(self) -> None:
        """初始化任务管理工具。"""
        self._task_service: TaskService | None = None

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
        records = storage.list_by_pipeline(task.pipeline_run_id)[0]
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
        records = storage.list_by_pipeline(task.pipeline_run_id)[0]
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
        service = provider.get_or_create(
            "task_service",
            lambda: TaskService(event_bus=provider.get("event_bus")),
        )
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
                "- pause/resume/cancel/retry/resume_completed：任务生命周期控制\n"
                "- inject：向运行中或暂停的子任务注入指令\n"
                "- delete：删除任务\n"
                "- complete_container/fail_container：标记容器完成/失败（仅L1）\n\n"
                "## 权限\n"
                "- L1：默认只显示自己提交的任务；传 show_all=true 可递归查看当前会话所有任务（含子任务的子任务）\n"
                "- L2：只能管理自己提交的子任务"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["get", "update", "list", "status", "pause", "resume", "cancel", "retry", "resume_completed", "delete", "inject", "complete_container", "fail_container"],
                        "description": (
                            "操作类型，根据场景选择：\n"
                            "- get：查询单个任务详情，需要 task_id；加 include_details=true 可展开最近执行活动\n"
                            "- list：列出任务列表；L1 默认只显示自己提交的任务，传 show_all=true 可递归查看当前会话所有层级任务；传 parent_task_id 可查看某容器下所有子任务的执行情况\n"
                            "- status：查看全局状态概览（各状态数量统计 + 最近任务摘要），适合快速了解整体进度\n"
                            "- update：更新任务状态字段\n"
                            "- pause：暂停运行中的任务\n"
                            "- resume：恢复暂停的任务\n"
                            "- cancel：取消任务\n"
                            "- retry：重试失败的任务（重置为pending重新执行）\n"
                            "- resume_completed：恢复已完成的任务继续执行（completed→pending，携带对话历史）\n"
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
                        "description": "任务ID，get/update/pause/resume/cancel/retry/delete/inject操作时必填（与 task_ids 二选一）",
                    },
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "批量任务ID列表（与 task_id 二选一，优先使用 task_ids）。适用于 pause/resume/cancel/retry/delete 操作",
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
                        "description": (
                            "注入的指令内容（inject操作时必填，retry操作时可选）。"
                            "该消息会以user角色注入到子任务的下一轮对话中，子任务Agent会看到并据此调整执行方向。\n"
                            "【内容粒度规则】根据触发性质区分注入内容的详细程度：\n"
                            "1. 常规检查/提醒（定时触发、进度检查、重试）：只给方向性提示，不给具体执行步骤。"
                            "正确：'检查子任务是否偏离，如有停滞继续推进'。"
                            "错误：'先做A再做B然后测试'——这是替下级安排工作流程，下级有自己的执行逻辑。\n"
                            "2. 纠正性注入（下级理解偏了、方向错了）：给出具体的纠正意见。"
                            "示例：'需求有变更，登录模块改用JWT方案而非Session方案'。\n"
                            "3. 错误修正（提交参数有误、路径错误）：给出具体修正内容。"
                            "示例：'workspace路径应为/xxx，请用正确路径重试'。\n"
                            "4. 用户指令传递（用户有新要求或变更）：给出用户的具体要求。"
                            "示例：'用户要求增加邮件通知功能'。\n"
                            "禁止任何情况下给出工作流程级别的建议（如'先写代码再测试'），下级Agent比你更清楚怎么执行。"
                        ),
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
                    "show_all": {
                        "type": "boolean",
                        "description": "是否显示当前会话的所有任务（含子任务的子任务）。默认 false，L1 只显示自己提交的任务。设为 true 时递归展示当前会话中所有层级的任务。仅 L1 生效。",
                        "default": False,
                    },
                    "goal": {
                        "type": "object",
                        "description": "更新后的任务目标（resume_completed操作时可选）。写入任务的metadata.goal，覆盖原有目标",
                    },
                    "acceptance_criteria": {
                        "type": "object",
                        "description": "更新后的验收标准（resume_completed操作时可选）。写入任务的metadata.acceptance_criteria，覆盖原有验收标准",
                    },
                },
                "required": ["action"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.SYSTEM,
            tags=["task", "management", "L1", "L2", "status", "control"],
            injected_params=["session_id", "user_id", "_session", "pipeline_id"],
            param_level_restrictions={
                "action": {
                    "enum_restrictions": {
                        "get": 0,
                        "update": 0,
                        "list": 0,
                        "status": 0,
                        "pause": 0,
                        "resume": 0,
                        "cancel": 0,
                        "retry": 0,
                        "resume_completed": 0,
                        "delete": 0,
                        "inject": 0,
                        "complete_container": 1,
                        "fail_container": 1,
                    },
                },
            },
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行任务管理操作。

        Args:
            inputs: 工具输入参数，必须包含 action 字段

        Returns:
            工具执行结果
        """
        action = inputs.get("action")
        parent_agent_level = inputs.get("parent_agent_level")

        if parent_agent_level is None:
            logger.error("[TaskTool] 注入参数缺失 | parent_agent_level 未注入")
            return create_failure_result(
                error="系统错误：parent_agent_level 未注入，无法确定调用者层级",
                error_code="MISSING_INJECTED_PARAM",
            )

        try:
            self._get_task_service()
        except RuntimeError as e:
            return create_failure_result(
                error=str(e),
                error_code="SERVICE_UNAVAILABLE",
            )

        # 检查是否使用批量参数
        task_ids = inputs.get("task_ids")
        if task_ids and isinstance(task_ids, list) and action in ("pause", "resume", "cancel", "retry", "resume_completed", "delete", "inject"):
            return await self._batch_tasks(inputs, parent_agent_level)

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
        elif action == "resume_completed":
            return await self._resume_completed_task(inputs, parent_agent_level)
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

        L1 主 Agent 可查看和管理当前会话的所有任务（含子任务的子任务），
        仅按 session_id 隔离，不检查 submitted_by_level。
        L2 只能管理自己提交的子任务（通过 parent_task_id 校验）。
        L3 禁止使用 task_manage 工具。

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

        if parent_agent_level == 2:
            submitted_by = (task.metadata or {}).get("submitted_by_level")
            if submitted_by is not None:
                if submitted_by != parent_agent_level:
                    return False, (
                        f"权限不足：本任务由 L{submitted_by} Agent 提交，"
                        f"当前 L{parent_agent_level} Agent 无法管理"
                    )
                return True, None

            pipeline_id = inputs.get("pipeline_id")
            if pipeline_id:
                if task.parent_pipeline_id != pipeline_id and task.pipeline_run_id != pipeline_id:
                    return False, (
                        f"任务不属于当前管道：task.parent_pipeline_id={task.parent_pipeline_id}，"
                        f"当前 pipeline_id={pipeline_id}"
                    )
                return True, None

            parent_task_id = inputs.get("parent_task_id")
            if parent_task_id:
                if task.parent_task_id == parent_task_id:
                    return True, None
                return False, (
                    f"L2 只能管理自己提交的子任务：task.parent_task_id={task.parent_task_id}，"
                    f"当前 parent_task_id={parent_task_id}"
                )
            return False, "L2 缺少 parent_task_id 参数，无法验证权限"

        return False, f"只有 L1 和 L2 Agent 能使用 task_manage 工具，当前层级：L{parent_agent_level}"

    # BUG-FIX-fix_20260512_async_list_all: 改为 async def，添加 await
    async def _get_all_tasks(self, limit: int = 5) -> list[TaskModel]:
        """获取全部任务列表。

        Args:
            limit: 返回数量限制

        Returns:
            任务模型列表（按创建时间倒序）
        """
        service = self._get_task_service()
        return await service.list_all(limit=limit)

    def _task_to_dict(
        self, task: TaskModel, include_details: bool = False, include_agent_calls: bool = False
    ) -> dict[str, Any]:
        """将 TaskModel 转换为工具返回的字典格式。

        BUG-FIX-fix_20260417_task_manage_records: 新增 include_agent_calls 参数，
        之前该参数在 schema 中定义但从未被使用。现在传 include_agent_calls=true 时
        自动启用 include_details，并筛选只返回工具调用类型的活动记录。

        精简策略：只返回 LLM 做决策需要的关键字段，移除大量冗余字段。

        Args:
            task: 任务模型
            include_details: 是否包含详细活动信息
            include_agent_calls: 是否只返回工具调用类型的活动记录

        Returns:
            可序列化的任务字典
        """
        result = {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "error": task.error,
        }

        if task.metadata:
            metadata = dict(task.metadata)

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
                result["evaluation_summary"] = eval_summary

            fail_reason = metadata.get("fail_reason") or metadata.get("container_reason")
            if fail_reason:
                result["fail_reason"] = fail_reason

            retry_count = metadata.get("retry_count")
            max_retries = metadata.get("max_retries")
            if retry_count is not None:
                result["retry_count"] = retry_count
            if max_retries is not None:
                result["max_retries"] = max_retries

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
            pipeline_id = inputs.get("pipeline_id")
            limit = inputs.get("limit", 5)
            show_all = inputs.get("show_all", False)

            # BUG-FIX-fix_20260512_async_list_all: 添加 await
            tasks = await self._get_all_tasks(limit)

            # 按权限和条件过滤
            filtered_tasks = []
            for task in tasks:
                if parent_agent_level == 1:
                    session_id = inputs.get("session_id")
                    if session_id and task.metadata.get("session_id") != session_id:
                        continue
                    if not show_all:
                        submitted_by = (task.metadata or {}).get("submitted_by_level")
                        if submitted_by is not None and submitted_by != 1:
                            continue
                elif parent_agent_level == 2:
                    pipeline_id = inputs.get("pipeline_id")
                    if pipeline_id:
                        if task.parent_pipeline_id != pipeline_id and task.pipeline_run_id != pipeline_id:
                            continue
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

    async def _batch_tasks(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """批量任务操作，每个任务独立返回结果"""
        action = inputs.get("action")
        task_ids = inputs.get("task_ids", [])
        results = []

        for task_id in task_ids:
            file_inputs = dict(inputs)
            file_inputs["task_id"] = task_id
            file_inputs.pop("task_ids", None)

            if action == "pause":
                result = await self._pause_task(file_inputs, parent_agent_level)
            elif action == "resume":
                result = await self._resume_task(file_inputs, parent_agent_level)
            elif action == "cancel":
                result = await self._cancel_task(file_inputs, parent_agent_level)
            elif action == "retry":
                result = await self._retry_task(file_inputs, parent_agent_level)
            elif action == "resume_completed":
                result = await self._resume_completed_task(file_inputs, parent_agent_level)
            elif action == "delete":
                result = await self._delete_task(file_inputs, parent_agent_level)
            elif action == "inject":
                result = await self._inject_task(file_inputs, parent_agent_level)
            else:
                result = create_failure_result(
                    error=f"不支持的批量操作: {action}",
                    error_code="INVALID_ACTION",
                )

            results.append({
                "task_id": task_id,
                "success": result.success,
                "data": result.data if result.success else None,
                "error": result.error if not result.success else None,
            })

        success_count = sum(1 for r in results if r["success"])
        failed_count = len(results) - success_count

        return create_success_result(
            data={
                "results": results,
                "summary": {
                    "total": len(results),
                    "success": success_count,
                    "failed": failed_count,
                },
            },
            metadata={"action": f"batch_{action}"},
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
                # BUG-FIX-fix_20260512_async_compat: force_transition 现在是 async
                await service.force_transition(task_id, target_status)

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
            pipeline_id = inputs.get("pipeline_id")
            user_parent_task_id = inputs.get("parent_task_id")
            limit = inputs.get("limit", 5)
            show_all = inputs.get("show_all", False)

            # 默认只展示自己的子任务：未显式传 parent_task_id 时，
            # 用注入的 task_id（当前任务ID）作为 parent_task_id
            injected_task_id = inputs.get("task_id")
            if not user_parent_task_id:
                if parent_agent_level == 2 and inputs.get("parent_task_id"):
                    user_parent_task_id = inputs["parent_task_id"]
                elif injected_task_id:
                    user_parent_task_id = injected_task_id

            # BUG-FIX-fix_20260512_async_list_all: 添加 await
            tasks = await self._get_all_tasks(limit)

            # 过滤
            filtered = []
            for task in tasks:
                if status_filter and task.status.value != status_filter:
                    continue

                if parent_agent_level == 1:
                    session_id = inputs.get("session_id")
                    if session_id and task.metadata.get("session_id") != session_id:
                        continue
                    if not show_all:
                        submitted_by = (task.metadata or {}).get("submitted_by_level")
                        if submitted_by is not None and submitted_by != 1:
                            continue
                elif parent_agent_level == 2:
                    if pipeline_id:
                        if task.parent_pipeline_id != pipeline_id and task.pipeline_run_id != pipeline_id:
                            continue
                    if inputs.get("parent_task_id"):
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
                    "d": [
                        [task_ids[i], titles[i], statuses[i], priorities[i], target_names[i], latest_actions[i], elapsed_list[i]]
                        for i in range(len(task_ids))
                    ],
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
            # BUG-FIX-fix_20260512_async_compat: pause_task 现在是 async
            await service.pause_task(task_id)

            return create_success_result(
                data={
                    "task_id": task_id,
                    "paused": True,
                    "old_status": TaskStatus.RUNNING.value,
                    "new_status": TaskStatus.SUSPENDED.value,
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

            if task.status != TaskStatus.SUSPENDED:
                return create_failure_result(
                    error=f"只有暂停状态的任务才能恢复，当前状态: {task.status.value}",
                    error_code="INVALID_STATUS",
                )

            reason = inputs.get("reason", "用户请求继续执行")
            await service.resume_task(task_id)

            return create_success_result(
                data={
                    "task_id": task_id,
                    "resumed": True,
                    "old_status": TaskStatus.SUSPENDED.value,
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

        将任务状态设为 CANCELLED 并记录取消原因，同时级联取消所有子任务。

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
                TaskStatus.SUSPENDED, TaskStatus.EVALUATING,
            }
            if task.status not in cancellable_statuses:
                return create_failure_result(
                    error=f"当前状态无法取消: {task.status.value}",
                    error_code="INVALID_STATUS",
                )

            reason = inputs.get("reason", "用户请求取消")
            old_status = task.status.value

            # BUG-FIX-fix_20260524_cancel_task_status:
            # 问题根因: 使用 fail_task 将状态设为 FAILED，无法区分"取消"和"失败"。
            # 修复方案: 改用 cancel_task 方法，将状态设为 CANCELLED。
            # 影响范围: Tool 层取消任务的状态。
            # 修复日期: 2026-05-24
            await service.cancel_task(task_id, reason=f"已取消: {reason}")

            # BUG-FIX-fix_20260531_cancel_pipeline_recursive:
            # 问题根因: _cancel_pipeline_recursive 是 TaskService 的方法，
            #           但代码中用 self（TaskTool实例）调用，导致 AttributeError，
            #           使 running 状态的任务无法被取消。
            # 修复方案: 改为通过 service 实例调用该方法。
            # 修复日期: 2026-05-31
            service._cancel_pipeline_recursive(task_id)
            cascaded = await service.cancel_task_cascade(task_id, reason=reason)

            result_data = {
                "task_id": task_id,
                "cancelled": True,
                "old_status": old_status,
                "new_status": TaskStatus.CANCELLED.value,
                "reason": reason,
            }
            if cascaded > 0:
                result_data["cascaded_subtasks"] = cascaded

            return create_success_result(
                data=result_data,
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

            # BUG-FIX-fix_20260523_retry_paused:
            # 支持 FAILED 和 SUSPENDED 两种状态的重试。
            # SUSPENDED 来自 TaskWorker.stop() 暂停的任务（系统重启后恢复场景）。
            if task.status not in (TaskStatus.FAILED, TaskStatus.SUSPENDED):
                return create_failure_result(
                    error=f"只有失败或暂停的任务才能重试，当前状态: {task.status.value}",
                    error_code="INVALID_STATUS",
                )

            # BUG-FIX-fix_20260519_pipeline_retry:
            # 问题根因: _retry_task 没有检查 max_retries，AI 可通过 task_manage retry
            #   无限次重试同一任务，每次都成功 failed->pending，max_retries 限制形同虚设。
            # 修复方案: 在重试前从 metadata 读取 retry_count/max_retries，
            #   超限时拒绝重试并返回失败。
            if not task.metadata:
                task.metadata = {}
            retry_count = task.metadata.get("retry_count", 0)
            max_retries = task.metadata.get("max_retries", 6)
            if retry_count >= max_retries:
                return create_failure_result(
                    error=(
                        f"任务已达到最大重试次数 ({retry_count}/{max_retries})，"
                        f"无法继续重试。请考虑其他方案或标记任务失败。"
                    ),
                    error_code="MAX_RETRIES_EXCEEDED",
                )

            reason = inputs.get("reason", "用户请求重试")
            message = inputs.get("message", "")
            old_status = task.status.value

            # 将纠正信息存入 metadata，供 _execute_background_task 读取拼入 full_input
            if message:
                task.metadata["retry_message"] = message
                logger.info(
                    "[TaskTool] retry 携带纠正信息 | task_id=%s | preview=%s",
                    task_id, message[:80],
                )

            # BUG-FIX-fix_20260519_pipeline_retry: 递增 retry_count 并写回 metadata
            task.metadata["retry_count"] = retry_count + 1

            # 利用状态机从 failed/paused -> pending
            # BUG-FIX-fix_20260512_async_compat: force_transition 现在是 async
            await service.force_transition(task.id, TaskStatus.PENDING)
            task.error = None
            # BUG-FIX-fix_20260512_async_compat: save_task 现在是 async
            await service.save_task(task)

            # BUG-FIX-fix_20260531_retry_pending_stuck:
            # 问题根因: _retry_task 中 target_id 为空时跳过 submit_task 调用，
            #   任务被设为 PENDING 但永远不会提交给 TaskWorker 执行，导致状态卡住。
            # 修复方案: target_id 优先从 metadata 获取，回退到 task.agent_name；
            #   移除 if target_id 守卫，始终提交任务。TaskWorker 未启动时
            #   任务留在 PENDING，启动后 _recover_running_tasks 会自动恢复。
            # 影响范围: 所有通过 task_manage retry 重试的任务。
            # 修复日期: 2026-05-31
            
            # BUG-FIX-fix_20260601_retry_via_taskworker:
            # 问题根因: _try_inject_message_for_retry 绕过 TaskWorker 直接
            #   注入管道，导致 start_task 不被调用，任务状态卡在 PENDING。
            # 修复方案: 始终走 submit_task → TaskWorker → _execute_background_task
            #   → start_task → engine.run。TaskWorker 内部已有 conversation_history
            #   恢复逻辑，无需单独的管道注入路径。
            # BUG-FIX-fix_20260601_retry_prepared_context:
            # retry 时 workspace、配置、输入都已就绪，通过 _prepared_context 跳过
            # _execute_background_task 中的重复准备工作（lifecycle/input_build）。
            target_id = task.metadata.get("target_id", "") or task.agent_name or ""
            execution_warning = None
            _ws_meta = task.metadata.get("ws_meta", {})
            _workspace = task.metadata.get("workspace", "") or _ws_meta.get("path", "")

            try:
                from infrastructure.service_provider import get_service_provider
                task_worker = get_service_provider().get("task_worker")
                if task_worker:
                    if not task_worker.submit_task({
                        "task_id": task.id,
                        "pipeline_id": task.parent_pipeline_id or "",
                        "pipeline_run_id": task.pipeline_run_id or "",
                        "target_type": task.target_type or "agent",
                        "target_id": target_id,
                        "user_input": task.title,
                        "description": task.description,
                        "acceptance_criteria": task.metadata.get("acceptance_criteria", {}),
                        "workspace": _workspace,
                        "isolation_level": task.metadata.get("isolation_level", ""),
                        # retry 预构建上下文：跳过 workspace 初始化 + input 构建
                        "_prepared_context": {
                            "workspace": _workspace,
                            "ws_meta": _ws_meta,
                            "full_input": task.title,  # retry 时 description 在 history 里
                            "isolation_mode": task.metadata.get("isolation_level", ""),
                            "has_explicit_workspace": True,
                            "agent_config_validated": True,
                        },
                    }):
                        execution_warning = "后台执行器未启动，任务已重置为pending但不会自动执行"
                    else:
                        logger.info("[TaskTool] retry 已提交到 TaskWorker: task_id=%s", task_id)
                else:
                    execution_warning = "后台执行器不可用，任务已重置为pending但不会自动执行"
            except Exception as submit_exc:
                logger.warning("[TaskTool] retry 提交任务失败: %s", submit_exc)
                execution_warning = f"任务提交失败: {submit_exc}"

            result_data = {
                "task_id": task_id,
                "retried": True,
                "old_status": old_status,
                "new_status": TaskStatus.PENDING.value,
                "reason": reason,
                "retry_count": retry_count + 1,
                "max_retries": max_retries,
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

    async def _resume_completed_task(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """恢复已完成的任务继续执行（completed → pending）。

        携带原对话历史重新提交任务到 TaskWorker，使管道从已有上下文继续执行。
        可选更新 goal 和 acceptance_criteria。

        Args:
            inputs: 工具输入参数，需包含 task_id
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

            if task.status != TaskStatus.COMPLETED:
                return create_failure_result(
                    error=f"只有已完成的任务才能恢复，当前状态: {task.status.value}",
                    error_code="INVALID_STATUS",
                )

            if not task.metadata:
                task.metadata = {}

            resume_count = task.metadata.get("resume_count", 0)
            max_resumes = task.metadata.get("max_resumes", 3)
            if resume_count >= max_resumes:
                return create_failure_result(
                    error=(
                        f"任务已达到最大恢复次数 ({resume_count}/{max_resumes})，"
                        f"无法继续恢复。"
                    ),
                    error_code="MAX_RESUMES_EXCEEDED",
                )

            old_status = task.status.value

            # 可选：更新 goal
            goal = inputs.get("goal")
            if goal:
                task.metadata["goal"] = goal

            # 可选：更新 acceptance_criteria
            acceptance_criteria = inputs.get("acceptance_criteria")
            if acceptance_criteria:
                task.metadata["acceptance_criteria"] = acceptance_criteria

            # 构造 resume_message
            resume_message = inputs.get("message", "")

            # 递增 resume_count
            task.metadata["resume_count"] = resume_count + 1

            # 加载 conversation_history
            conversation_history = []
            has_history = False
            history_warning = None

            if task.pipeline_run_id:
                try:
                    from pipeline.message_bus import _load_history_from_storage
                    from infrastructure.service_provider import get_service_provider
                    provider = get_service_provider()
                    conversation_history = _load_history_from_storage(
                        task.pipeline_run_id, provider
                    ) or []
                    has_history = bool(conversation_history)
                    if not has_history:
                        history_warning = "未找到对话历史记录，任务将从零开始执行"
                except Exception as hist_err:
                    logger.warning(
                        "[TaskTool] resume_completed 加载对话历史失败: %s", hist_err
                    )
                    history_warning = f"加载对话历史失败: {hist_err}"
            else:
                history_warning = "任务无 pipeline_run_id，无法恢复对话历史"

            # 将 resume_message 存入 metadata，供 _execute_background_task 读取
            if resume_message:
                task.metadata["resume_message"] = resume_message
                logger.info(
                    "[TaskTool] resume_completed 携带恢复信息 | task_id=%s | preview=%s",
                    task_id, resume_message[:80],
                )

            # force_transition: completed → pending
            await service.force_transition(task.id, TaskStatus.PENDING)
            task.error = None
            await service.save_task(task)

            # 通过 TaskWorker 重提交，携带 _prepared_context + conversation_history
            target_id = task.metadata.get("target_id", "") or task.agent_name or ""
            execution_warning = None
            _ws_meta = task.metadata.get("ws_meta", {})
            _workspace = task.metadata.get("workspace", "") or _ws_meta.get("path", "")

            try:
                from infrastructure.service_provider import get_service_provider
                task_worker = get_service_provider().get("task_worker")
                if task_worker:
                    if not task_worker.submit_task({
                        "task_id": task.id,
                        "pipeline_id": task.parent_pipeline_id or "",
                        "pipeline_run_id": task.pipeline_run_id or "",
                        "target_type": task.target_type or "agent",
                        "target_id": target_id,
                        "user_input": task.title,
                        "description": task.description,
                        "acceptance_criteria": task.metadata.get("acceptance_criteria", {}),
                        "workspace": _workspace,
                        "isolation_level": task.metadata.get("isolation_level", ""),
                        "_prepared_context": {
                            "workspace": _workspace,
                            "ws_meta": _ws_meta,
                            "full_input": task.title,
                            "isolation_mode": task.metadata.get("isolation_level", ""),
                            "has_explicit_workspace": True,
                            "agent_config_validated": True,
                        },
                        "conversation_history": conversation_history,
                        "resume_message": resume_message,
                    }):
                        execution_warning = "后台执行器未启动，任务已重置为pending但不会自动执行"
                    else:
                        logger.info(
                            "[TaskTool] resume_completed 已提交到 TaskWorker: task_id=%s",
                            task_id,
                        )
                else:
                    execution_warning = "后台执行器不可用，任务已重置为pending但不会自动执行"
            except Exception as submit_exc:
                logger.warning("[TaskTool] resume_completed 提交任务失败: %s", submit_exc)
                execution_warning = f"任务提交失败: {submit_exc}"

            # 合并 warnings
            warnings = []
            if history_warning:
                warnings.append(history_warning)
            if execution_warning:
                warnings.append(execution_warning)

            result_data = {
                "task_id": task_id,
                "resumed": True,
                "old_status": old_status,
                "new_status": TaskStatus.PENDING.value,
                "resume_count": resume_count + 1,
                "max_resumes": max_resumes,
                "has_conversation_history": has_history,
            }
            if warnings:
                result_data["warning"] = "; ".join(warnings)

            return create_success_result(
                data=result_data,
                metadata={"action": "resume_completed_task"},
            )

        except InvalidTransitionError as e:
            return create_failure_result(
                error=f"恢复失败（状态转换不合法）: {e}",
                error_code="INVALID_TRANSITION",
            )
        except Exception as e:
            logger.error("[TaskTool] 恢复已完成任务失败: %s", e)
            return create_failure_result(
                error=f"恢复已完成任务失败: {str(e)}",
                error_code="RESUME_FAILED",
            )

    async def _inject_task(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """向运行中的子任务注入指令。

        通过统一的 send_pipeline_message() 入口将消息投递到目标管道，
        由 pipeline 内部根据当前状态（运行中/挂起/未启动）自动选择
        最优投递路径（双通道注入 / inject_and_wake）。

        Args:
            inputs: 工具输入参数，需包含 task_id 和 message
            parent_agent_level: 父 Agent 层级，用于权限校验和注入来源标记

        Returns:
            ToolExecutionResult: 注入结果，包含 trigger 方式和消息预览；
                失败时返回错误码及原因
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

            if task.status not in [TaskStatus.RUNNING, TaskStatus.SUSPENDED]:
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

            # ── 统一消息注入 ──
            # 通过 send_pipeline_message() 统一入口投递消息，
            # 内部自动根据管道状态选择最优投递路径。
            inject_result = {
                "task_id": task_id,
                "injected": True,
                "target_pipeline_id": target_pipeline_id,
                "message_preview": message[:100],
            }

            try:
                from pipeline.message_bus import send_pipeline_message
                result = await send_pipeline_message(
                    target_pipeline_id, message,
                    task_id=task_id,
                    metadata={
                        "source": "task_inject",
                        "injected_by": f"L{parent_agent_level}",
                        "task_id": task_id,
                    },
                )
                inject_result["trigger"] = result.method
                if not result.success:
                    inject_result["trigger"] = "failed"
                    inject_result["error"] = result.error
                logger.info(
                    "[TaskTool] 消息注入完成 | pipeline_id=%s | method=%s | preview=%s",
                    target_pipeline_id, result.method, message[:80],
                )
            except Exception as _wake_err:
                logger.warning("[TaskTool] 消息注入失败: %s", _wake_err)

            return create_success_result(
                data=inject_result,
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
        """删除任务，根据任务类型执行不同策略。

        委托 TaskService 执行实际删除操作：
          - 容器任务: 软删除（标记取消，保留数据）+ 级联清理子任务资源
          - 非容器任务: 硬删除（级联清理 + 删除记录）

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

            if task.metadata.get("task_scope") == "container":
                result_data = await service.soft_delete_container(task_id, reason=reason)
                return create_success_result(
                    data=result_data,
                    metadata={"action": "soft_delete_container"},
                )
            else:
                result_data = await service.hard_delete_task(task_id, reason=reason)
                return create_success_result(
                    data=result_data,
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

        # 清理子任务的 worktree（在状态转换之前执行）
        cleanup_info: dict[str, Any] = {}
        try:
            cleanup_info = await service._cleanup_subtask_worktrees(task, subtasks)
        except Exception as e:
            logger.warning(
                "[TaskTool] 容器 %s 子任务 worktree 清理异常 (non-fatal): %s",
                task_id, e,
            )
            cleanup_info = {
                "total_subtasks": len(subtasks),
                "cleaned_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [str(e)],
            }

        try:
            # BUG-FIX-fix_20260512_async_compat: force_transition 现在是 async
            await service.force_transition(task.id, TaskStatus.COMPLETED)
            task.completed_at = datetime.now().isoformat()
            # BUG-FIX-fix_20260512_async_compat: save_task 现在是 async
            await service.save_task(task)
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
                    "cleanup": cleanup_info,
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
            # BUG-FIX-fix_20260512_async_compat: force_transition 现在是 async
            await service.force_transition(task.id, TaskStatus.FAILED)
            task.completed_at = datetime.now().isoformat()
            task.metadata = task.metadata or {}
            task.metadata["container_reason"] = reason
            # BUG-FIX-fix_20260512_async_compat: save_task 现在是 async
            await service.save_task(task)
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

