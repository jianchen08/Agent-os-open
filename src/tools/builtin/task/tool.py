"""

任务管理工具（简化版）



暴露接口：

- get_tool_definition() -> Tool：工具定义

- TaskTool：任务管理工具类



6 个操作：get / continue / stop / delete / complete / fail



使用 tasks.service.TaskService（JSON 文件存储）进行任务 CRUD 和状态管理。

"""



import logging
from typing import Any

from core.results import ToolExecutionResult
from tasks.service import TaskService
from tasks.state_machine import InvalidTransitionError
from tasks.types import TaskModel, TaskStatus
from tools.builtin.base import BuiltinTool
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

    """任务管理工具（简化版）。



    6 个操作：

    - get：查询任务（合并旧 get/list/status）

    - continue：继续执行（合并旧 retry/inject/resume）

    - stop：停止任务（合并旧 pause/cancel，统一设 STOPPED）

    - delete：删除任务

    - complete：容器完成（L1，旧 complete_container）

    - fail：容器失败（L1，旧 fail_container）



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

        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

        provider = get_service_provider()

        return provider.get("execution_record_storage")



    @staticmethod

    def _calc_elapsed_seconds(task: TaskModel) -> float | None:

        """计算任务已耗时（秒）。"""

        if not task.started_at:

            return None

        from datetime import datetime  # noqa: PLC0415

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



        Returns:

            TaskService 实例



        Raises:

            RuntimeError: TaskService 创建失败

        """

        if self._task_service is not None:

            return self._task_service

        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

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

                "## 6 个操作\n"

                "- **get**：查询任务。不传 task_id → 返回列表简表；传 task_id → 返回单个任务详情\n"

                "- **continue**：继续执行。可重试失败任务、恢复已停止任务、向运行中任务注入指令\n"

                "- **stop**：停止任务。统一进入 stopped 状态（数据完好，可 continue 恢复）\n"

                "- **delete**：彻底删除任务\n"

                "- **complete**：标记容器任务完成（仅L1）\n"

                "- **fail**：标记容器任务失败（仅L1）\n\n"

                "## continue 的四种行为\n"

                "- 运行中任务 + message：注入指令（不改变状态）\n"

                "- 失败/超时任务：重试（自动继承管道+空间）\n"

                "- 失败/超时任务 + message：重试 + 注入指令\n"

                "- 已停止任务：恢复执行\n\n"

                "## 权限\n"

                "- L1：默认只显示自己提交的任务；传 show_all=true 可递归查看当前会话所有任务\n"

                "- L2：只能管理自己提交的子任务"

            ),

            input_schema={

                "type": "object",

                "properties": {

                    "action": {

                        "type": "string",

                        "enum": ["get", "continue", "stop", "delete", "complete", "fail"],

                        "description": (

                            "操作类型：\n"

                            "- get：查询任务。不传 task_id 返回列表简表，传 task_id 返回详情\n"

                            "- continue：继续执行（重试/恢复/注入指令）\n"

                            "- stop：停止任务（统一进入 stopped 状态）\n"

                            "- delete：删除任务\n"

                            "- complete：标记容器完成（仅L1）\n"

                            "- fail：标记容器失败（仅L1）"

                        ),

                    },

                    "task_scope": {

                        "type": "string",

                        "enum": ["all", "container", "non_container"],

                        "description": "任务范围过滤（get 列表模式时生效）",

                        "default": "all",

                    },

                    "task_id": {

                        "type": "string",

                        "description": "目标任务 ID",

                    },

                    "task_ids": {

                        "type": "array",

                        "items": {"type": "string"},

                        "description": "批量任务 ID 列表（与 task_id 二选一）。适用于 continue/stop/delete 操作",

                    },

                    "status": {

                        "type": "string",

                        "enum": [

                            "pending",

                            "running",

                            "stopped",

                            "completed",

                            "failed",

                            "timeout",

                        ],

                        "description": "按状态筛选（get 列表模式时生效）",

                    },

                    "reason": {

                        "type": "string",

                        "description": "操作原因说明（stop/delete 时推荐填写）",

                    },

                    "message": {

                        "type": "string",

                        "description": (

                            "注入的指令内容（continue 操作时可选）。\n"

                            "该消息会以 user 角色注入到子任务的下一轮对话中。\n"

                            "【内容粒度规则】\n"

                            "1. 常规检查/提醒：只给方向性提示，不给具体执行步骤\n"

                            "2. 纠正性注入（下级理解偏了、方向错了）：给出具体的纠正意见\n"

                            "3. 错误修正（提交参数有误、路径错误）：给出具体修正内容\n"

                            "4. 用户指令传递（用户有新要求或变更）：给出用户的具体要求\n"

                            "禁止任何情况下给出工作流程级别的建议，下级 Agent 比你更清楚怎么执行。"

                        ),

                    },

                    "container_reason": {

                        "type": "string",

                        "description": "容器操作原因（complete/fail 操作时填写）",

                    },

                    "include_details": {

                        "type": "boolean",

                        "description": "是否包含详细信息（get 详情模式生效）。设为 true 时返回 recent_activities 和 elapsed_seconds",

                        "default": False,

                    },

                    "include_agent_calls": {

                        "type": "boolean",

                        "description": "是否只返回工具调用类型的活动记录（get 详情模式生效，自动启用详细信息）",

                        "default": False,

                    },

                    "parent_task_id": {

                        "type": "string",

                        "description": "父任务 ID（get 列表模式时传入可筛选该容器下的子任务）",

                    },

                    "project_id": {

                        "type": "string",

                        "description": "项目 ID，用于筛选特定项目的任务",

                    },

                    "session_id": {

                        "type": "string",

                        "description": "会话 ID，用于筛选特定会话的任务",

                    },

                    "limit": {

                        "type": "integer",

                        "description": "返回数量限制，默认为50，最大100",

                        "default": 50,

                        "maximum": 100,

                    },

                    "show_all": {

                        "type": "boolean",

                        "description": "是否显示当前会话的所有任务（含子任务的子任务）。默认 false，L1 只显示自己提交的任务。仅 L1 生效。",

                        "default": False,

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

                        "continue": 0,

                        "stop": 0,

                        "delete": 0,

                        "complete": 1,

                        "fail": 1,

                    },

                },

            },

        )



    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:  # noqa: PLR0911

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

        if task_ids and isinstance(task_ids, list) and action in ("continue", "stop", "delete"):

            return await self._batch_tasks(inputs, parent_agent_level)



        if action == "get":

            return await self._get_task(inputs, parent_agent_level)

        if action == "continue":

            return await self._continue_task(inputs, parent_agent_level)

        if action == "stop":

            return await self._stop_task(inputs, parent_agent_level)

        if action == "delete":

            return await self._delete_task(inputs, parent_agent_level)

        if action == "complete":

            return await self._complete_task(inputs, parent_agent_level)

        if action == "fail":

            return await self._fail_task(inputs, parent_agent_level)

        return create_failure_result(

            error=f"不支持的操作: {action}",

            error_code="INVALID_ACTION",

        )



    @staticmethod

    def _check_permission(  # noqa: PLR0911

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

                if pipeline_id not in (task.parent_pipeline_id, task.pipeline_run_id):

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



        精简策略：只返回 LLM 做决策需要的关键字段。



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

            # ── 工作空间信息（直接从任务元数据读取，不二次解析） ──

            result["workspace"] = metadata.get("workspace", "")

            ws_meta = metadata.get("ws_meta")

            if isinstance(ws_meta, dict) and ws_meta.get("path"):

                result["resolved_workspace"] = ws_meta["path"]



            eval_summary = None

            if "evaluation_history" in metadata:

                history = metadata.pop("evaluation_history")

                if history:

                    last = history[-1]

                    eval_summary = {

                        "passed": last.get("passed"),

                        "summary": last.get("summary", ""),

                        "attempt_count": len(history),

                        "evidence": last.get("evidence", []),

                        "suggestions": last.get("suggestions", []),

                        "score": last.get("score"),

                        "metrics": last.get("metrics", []),

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



    # ── get：查询任务（合并旧 get/list/status）──



    async def _get_task(

        self, inputs: dict[str, Any], parent_agent_level: int

    ) -> ToolExecutionResult:

        """查询任务。



        - 传 task_id：返回单个任务详情

        - 不传 task_id：返回任务列表简表



        Args:

            inputs: 工具输入参数

            parent_agent_level: 父 Agent 层级



        Returns:

            任务详情、列表或错误结果

        """

        task_id = inputs.get("task_id")



        if task_id:

            return await self._get_task_detail(inputs, parent_agent_level, task_id)

        return await self._get_task_list(inputs, parent_agent_level)



    async def _get_task_detail(

        self, inputs: dict[str, Any], parent_agent_level: int, task_id: str

    ) -> ToolExecutionResult:

        """查询单个任务详情。"""

        try:

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



    async def _get_task_list(  # noqa: PLR0912,PLR0915

        self, inputs: dict[str, Any], parent_agent_level: int

    ) -> ToolExecutionResult:

        """获取任务列表简表。"""

        try:

            status_filter = inputs.get("status")

            inputs.get("session_id")

            pipeline_id = inputs.get("pipeline_id")

            user_parent_task_id = inputs.get("parent_task_id")

            limit = inputs.get("limit", 50)

            show_all = inputs.get("show_all", False)



            tasks = await self._get_all_tasks(limit)



            # 过滤

            filtered = []

            for task in tasks:

                if status_filter and task.status.value != status_filter:

                    continue



                if parent_agent_level == 1:

                    session_id_val = inputs.get("session_id")

                    if session_id_val and task.metadata.get("session_id") != session_id_val:

                        continue

                    if not show_all:

                        submitted_by = (task.metadata or {}).get("submitted_by_level")

                        if submitted_by is not None and submitted_by != 1:

                            continue

                elif parent_agent_level == 2:

                    if pipeline_id:  # noqa: SIM102

                        if pipeline_id not in (task.parent_pipeline_id, task.pipeline_run_id):

                            continue

                    if inputs.get("parent_task_id"):  # noqa: SIM102

                        if task.parent_task_id != inputs["parent_task_id"]:

                            continue



                if user_parent_task_id and task.parent_task_id != user_parent_task_id:

                    continue



                task_scope = inputs.get("task_scope", "all")

                if task_scope != "all":

                    scope = task.metadata.get("task_scope", "non_container")

                    if scope != task_scope:

                        continue



                project_id = inputs.get("project_id")

                if project_id:

                    meta_project = task.metadata.get("project_id")

                    if meta_project != project_id:

                        continue



                filtered.append(task)



            # 构建简表

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

                metadata={"action": "get_task_list"},

            )



        except Exception as e:

            logger.error("[TaskTool] 列出任务失败: %s", e)

            return create_failure_result(

                error=f"列出任务失败: {str(e)}",

                error_code="LIST_FAILED",

            )



    # ── continue：继续执行（合并旧 retry/inject/resume）──



    async def _continue_task(  # noqa: PLR0911

        self, inputs: dict[str, Any], parent_agent_level: int

    ) -> ToolExecutionResult:

        """继续执行任务。



        根据当前状态自动选择行为：

        - running + message：注入指令（不改变状态）

        - stopped：恢复执行（stopped → running）

        - failed/timeout：重试（→ pending，自动继承管道+空间）

        - failed/timeout + message：重试 + 注入指令



        Args:

            inputs: 工具输入参数

            parent_agent_level: 父 Agent 层级



        Returns:

            操作结果

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



            message = inputs.get("message", "")



            # ── 场景 1：运行中任务 → 注入指令 ──

            if task.status == TaskStatus.RUNNING:

                return await self._inject_to_running(task, message, parent_agent_level)



            # ── 场景 2：已停止任务 → 恢复执行 ──

            if task.status == TaskStatus.STOPPED:

                return await self._resume_from_stopped(task, message, service)



            # ── 场景 3：失败/超时任务 → 重试 ──

            if task.status in (TaskStatus.FAILED, TaskStatus.TIMEOUT):

                return await self._retry_from_terminal(task, message, service)



            return create_failure_result(

                error=f"当前状态 {task.status.value} 不支持 continue 操作。"

                f"支持的状态：running（注入指令）、stopped（恢复）、failed/timeout（重试）",

                error_code="INVALID_STATUS",

            )



        except InvalidTransitionError as e:

            return create_failure_result(

                error=f"continue 失败（状态转换不合法）: {e}",

                error_code="INVALID_TRANSITION",

            )

        except Exception as e:

            logger.error("[TaskTool] continue 失败: %s", e)

            return create_failure_result(

                error=f"continue 失败: {str(e)}",

                error_code="CONTINUE_FAILED",

            )



    async def _inject_to_running(

        self, task: TaskModel, message: str, parent_agent_level: int

    ) -> ToolExecutionResult:

        """向运行中的任务注入指令（continue 场景 1）。"""

        if not message:

            return create_failure_result(

                error="运行中的任务 continue 需要提供 message 参数（注入指令内容）",

                error_code="MISSING_MESSAGE",

            )



        target_pipeline_id = task.pipeline_run_id

        if not target_pipeline_id:

            return create_failure_result(

                error="任务尚未启动或 pipeline_run_id 未绑定，无法注入",

                error_code="MISSING_PIPELINE_ID",

            )



        inject_result: dict[str, Any] = {

            "task_id": task.id,

            "injected": True,

            "target_pipeline_id": target_pipeline_id,

            "message_preview": message[:100],

        }



        try:

            from tools.tool_context import MessageType, PipelineMessage, emit  # noqa: PLC0415

            _cont_msg = PipelineMessage(

                type=MessageType.CHAT,

                content=message,

                pipeline_id=target_pipeline_id,

                metadata={

                    "source": "task_continue",

                    "injected_by": f"L{parent_agent_level}",

                    "task_id": task.id,

                },

            )

            result = await emit(

                _cont_msg,

                task_id=task.id,

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

            metadata={"action": "continue_inject"},

        )



    async def _resume_from_stopped(

        self, task: TaskModel, message: str, service: TaskService

    ) -> ToolExecutionResult:

        """从 stopped 状态恢复执行（continue 场景 2）。"""

        old_status = task.status.value



        if message:

            if not task.metadata:

                task.metadata = {}

            task.metadata["retry_message"] = message

            logger.info(

                "[TaskTool] resume 携带注入信息 | task_id=%s | preview=%s",

                task.id, message[:80],

            )



        await service.resume_task(task.id)



        result_data: dict[str, Any] = {

            "task_id": task.id,

            "resumed": True,

            "old_status": old_status,

            "new_status": TaskStatus.RUNNING.value,

        }

        if message:

            result_data["message_injected"] = True



        return create_success_result(

            data=result_data,

            metadata={"action": "continue_resume"},

        )



    async def _retry_from_terminal(  # noqa: PLR0912

        self, task: TaskModel, message: str, service: TaskService

    ) -> ToolExecutionResult:

        """从 failed/timeout 状态重试（continue 场景 3）。"""

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



        old_status = task.status.value



        # 将纠正信息存入 metadata

        if message:

            task.metadata["retry_message"] = message

            logger.info(

                "[TaskTool] retry 携带纠正信息 | task_id=%s | preview=%s",

                task.id, message[:80],

            )



        # 递增 retry_count

        task.metadata["retry_count"] = retry_count + 1



        # 利用状态机从 failed/timeout → pending

        await service.force_transition(task.id, TaskStatus.PENDING)

        task.error = None

        await service.save_task(task)



        # 通过 TaskWorker 重提交

        target_id = task.metadata.get("target_id", "") or task.agent_name or ""

        execution_warning = None

        _ws_meta = task.metadata.get("ws_meta", {})

        _workspace = task.metadata.get("workspace", "") or _ws_meta.get("path", "")



        try:

            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

            task_worker = get_service_provider().get("task_worker")

            if task_worker:



                # 恢复 pipe 继承信息：从源任务获取 pipeline_run_id

                _inherit_pipe_from = task.metadata.get("inherit_pipe_from")

                _inherit_pipe_pipeline_id = ""

                if _inherit_pipe_from:

                    try:

                        source_task = await service.get_task(_inherit_pipe_from)

                        if source_task and hasattr(source_task, "pipeline_run_id"):

                            _inherit_pipe_pipeline_id = source_task.pipeline_run_id or ""

                    except Exception:

                        pass



                task_data = {

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

                }

                if _inherit_pipe_pipeline_id:

                    task_data["_inherit_pipe_pipeline_id"] = _inherit_pipe_pipeline_id



                if not task_worker.submit_task(task_data):

                    execution_warning = "后台执行器未启动，任务已重置为 pending 但不会自动执行"

                else:

                    logger.info("[TaskTool] retry 已提交到 TaskWorker: task_id=%s", task.id)

            else:

                execution_warning = "后台执行器不可用，任务已重置为 pending 但不会自动执行"

        except Exception as submit_exc:

            logger.warning("[TaskTool] retry 提交任务失败: %s", submit_exc)

            execution_warning = f"任务提交失败: {submit_exc}"



        result_data: dict[str, Any] = {

            "task_id": task.id,

            "retried": True,

            "old_status": old_status,

            "new_status": TaskStatus.PENDING.value,

            "retry_count": retry_count + 1,

            "max_retries": max_retries,

        }

        if execution_warning:

            result_data["warning"] = execution_warning



        return create_success_result(

            data=result_data,

            metadata={"action": "continue_retry"},

        )



    # ── stop：停止任务（合并旧 pause/cancel，统一设 STOPPED）──



    async def _stop_task(  # noqa: PLR0911

        self, inputs: dict[str, Any], parent_agent_level: int

    ) -> ToolExecutionResult:

        """停止任务（统一进入 stopped 状态）。



        将 running/pending 等非终态任务设为 STOPPED，同时级联停止子任务。

        stopped 状态数据完好，可通过 continue 恢复。



        Args:

            inputs: 工具输入参数

            parent_agent_level: 父 Agent 层级



        Returns:

            停止结果或错误

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



            # 只有非终态任务可以停止

            stoppable_statuses = {

                TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.STOPPED,

            }

            if task.status not in stoppable_statuses:

                return create_failure_result(

                    error=f"当前状态 {task.status.value} 无法停止。可停止的状态：pending/running/stopped",

                    error_code="INVALID_STATUS",

                )



            if task.status == TaskStatus.STOPPED:

                return create_failure_result(

                    error="任务已是 stopped 状态",

                    error_code="ALREADY_STOPPED",

                )



            reason = inputs.get("reason", "用户请求停止")

            old_status = task.status.value



            # BUG-FIX: pause_task 的参数是 paused_by 而非 reason

            await service.pause_task(task_id, paused_by=f"停止(用户): {reason}")



            # 级联停止子任务（仅对有子任务的任务）

            cascaded = 0

            try:

                service._cancel_pipeline_recursive(task_id)

                cascaded = await service.cancel_task_cascade(task_id, reason=reason)

            except Exception as cascade_err:

                logger.warning("[TaskTool] 级联停止子任务失败 (non-fatal): %s", cascade_err)



            result_data: dict[str, Any] = {

                "task_id": task_id,

                "stopped": True,

                "old_status": old_status,

                "new_status": TaskStatus.STOPPED.value,

                "reason": reason,

            }

            if cascaded > 0:

                result_data["cascaded_subtasks"] = cascaded



            return create_success_result(

                data=result_data,

                metadata={"action": "stop_task"},

            )



        except InvalidTransitionError as e:

            return create_failure_result(

                error=f"停止失败（状态转换不合法）: {e}",

                error_code="INVALID_TRANSITION",

            )

        except Exception as e:

            logger.error("[TaskTool] 停止任务失败: %s", e)

            return create_failure_result(

                error=f"停止任务失败: {str(e)}",

                error_code="STOP_FAILED",

            )



    # ── delete：删除任务 ──



    async def _delete_task(

        self, inputs: dict[str, Any], parent_agent_level: int

    ) -> ToolExecutionResult:

        """删除任务，根据任务类型执行不同策略。



        委托 TaskService 执行实际删除操作：

          - 容器任务: 软删除（标记停止，保留数据）+ 级联清理子任务资源

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



    # ── complete：标记容器完成（L1，旧 complete_container）──



    async def _complete_task(  # noqa: PLR0911

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

        from datetime import datetime  # noqa: PLC0415



        if parent_agent_level != 1:

            return create_failure_result(

                error="容器操作仅限 L1 主 Agent 执行",

                error_code="PERMISSION_DENIED",

            )



        task_id = inputs.get("task_id")

        if not task_id:

            return create_failure_result(

                error="complete 操作必须提供 task_id",

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



        if task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):

            return create_failure_result(

                error=f"容器当前状态为 {task.status.value}，只能操作 PENDING 或 RUNNING 状态的容器",

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

            await service.force_transition(task.id, TaskStatus.COMPLETED)

            task.completed_at = datetime.now().isoformat()

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

                metadata={"action": "complete_task"},

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



    # ── fail：标记容器失败（L1，旧 fail_container）──



    async def _fail_task(  # noqa: PLR0911

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

        from datetime import datetime  # noqa: PLC0415



        if parent_agent_level != 1:

            return create_failure_result(

                error="容器操作仅限 L1 主 Agent 执行",

                error_code="PERMISSION_DENIED",

            )



        task_id = inputs.get("task_id")

        if not task_id:

            return create_failure_result(

                error="fail 操作必须提供 task_id",

                error_code="MISSING_TASK_ID",

            )



        reason = inputs.get("container_reason", inputs.get("reason", ""))

        if not reason:

            return create_failure_result(

                error="fail 操作必须提供 container_reason 说明失败原因",

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



        if task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):

            return create_failure_result(

                error=f"容器当前状态为 {task.status.value}，只能操作 PENDING 或 RUNNING 状态的容器",

                error_code="INVALID_STATUS",

            )



        reason = inputs.get("container_reason", inputs.get("reason", ""))



        try:

            await service.force_transition(task.id, TaskStatus.FAILED)

            task.completed_at = datetime.now().isoformat()

            task.metadata = task.metadata or {}

            task.metadata["container_reason"] = reason

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

                metadata={"action": "fail_task"},

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



    # ── 批量操作 ──



    async def _batch_tasks(

        self, inputs: dict[str, Any], parent_agent_level: int

    ) -> ToolExecutionResult:

        """批量任务操作，每个任务独立返回结果。"""

        action = inputs.get("action")

        task_ids = inputs.get("task_ids", [])

        results = []



        for task_id in task_ids:

            file_inputs = dict(inputs)

            file_inputs["task_id"] = task_id

            file_inputs.pop("task_ids", None)



            if action == "continue":

                result = await self._continue_task(file_inputs, parent_agent_level)

            elif action == "stop":

                result = await self._stop_task(file_inputs, parent_agent_level)

            elif action == "delete":

                result = await self._delete_task(file_inputs, parent_agent_level)

            else:

                result = create_failure_result(

                    error=f"不支持的批量操作: {action}",

                    error_code="INVALID_ACTION",

                )



            results.append({

                "task_id": task_id,

                "success": result.success,

                "data": result.output if result.success else None,

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

