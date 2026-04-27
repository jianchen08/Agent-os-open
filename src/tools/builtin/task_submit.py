"""
任务提交工具

暴露接口：
- get_tool_definition() -> Tool：工具定义
- TaskSubmitTool：任务提交工具类
"""

import logging
from typing import Any

from core.results import ToolExecutionResult
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


class TaskSubmitTool(BuiltinTool):
    """任务提交工具。

    负责创建任务并通过 EventBus 发布 task.submitted 事件，
    由 TaskWorker 订阅事件并触发后台执行。

    依赖：
    - TaskService：任务创建与存储（JSON 文件存储）
    - EventBus：事件发布（task.submitted）
    """

    def __init__(self) -> None:
        """初始化任务提交工具"""
        self._task_service: Any = None
        self._event_bus: Any = None

    def _get_task_service(self) -> Any:
        """获取共享的 TaskService 实例。

        通过 ServiceProvider 统一获取，支持显式注册、sys 全局变量和懒加载创建。

        Returns:
            TaskService 实例，创建失败时返回 None
        """
        if self._task_service is not None:
            return self._task_service
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
        service = provider.get_or_create("task_service", lambda: __import__("tasks.service", fromlist=["TaskService"]).TaskService())
        if service is not None:
            self._task_service = service
        return self._task_service

    def _get_event_bus(self) -> Any:
        """获取共享的 EventBus 实例。

        通过 ServiceProvider 统一获取，支持显式注册、sys 全局变量和懒加载创建。

        Returns:
            EventBus 实例，获取失败时返回 None
        """
        if self._event_bus is not None:
            return self._event_bus
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
        bus = provider.get_or_create("event_bus", lambda: __import__("pipeline.event_bus", fromlist=["EventBus"]).EventBus())
        if bus is not None:
            self._event_bus = bus
        return self._event_bus

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义（标准 OpenAI Function Calling 格式）"""
        return Tool(
            name="task_submit",
            description="""
任务提交工具。将任务提交给指定的 Agent 执行，配置验收标准确保结果可验证。

【示例】
{"target_type": "agent", "target_id": "general_agent", "goal": {"title": "实现用户登录"}, "acceptance_criteria": {"file_check": {"input_params": {"path": "src/auth/login.py"}}}}
""".strip(),
            input_schema={
                "type": "object",
                "properties": {
                    "target_type": {
                        "type": "string",
                        "enum": ["agent"],
                        "description": "目标类型，固定为 agent。non_container 必填，container 不需要",
                    },
                    "target_id": {
                        "type": "string",
                        "description": "目标 Agent ID。non_container 必填，container 不需要。可通过 resource_search 查找",
                    },
                    "goal": {
                        "type": "object",
                        "description": "任务目标（必填）",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "任务标题（必填），简短明确",
                            },
                            "description": {
                                "type": "string",
                                "description": "详细描述（可选），补充具体要求和预期结果",
                            },
                            "context": {
                                "type": "object",
                                "description": "上下文数据（可选），传递给执行 Agent 的额外数据",
                            },
                        },
                        "required": ["title"],
                    },
                    "description": {
                        "type": "string",
                        "description": "任务描述（可选，用于日志/审计）",
                    },
                    "acceptance_criteria": {
                        "type": "object",
                        "description": """
验收标准字典。non_container 必填，container 不需要。
key 为评估指标 ID，value 为配置对象 {"input_params": {...}}。

【获取推荐指标】
用 resource_search(resource_type="agent", query="agent名称") 搜索 Agent，返回的 description 中包含推荐评估指标（格式：推荐评估：metric_id(参数键值对)）。将 metric_id 作为 key、default_params 作为 input_params 基础。

【按产出物选择指标】
- 文件产出物 → file_check(存在) + format_valid(格式)
- 测试产出物 → test_check(通过率) + bash_check(覆盖率)
- API产出物 → api_check(响应) + format_valid(格式)
- 代码产出物 → code_check(静态检查) + test_check(测试)

【常用评估指标】
- file_check: 文件存在性检查。input_params: {"path": "文件路径"}
- format_valid: 格式验证。input_params: {"path": "文件路径"} 或 {"data": "数据内容"}
- test_check: 测试检查。input_params: {"command": "测试命令"}
- code_check: 代码静态检查。input_params: {"command": "检查命令"}
- bash_check: 命令执行检查。input_params: {"command": "要执行的命令"}
- api_check: API响应检查。input_params: {"url": "API URL"}
- function_verify: 功能验证。input_params: {"requirement": "需求描述", "implementation": "实现代码"}
- semantic_check: 质量评估。input_params: {"criteria": "评估要求描述（自然语言）"} 或 {}（不传参数，评估Agent根据任务目标自动评估）
- human_review: 人工审核。input_params: {"mode": "choice", "title": "审核标题"}

【重要】所有参数值必须是正确类型：string传字符串，object传对象(不能用字符串代替)，array传数组。
""".strip(),
                        "additionalProperties": {
                            "type": "object",
                            "description": "评估指标的配置对象",
                            "properties": {
                                "input_params": {
                                    "type": "object",
                                    "description": '传递给评估工具的参数。例如 file_check 需要 {"path": "src/main.py"}，可选 action 参数指定操作类型',
                                },
                                "expected_output": {
                                    "type": "object",
                                    "description": "预期输出，用于验证评估结果（可选）",
                                },
                                "pass_threshold": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 100,
                                    "description": "任务级别的通过阈值（0-100），优先级高于指标默认阈值",
                                },
                            },
                            "required": [],
                        },
                    },
                    "priority": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                        "description": "任务优先级，1-10，数值越大优先级越高",
                    },
                    "max_retries": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 3,
                        "description": "任务失败时的最大重试次数",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "元数据，存储额外的任务信息（可选）",
                    },
                    "needs_preparation": {
                        "type": "boolean",
                        "description": "是否需要准备阶段（调研、分解、规划）。复杂任务建议设为 true",
                    },
                    "task_scope": {
                        "type": "string",
                        "enum": ["non_container", "container"],
                        "default": "non_container",
                        "description": (
                            "任务范围：container（容器任务，用于组织复杂长期任务的子任务链，"
                            "不能指定 target_type 和 target_id）或 "
                            "non_container（非容器任务，实际执行的任务，"
                            "必须指定 target_type 和 target_id）"
                        ),
                    },
                    "parent_task_id": {
                        "type": "string",
                        "description": (
                            "父任务 ID。为容器任务创建子任务时需要指定此参数，"
                            "将子任务关联到对应的容器。"
                        ),
                    },
                    "task_role": {
                        "type": "string",
                        "enum": ["solution_preparation", "solution_refinement", "final_validation"],
                        "description": (
                            "子任务角色标记（可选）。用于容器任务的子任务，"
                            "标记该子任务在容器中的角色。"
                            "final_validation 表示最终验证任务，"
                            "容器完成条件要求至少有一个 final_validation 子任务通过"
                        ),
                    },
                    "workspace": {
                        "type": "string",
                        "description": """
工作空间路径。不设置：系统在 .ai_workspaces 下创建新目录（新建项目）。
设置为已存在的项目目录（绝对路径）：系统创建隔离副本进行修改（改造现有项目）。
改造 Agent OS 自身时，使用 {{project_root}} 变量。子任务不需要设置此参数，自动继承父任务的工作空间。
""".strip(),
                    },
                },
                "required": ["goal"],
                "allOf": [
                    {
                        "if": {
                            "not": {
                                "required": ["task_scope"],
                                "properties": {"task_scope": {"const": "container"}}
                            }
                        },
                        "then": {
                            "required": [
                                "target_type",
                                "target_id",
                                "acceptance_criteria",
                            ],
                            "properties": {
                                "acceptance_criteria": {
                                    "type": "object",
                                    "minProperties": 1,
                                    "additionalProperties": {
                                        "type": "object",
                                        "required": ["input_params"],
                                    },
                                },
                            },
                        },
                    },
                    {
                        "if": {
                            "required": ["task_scope"],
                            "properties": {"task_scope": {"const": "container"}}
                        },
                        "then": {
                            "not": {
                                "anyOf": [
                                    {"required": ["target_type"]},
                                    {"required": ["target_id"]},
                                    {"required": ["parent_task_id"]},
                                ]
                            }
                        },
                    },
                ],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.L1_L2_ONLY,
            tags=["task", "submit"],
            injected_params=[
                "user_id",
                "session_id",
                "task_id",
                "pipeline_id",
                "dependencies",
                "tool_record_id",
                "parent_agent_level",
            ],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行任务提交。

        流程：
        1. 参数验证（goal, target_type, target_id, acceptance_criteria）
        2. 验证 parent_task_id 权限
        3. 验证依赖任务存在
        4. 使用 TaskService 创建任务（status=pending）
        5. 通过 EventBus 发布 task.submitted 事件
        6. 返回提交结果
        """
        task_scope = inputs.get("task_scope", "non_container")
        goal = inputs.get("goal")
        if isinstance(goal, str):
            import json
            try:
                goal = json.loads(goal)
            except (json.JSONDecodeError, ValueError):
                # LLM 可能将 goal 作为纯文本标题传递而非 JSON 对象，
                # 此时将其作为 title 使用
                goal = {"title": goal}
                logger.info(
                    "[TaskSubmit] goal 为纯文本，自动包装为 {'title': '%s'}",
                    goal["title"][:80],
                )
        if not isinstance(goal, dict):
            logger.warning(
                "[TaskSubmit] goal 类型异常: %s (value=%s)",
                type(goal).__name__ if goal is not None else "None",
                str(goal)[:200] if goal else "None",
            )
            goal = None
        parent_agent_level = inputs.get("parent_agent_level", 1)

        logger.info(
            "[TaskSubmit] 开始执行 | task_scope=%s | parent_agent_level=%s",
            task_scope, parent_agent_level,
        )

        # ── 1. 基础参数验证 ──
        if not goal or not goal.get("title"):
            logger.error("[TaskSubmit] 参数验证失败 | goal 为空")
            return create_failure_result(
                error="必须提供 goal（含 title 字段）",
                error_code="MISSING_GOAL",
            )

        # 容器任务走独立分支
        if task_scope == "container":
            return await self._execute_long_term(inputs)

        target_type = inputs.get("target_type")
        target_id = inputs.get("target_id")
        description = inputs.get("description") or goal.get("description", "")
        acceptance_criteria = inputs.get("acceptance_criteria", {})
        parent_task_id = inputs.get("parent_task_id")

        # BUG-FIX-fix_20260420_eval_inject: LLM 可能传入非 dict 类型的
        # acceptance_criteria（如字符串、列表），导致跳过自动补全又跳过验证。
        # 统一规范化为 dict，非 dict 视为空以触发自动补全。
        if not isinstance(acceptance_criteria, dict):
            logger.warning(
                "[TaskSubmit] acceptance_criteria 类型异常: %s，重置为空 dict 以触发自动补全",
                type(acceptance_criteria).__name__,
            )
            acceptance_criteria = {}

        # BUG-FIX-fix_20260419_auto_criteria: LLM 可能不传 acceptance_criteria，
        # 当 target_id 是已知 agent 时，自动从 agent 配置的 recommended_metrics 中补全。
        # BUG-FIX-fix_20260424_use_recommended_only: 当目标 agent 定义了
        # recommended_metrics 时，只使用这些指标，忽略 LLM 传入的额外指标。
        # 原因：上级不知道下级的具体文件路径，LLM 可能添加路径错误的 file_check。
        if target_type == "agent" and target_id:
            auto_criteria = self._auto_fill_criteria(
                target_id, context=inputs,
            )
            if auto_criteria:
                if not acceptance_criteria:
                    acceptance_criteria = auto_criteria
                    logger.info(
                        "[TaskSubmit] 自动补全验收标准 | target_id=%s | metrics=%s",
                        target_id, list(auto_criteria.keys()),
                    )
                else:
                    # recommended_metrics 存在时，以配置为准，忽略 LLM 传入的额外指标
                    discarded = [
                        k for k in acceptance_criteria
                        if k not in auto_criteria
                    ]
                    if discarded:
                        logger.info(
                            "[TaskSubmit] 丢弃 LLM 传入的非推荐指标: %s "
                            "(以 %s recommended_metrics 为准)",
                            discarded, target_id,
                        )
                    acceptance_criteria = auto_criteria
        injected_task_id = inputs.get("task_id")
        if parent_task_id is None and injected_task_id:
            parent_task_id = injected_task_id
            logger.info(
                "[TaskSubmit] 自动注入 parent_task_id=%s (来自管道所属任务)",
                parent_task_id,
            )
        workspace = inputs.get("workspace", "")

        logger.info(
            "[TaskSubmit] 非容器任务 | target_type=%s | target_id=%s",
            target_type, target_id,
        )
        logger.debug(
            "[TaskSubmit] 任务详情 | title=%s | metric_count=%d",
            goal.get("title", "N/A"), len(acceptance_criteria),
        )

        # ── 2. 非容器任务必填参数验证 ──
        if not target_type:
            return create_failure_result(
                error="目标类型不能为空",
                error_code="MISSING_TARGET_TYPE",
            )
        if not target_id:
            return create_failure_result(
                error="目标 ID 不能为空",
                error_code="MISSING_TARGET_ID",
            )
        if not acceptance_criteria:
            return create_failure_result(
                error="必须提供 acceptance_criteria",
                error_code="MISSING_METRICS",
            )

        # ── 3. 权限验证 ──
        if not self._validate_parent_task_id(
            parent_agent_level, parent_task_id, task_scope
        ):
            return create_failure_result(
                error="L2 Agent 不能显式指定 parent_task_id（系统会自动注入当前任务 ID）",
                error_code="L2_CANNOT_SPECIFY_PARENT_TASK_ID",
            )

        # ── 4. 依赖任务验证 ──
        dependencies = inputs.get("dependencies", [])
        if dependencies:
            missing_ids = self._check_dependencies_exist(dependencies)
            if missing_ids:
                logger.error("[TaskSubmit] 依赖验证失败 | 不存在的任务: %s", missing_ids)
                return create_failure_result(
                    error=f"依赖任务不存在: {missing_ids}",
                    error_code="DEPENDENCY_NOT_FOUND",
                )
            logger.info("[TaskSubmit] 依赖验证通过 | dependencies=%s", dependencies)

        # ── 5. 获取服务 ──
        task_service = self._get_task_service()
        if task_service is None:
            return create_failure_result(
                error="任务服务不可用，请检查系统配置",
                error_code="SERVICE_UNAVAILABLE",
            )

        # ── 6. 创建任务 ──
        raw_priority = inputs.get("priority", 5)
        try:
            from tasks.types import TaskPriority as TP
            TP(raw_priority)
        except (ValueError, AttributeError):
            raw_priority = 5

        try:
            child_agent_level = min(parent_agent_level + 1, 3)
            from agents.types import AgentLevel
            level_values = {"L1": 1, "L2": 2, "L3": 3}
            level_str = f"L{child_agent_level}"
            if level_str in level_values:
                child_level = AgentLevel(level_str)
            else:
                child_level = AgentLevel.L3_ATOMIC

            pipeline_id = inputs.get("pipeline_id")
            task = task_service.create_task(
                title=goal["title"],
                description=description,
                parent_task_id=parent_task_id,
                parent_pipeline_id=pipeline_id,
                target_type=target_type,
                dependencies=dependencies or None,
                priority=raw_priority,
                agent_level=child_level,
                metadata=self._build_metadata(inputs, goal, acceptance_criteria),
            )
        except Exception as e:
            logger.error("[TaskSubmit] 任务创建失败: %s", e)
            return create_failure_result(
                error=f"任务创建失败: {e}",
                error_code="TASK_CREATE_FAILED",
            )

        # ── 7. 发布事件（BUG-FIX-P3：EventBus 不可用或失败时回滚任务） ──
        event_bus = self._get_event_bus()
        if event_bus is None:
            logger.error("[TaskSubmit] EventBus 不可用，回滚任务 %s", task.id)
            try:
                task_service._storage.delete(task.id)
            except Exception as del_e:
                logger.error("[TaskSubmit] 回滚失败（_storage.delete）: %s", del_e)
            return create_failure_result(
                error="任务提交失败：EventBus 不可用，无法触发后台执行",
                error_code="EVENT_BUS_UNAVAILABLE",
            )

        # BUG-FIX-C3：emit 前检查是否有订阅者，防止 TaskWorker 未启动时任务永远不被执行
        if hasattr(event_bus, 'has_subscribers') and not event_bus.has_subscribers("task.submitted"):
            logger.error("[TaskSubmit] 无订阅者: task.submitted, 回滚任务 %s", task.id)
            try:
                task_service._storage.delete(task.id)
            except Exception as del_e:
                logger.error("[TaskSubmit] 回滚失败: %s", del_e)
            return create_failure_result(
                error="任务提交失败：后台执行器(TaskWorker)未启动，无法处理任务",
                error_code="NO_SUBSCRIBER",
            )

        try:
            # Determine is_root: container sub-tasks (parent is container) get own workspace,
            # agent sub-tasks (parent is non_container) share parent workspace.
            is_root = True
            if parent_task_id and task_service:
                try:
                    parent_task = task_service.get_task(parent_task_id)
                    if parent_task and parent_task.metadata:
                        parent_scope = parent_task.metadata.get("task_scope", "non_container")
                        if parent_scope != "container":
                            is_root = False
                except Exception:
                    pass

            await event_bus.emit("task.submitted", {
                "task_id": task.id,
                "target_type": target_type,
                "target_id": target_id,
                "user_input": goal.get("title", ""),
                "description": description or goal.get("description", ""),
                "acceptance_criteria": acceptance_criteria,
                "workspace": workspace,
                "priority": inputs.get("priority", 5),
                "is_root": is_root,
            })
            logger.info("[TaskSubmit] 事件已发布 | task_id=%s", task.id)
        except Exception as e:
            logger.error("[TaskSubmit] EventBus emit 失败，回滚任务 %s: %s", task.id, e)
            try:
                task_service._storage.delete(task.id)
            except Exception as del_e:
                logger.error("[TaskSubmit] 回滚失败（_storage.delete）: %s", del_e)
            return create_failure_result(
                error=f"任务提交失败：事件发布异常 - {e}",
                error_code="EVENT_BUS_ERROR",
            )

        logger.info("[TaskSubmit] 任务提交成功 | task_id=%s | title=%s", task.id, task.title)

        return create_success_result(
            data={
                "task_id": task.id,
                "title": task.title,
                "status": task.status.value,
                "target_type": target_type,
                "target_id": target_id,
                "submit_status": "submitted",
                # BUG-FIX-P1：返回消息与 Agent 指令冲突，改为说明异步等待机制
                "message": (
                    f"任务 [{task.title}]（ID: {task.id}）已提交，目标执行者：{target_id}，状态：异步执行中。"
                    "该任务需要一定时间完成。"
                    "子任务完成后系统会自动通知你并恢复执行。"
                    "在此期间请不要再调用任何工具（包括 task_manage），直接输出纯文本等待即可。"
                ),
            },
            metadata={
                "action": "task_submit",
                "task_scope": task_scope,
            },
        )

    async def _execute_long_term(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """处理容器任务提交。

        容器任务不指定执行者，只创建一个 pending 状态的父任务框架。

        Args:
            inputs: 工具输入参数

        Returns:
            工具执行结果
        """
        goal = inputs.get("goal")
        parent_agent_level = inputs.get("parent_agent_level", 1)

        logger.info(
            "[TaskSubmit] 容器任务提交 | title=%s | parent_agent_level=%s",
            goal.get("title") if goal else "N/A", parent_agent_level,
        )

        if parent_agent_level != 1:
            return create_failure_result(
                error="容器任务只能由 L1 Agent 提交",
                error_code="L2_CANNOT_SUBMIT_CONTAINER",
            )

        task_service = self._get_task_service()
        if task_service is None:
            return create_failure_result(
                error="任务服务不可用，请检查系统配置",
                error_code="SERVICE_UNAVAILABLE",
            )

        try:
            description = inputs.get("description") or goal.get("description", "")
            pipeline_id = inputs.get("pipeline_id")
            task = task_service.create_task(
                title=goal["title"],
                description=description,
                parent_pipeline_id=pipeline_id,
                metadata=self._build_metadata(inputs, goal, {}),
            )
        except Exception as e:
            logger.error("[TaskSubmit] 容器任务创建失败: %s", e)
            return create_failure_result(
                error=f"容器任务创建失败: {e}",
                error_code="TASK_CREATE_FAILED",
            )

        # 将当前管道 ID 绑定到容器任务，使子任务完成时能通知父管道
        pipeline_id = inputs.get("pipeline_id")
        if pipeline_id:
            try:
                task_service.bind_pipeline_run(task.id, pipeline_id)
                logger.info(
                    "[TaskSubmit] 容器任务已绑定管道 | task_id=%s | pipeline_id=%s",
                    task.id, pipeline_id,
                )
                # 按根任务分组执行记录
                exec_storage = self._get_execution_record_storage()
                if exec_storage:
                    root_id = task_service.get_root_task_id(task.id)
                    if root_id:
                        exec_storage.register_pipeline(pipeline_id, root_id)
            except Exception as exc:
                logger.warning(
                    "[TaskSubmit] 容器任务绑定管道失败 | task_id=%s | error=%s",
                    task.id, exc,
                )

        logger.info("[TaskSubmit] 容器任务提交成功 | task_id=%s | title=%s", task.id, task.title)

        # BUG-FIX-fix_20260425_container_workspace_race:
        # 问题根因: 容器任务不发布 task.submitted 事件，TaskWorker 不会收到通知，
        #           因此不会调用 _execute_background_task 中的 init_container_workspace。
        #           子任务提交后找不到容器工作空间 → 初始化失败。
        # 修复方案: 容器任务也发布 task.submitted 事件，TaskWorker 会跳过业务逻辑执行，
        #           但会执行容器工作空间初始化（mkdir + git init）。
        event_bus = self._get_event_bus()
        if event_bus:
            try:
                await event_bus.emit("task.submitted", {
                    "task_id": task.id,
                    "target_type": None,
                    "target_id": None,
                    "user_input": goal.get("title", ""),
                    "description": description or goal.get("description", ""),
                    "acceptance_criteria": {},
                    "workspace": inputs.get("workspace"),
                    "parent_task_id": None,
                    "is_root": True,
                })
                logger.info("[TaskSubmit] 容器任务事件已发布 | task_id=%s", task.id)
            except Exception as exc:
                logger.warning("[TaskSubmit] 容器任务事件发布失败 | task_id=%s | error=%s", task.id, exc)

        return create_success_result(
            data={
                "task_id": task.id,
                "title": task.title,
                "status": task.status.value,
                "task_scope": "container",
                "submit_status": "submitted",
                # BUG-FIX-fix_20260425_container_flow:
                # 问题根因: 容器任务返回消息说"请先继续处理其他工作"和"在收到系统提醒前，不要查询任务状态"，
                #           导致 LLM 误以为不需要继续操作，直接结束对话，未提交子任务。
                # 修复方案: 容器任务只是组织框架，LLM 必须在同一轮对话中立即提交准备子任务。
                #           返回消息明确引导 LLM 继续提交 solution_preparation_agent 子任务。
                "message": (
                    f"容器任务 [{task.title}]（ID: {task.id}）已提交。"
                    "容器只是组织框架，不直接执行。你现在必须立即继续操作："
                    f"下一步——使用 task_submit(parent_task_id='{task.id}', target_type='agent', "
                    "target_id='solution_preparation_agent') 提交方案准备子任务。"
                    "请在同一轮对话中立即调用，不要等待。"
                ),
            },
            metadata={"action": "task_submit_container"},
        )

    def _validate_parent_task_id(
        self, parent_agent_level: int, parent_task_id: str | None, task_scope: str
    ) -> bool:
        """验证 parent_task_id 参数的使用权限。

        Args:
            parent_agent_level: 父 Agent 层级
            parent_task_id: 用户指定的父任务 ID
            task_scope: 任务范围

        Returns:
            验证是否通过
        """
        if task_scope == "container":
            if parent_task_id is not None:
                logger.warning("[TaskSubmit] 容器任务不能有父任务 | parent_task_id=%s", parent_task_id)
                return False
            return True

        if parent_agent_level == 1 and parent_task_id is not None:
            task_service = self._get_task_service()
            if task_service and task_service.get_task(parent_task_id) is None:
                logger.error("[TaskSubmit] parent_task_id 不存在: %s", parent_task_id)
                return False

        return True

    def _check_dependencies_exist(self, dependencies: list[str]) -> list[str]:
        """检查依赖任务是否存在。

        通过 TaskService 查询每个依赖任务，返回不存在的 ID 列表。

        Args:
            dependencies: 依赖任务 ID 列表

        Returns:
            不存在的任务 ID 列表
        """
        if not dependencies:
            return []

        task_service = self._get_task_service()
        if task_service is None:
            logger.warning("[TaskSubmit] TaskService 不可用，跳过依赖检查")
            return []

        missing_ids = []
        for dep_id in dependencies:
            if task_service.get_task(dep_id) is None:
                missing_ids.append(dep_id)
        return missing_ids

    def _build_metadata(
        self,
        inputs: dict[str, Any],
        goal: dict[str, Any],
        acceptance_criteria: dict[str, Any],
    ) -> dict[str, Any]:
        """构建任务元数据。

        将 acceptance_criteria、goal context、workspace、max_retries 等信息
        存入 metadata 以便后续流程使用。

        Args:
            inputs: 工具输入参数
            goal: 任务目标字典
            acceptance_criteria: 验收标准字典

        Returns:
            合并后的元数据字典
        """
        metadata: dict[str, Any] = {}

        # 合并用户传入的 metadata
        if inputs.get("metadata"):
            metadata.update(inputs["metadata"])

        # 存储验收标准（供 task_evaluate 使用）
        # BUG-FIX-fix_20260420_eval_inject: 防御性检查，确保非 dict 类型不会静默丢失
        if acceptance_criteria:
            if isinstance(acceptance_criteria, dict):
                metadata["acceptance_criteria"] = acceptance_criteria
                metadata["evaluation_metric_ids"] = list(acceptance_criteria.keys())
            else:
                logger.warning(
                    "[TaskSubmit] _build_metadata 收到非 dict 的 acceptance_criteria: %s，跳过存储",
                    type(acceptance_criteria).__name__,
                )

        # 存储 goal 中的上下文
        if goal.get("context"):
            metadata["goal_context"] = goal["context"]

        # 存储执行相关参数
        # BUG-FIX-fix_20260422_workspace_nesting: 子任务不存储 LLM 传递的 workspace，
        # 子任务的 workspace 由祖先链自动解析，存储会导致路径双重嵌套
        if inputs.get("workspace") and not inputs.get("parent_task_id"):
            metadata["workspace"] = inputs["workspace"]
        if inputs.get("max_retries"):
            metadata["max_retries"] = inputs["max_retries"]
        if inputs.get("needs_preparation"):
            metadata["needs_preparation"] = inputs["needs_preparation"]
        if inputs.get("task_scope"):
            metadata["task_scope"] = inputs["task_scope"]

        # 存储执行者信息
        if inputs.get("target_id"):
            metadata["target_id"] = inputs["target_id"]

        # 存储任务角色标记（用于容器完成条件判断）
        # 可选值：solution_preparation、solution_refinement、final_validation
        if inputs.get("task_role"):
            metadata["task_role"] = inputs["task_role"]

        return metadata

    def _auto_fill_criteria(
        self,
        target_id: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """从 agent 配置的 recommended_metrics 自动构建验收标准。

        当 LLM 不传 acceptance_criteria 时，尝试从对应 agent 的 YAML 配置中
        读取 recommended_metrics 并转换为 task_evaluate 可用的格式。

        Args:
            target_id: agent 配置 ID
            context: 额外上下文（如 inputs/goal），用于替换模板变量

        Returns:
            验收标准字典，找不到时返回空 dict
        """
        import yaml
        from pathlib import Path

        # BUG-FIX-fix_20260420_eval_inject: 使用文件路径推导项目根目录，
        # 而非 Path.cwd()，避免工作目录变化导致找不到配置。
        # task_submit.py 位于 src/tools/builtin/，向上 4 层即为项目根目录。
        _project_root = Path(__file__).resolve().parent.parent.parent.parent
        config_dir = _project_root / "config" / "agents"

        if not config_dir.exists():
            logger.warning(
                "[TaskSubmit] agent 配置目录不存在: %s", config_dir,
            )
            return {}

        yaml_path = None
        for p in config_dir.rglob(f"{target_id}.yaml"):
            yaml_path = p
            break

        if not yaml_path or not yaml_path.exists():
            logger.debug(
                "[TaskSubmit] 未找到 agent 配置: %s (搜索目录: %s)",
                target_id, config_dir,
            )
            return {}

        try:
            with open(yaml_path, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            recommended = config.get("recommended_metrics", [])
            if not recommended:
                return {}

            # Build template variables from context
            tmpl_vars: dict[str, str] = {}
            if context:
                for key in ("tool_id", "task_id"):
                    val = context.get(key)
                    if val:
                        tmpl_vars[key] = str(val)
                # Also check nested goal/inputs
                goal = context.get("goal") or {}
                if isinstance(goal, dict):
                    for key in ("tool_id", "title"):
                        val = goal.get(key)
                        if val:
                            tmpl_vars.setdefault(key, str(val))
                    # Extract agent_id from goal.context (set by L2 agents)
                    goal_ctx = goal.get("context")
                    if isinstance(goal_ctx, dict):
                        for key in ("agent_id", "agent_name", "output_dir"):
                            val = goal_ctx.get(key)
                            if val:
                                tmpl_vars.setdefault(key, str(val))

            criteria = {}
            for metric in recommended:
                metric_id = metric.get("metric_id")
                if not metric_id:
                    continue
                default_params = metric.get("default_params", {})
                # 如果参数值包含未解析的 {{...}} 模板变量，跳过整个指标。
                # 模板变量应在提交时被替换为具体值，不应存储模板字符串。
                has_unresolved = any(
                    isinstance(v, str) and "{{" in v and "}}" in v
                    for v in default_params.values()
                )
                if has_unresolved:
                    logger.info(
                        "[TaskSubmit] 跳过指标 %s: 包含未解析的模板变量 (params=%s)",
                        metric_id, default_params,
                    )
                    continue
                # 用已知变量替换单花括号模板变量 {var}
                if tmpl_vars:
                    replaced = {}
                    for k, v in default_params.items():
                        if isinstance(v, str):
                            for var_name, var_val in tmpl_vars.items():
                                v = v.replace("{" + var_name + "}", var_val)
                        replaced[k] = v
                    default_params = replaced
                criteria[metric_id] = {"input_params": default_params}
            logger.info(
                "[TaskSubmit] 从 %s 加载了 %d 个推荐指标 (vars=%s)",
                yaml_path.name, len(criteria), list(tmpl_vars.keys()),
            )
            return criteria
        except Exception as e:
            logger.debug("[TaskSubmit] 自动补全验收标准失败: %s", e)
            return {}
