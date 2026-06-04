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

    负责创建任务并通过 TaskWorker 提交后台执行。

    依赖：
    - TaskService：任务创建与存储（JSON 文件存储）
    - TaskWorker：后台任务执行器
    """

    def __init__(self) -> None:
        """初始化任务提交工具"""
        self._task_service: Any = None

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
        service = provider.get_or_create(
            "task_service",
            lambda: __import__("tasks.service", fromlist=["TaskService"]).TaskService(
                event_bus=provider.get("event_bus"),
            ),
        )
        if service is not None:
            self._task_service = service
        return self._task_service

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义（标准 OpenAI Function Calling 格式）"""
        return Tool(
            name="task_submit",
            description="""
任务提交工具。将任务提交给指定的 Agent 执行，配置验收标准确保结果可验证。

【示例】
{"target_type": "agent", "target_id": "code_writer_agent", "goal": {"title": "实现用户登录"}, "acceptance_criteria": {"file_check": {"input_params": {"path": "src/auth/login.py"}}}}
注意：target_id 应使用系统提供的 Agent 映射表中的专用 Agent ID，不要用 general_agent 替代。
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
                        "description": "目标 Agent ID。non_container 必填，container 不需要。如果系统提供了 Agent 映射表，直接使用映射表中的 ID，不要用 resource_search 搜索",
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
                                "description": (
                                    "任务描述。只写目标、背景和要求，"
                                    "禁止写执行步骤、工具选择、流程顺序等执行细节。"
                                    "如实传递用户明确说明的要求，禁止添加主观判断。"
                                    "如果当前会话有相关的历史任务产出物，必须附上文件路径，避免重复工作。"
                                    "正确示例：'实现用户登录API，支持邮箱+密码登录，返回JWT token'。"
                                    "错误示例：'先用file_write创建login.py，再写LoginService类，"
                                    "然后用bash_execute安装依赖，最后用test工具测试'"
                                ),
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
如果系统提供了 Agent 映射表，映射表的"推荐指标"列已包含推荐评估指标（格式：metric_id(参数键值对)），直接使用即可。如果没有映射表，可用 resource_search(resource_type="agent", query="agent名称") 搜索 Agent，返回的 description 中包含推荐评估指标。

【按产出物选择指标】
- 文件产出物 → file_check(存在) + format_valid(格式)
- 代码产出物 → file_check + format_valid（一般任务）/ function_verify + semantic_check（仅重要任务）
- 文档/方案产出物 → file_check（一般任务）/ semantic_check（仅重要任务）
- 需要人工确认 → human_review(人工审核)

⚠️ Agent 评估器（semantic_check / function_verify）会调用 LLM，有较高 token 成本，仅在重要任务（涉及核心功能、影响项目质量、用户明确要求质量）中使用。简单任务仅用 tool 类指标（file_check、format_valid 等）即可。

【常用评估指标】
- file_check: 文件存在性检查。input_params: {"path": "具体文件路径（如 tests/test_login.py，禁止传目录如 tests/）"}
- format_valid: 格式验证。input_params: {"path": "文件路径"} 或 {"data": "数据内容"}
- semantic_check: 质量评估（调用 LLM，慎用）。input_params: {"criteria": "评估要求描述（自然语言）"} 或 {}（不传参数，评估Agent根据任务目标自动评估）
- function_verify: 功能验证（调用 LLM，慎用）。input_params: {"requirement": "需求描述", "implementation": "实现代码"}
- human_review: 人工审核。input_params: {"mode": "choice", "title": "审核标题"}

【重要】所有参数值必须是正确类型：string传字符串，object传对象(不能用字符串代替)，array传数组。
""".strip(),
                        "additionalProperties": {
                            "type": "object",
                            "description": "评估指标的配置对象",
                            "properties": {
                                "input_params": {
                                    "type": "object",
                                    "description": '传递给评估工具的参数。例如 file_check 需要 {"path": "具体文件路径（如 src/main.py）"}',
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
                            "任务范围：non_container（非容器任务，实际执行的任务，"
                            "必须指定 target_type 和 target_id）。"
                            "container 仅限 L1 Agent 使用（用于组织复杂长期任务的子任务链），"
                            "L2 Agent 禁止使用 container"
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
                        "enum": ["solution_planning", "final_validation"],
                        "description": (
                            "子任务角色标记（可选）。用于容器任务的子任务，"
                            "标记该子任务在容器中的角色。"
                            "final_validation 表示最终验证任务，"
                            "容器完成条件要求至少有一个 final_validation 子任务通过"
                        ),
                    },
                    "workspace": {
                        "type": "string",
                        "description": (
                            "目标项目路径。指定任务需要操作（读取或修改）的项目目录，"
                            "系统会在工作空间中基于该目录创建隔离的 worktree 副本进行操作。"
                            "**重要**：当任务需要对某个特定文件夹进行读取或修改时，"
                            "必须设置此参数为该目标文件夹的路径，否则任务将无法定位到正确的目标目录。"
                            "容器任务：传入目标项目目录时，系统会复制到隔离空间中操作；"
                            "不设置则创建空的工作空间。"
                            "非容器任务（子任务）：不设置时系统自动基于父容器空间创建 worktree；"
                            "传入目标项目路径时基于该项目创建 worktree。"
                            "如需直接在目标项目目录工作（不隔离），设置 isolation_level 为 host。"
                        ),
                    },
                    "isolation_level": {
                        "type": "string",
                        "enum": ["host", "container"],
                        "description": (
                            "隔离级别（可选，默认使用系统配置）。"
                            "host：直接在原空间工作，不做隔离。适合在已有项目上直接修改。"
                            "container：在隔离的工作空间中工作，不影响原项目。"
                        ),
                    },
                    "inherit_workspace_from": {
                        "type": "string",
                        "description": (
                            "继承之前任务的工作空间（可选）。传入旧任务 ID，"
                            "新任务直接复用旧任务的工作空间路径（不复制、不初始化），"
                            "新 Agent 能看到旧任务的所有文件。"
                            "用于重试策略步骤⑤重新提交时，让新任务在旧工作空间中继续工作。"
                            "如果旧工作空间已不存在，继承不生效，走正常空工作空间创建。"
                        ),
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
            param_level_restrictions={
                "task_scope": {
                    "enum_restrictions": {
                        "non_container": 0,
                        "container": 1,
                    },
                },
            },
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
        import time as _time
        _t0 = _time.monotonic()
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
        parent_agent_level = inputs.get("parent_agent_level")

        logger.info(
            "[TaskSubmit] 开始执行 | task_scope=%s | parent_agent_level=%s",
            task_scope, parent_agent_level,
        )

        # ── 0. 注入参数校验 ──
        if parent_agent_level is None:
            logger.error("[TaskSubmit] 注入参数缺失 | parent_agent_level 未注入")
            return create_failure_result(
                error="系统错误：parent_agent_level 未注入，无法确定调用者层级",
                error_code="MISSING_INJECTED_PARAM",
            )

        # ── 1. 基础参数验证 ──
        if not goal or not goal.get("title"):
            logger.error("[TaskSubmit] 参数验证失败 | goal 为空")
            return create_failure_result(
                error="必须提供 goal（含 title 字段）",
                error_code="MISSING_GOAL",
            )

        # 容器任务走独立分支（_execute_long_term 内部也有层级校验，
        # 此处提前拦截避免进入容器创建流程）
        if task_scope == "container":
            if parent_agent_level >= 2:
                logger.warning(
                    "[TaskSubmit] L%d Agent 试图创建容器任务，已拦截",
                    parent_agent_level,
                )
                return create_failure_result(
                    error=(
                        "L2/L3 Agent 不能创建 container 任务。"
                        "你已在 non_container 任务中，"
                        "直接使用 task_submit(task_scope='non_container') "
                        "创建子任务即可"
                    ),
                    error_code="L2_CANNOT_SUBMIT_CONTAINER",
                )
            return await self._execute_long_term(inputs)

        target_type = inputs.get("target_type")
        target_id = inputs.get("target_id")
        description = inputs.get("description") or goal.get("description", "")
        acceptance_criteria = inputs.get("acceptance_criteria", {})
        parent_task_id = inputs.get("parent_task_id")

        # BUG-FIX-fix_20260530_description_lost: 诊断日志
        # 追踪 description 在 task_submit 入口的值
        logger.info(
            "[TaskSubmit] description 追踪 | has_inputs_desc=%s | has_goal_desc=%s | final_desc_len=%d | preview=%s",
            bool(inputs.get("description")),
            bool(goal.get("description")),
            len(description),
            description[:80] if description else "(empty)",
        )

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

        # ── L2/L3 层级校验：禁止显式指定 parent_task_id ──
        if parent_agent_level >= 2 and task_scope != "container" and parent_task_id is not None:
            logger.warning(
                "[TaskSubmit] L%d Agent 显式指定 parent_task_id=%s，已拦截",
                parent_agent_level, parent_task_id,
            )
            return create_failure_result(
                error=f"L{parent_agent_level} Agent 不能显式指定 parent_task_id"
                     "（系统自动注入当前任务 ID）",
                error_code="L2_CANNOT_SPECIFY_PARENT_TASK_ID",
            )

        injected_task_id = inputs.get("task_id")
        if parent_task_id is None and injected_task_id:
            parent_task_id = injected_task_id
            logger.info(
                "[TaskSubmit] 自动注入 parent_task_id=%s (来自管道所属任务)",
                parent_task_id,
            )

        # ── 子任务：清除自动注入的 workspace ──
        # ParamInjectPlugin 会将当前管道的 workspace 注入所有工具调用，
        # 但子任务的工作空间由父任务链自动解析，不能使用注入值，否则路径双重嵌套。
        if parent_task_id and inputs.get("workspace"):
            logger.info(
                "[TaskSubmit] 子任务清除自动注入的 workspace | parent_task_id=%s | workspace=%s",
                parent_task_id, inputs["workspace"],
            )
            del inputs["workspace"]

        # BUG-FIX-fix_20260523_l2_isolation_override:
        # 子任务无权决定隔离级别，由系统根据父任务链自动继承。
        # LLM 可能传入 isolation_level="host"，导致子任务绕过沙箱隔离。
        if parent_task_id and inputs.get("isolation_level"):
            logger.info(
                "[TaskSubmit] 子任务清除 LLM 传入的 isolation_level | parent_task_id=%s | isolation_level=%s",
                parent_task_id, inputs["isolation_level"],
            )
            del inputs["isolation_level"]

        # ── L2/L3 层级校验：自动注入后仍无 parent_task_id → 拒绝创建根任务 ──
        if parent_agent_level >= 2 and task_scope != "container" and parent_task_id is None:
            logger.warning(
                "[TaskSubmit] L%d Agent 无可注入的 parent_task_id，拒绝创建根任务",
                parent_agent_level,
            )
            return create_failure_result(
                error=f"L{parent_agent_level} Agent 必须在任务上下文中提交子任务，"
                     "无法创建根任务",
                error_code="L2_REQUIRES_PARENT_TASK",
            )

        workspace = inputs.get("workspace", "")

        # ── inherit_workspace_from 解析 ──
        # 直接复用旧任务的 ws_meta.path，不复制、不初始化。
        # 旧工作空间不存在则报错返回，让 agent 重新提交。
        inherit_from = inputs.get("inherit_workspace_from")
        _inherit_resolved = False
        old_ws_meta = None
        # inherit_workspace_from 显式指定时，覆盖 param_inject 注入的 workspace
        if inherit_from:
            task_service = self._get_task_service()
            if not task_service:
                return create_failure_result(
                    error=(
                        f"无法查找任务 {inherit_from}：任务服务不可用。"
                        "请去掉 inherit_workspace_from 参数重新提交，使用空工作空间。"
                    ),
                )
            try:
                old_task = task_service.get_task(inherit_from)
                if not old_task or not old_task.metadata:
                    return create_failure_result(
                        error=(
                            f"任务 {inherit_from} 不存在或无元数据。"
                            "请去掉 inherit_workspace_from 参数重新提交，使用空工作空间。"
                        ),
                    )
                old_ws_meta = old_task.metadata.get("ws_meta")
                if not isinstance(old_ws_meta, dict):
                    return create_failure_result(
                        error=(
                            f"任务 {inherit_from} 没有工作空间信息。"
                            "请去掉 inherit_workspace_from 参数重新提交，使用空工作空间。"
                        ),
                    )
                from pathlib import Path
                old_ws_path = old_ws_meta.get("path", "")
                if not old_ws_path or not Path(old_ws_path).exists():
                    return create_failure_result(
                        error=(
                            f"任务 {inherit_from} 的工作空间已不存在: {old_ws_path or '(空)'}。"
                            "无法继承，请去掉 inherit_workspace_from 参数重新提交，"
                            "使用空工作空间开始。"
                        ),
                    )
                workspace = old_ws_path
                _inherit_resolved = True
                logger.info(
                    "[TaskSubmit] inherit_workspace_from: "
                    "task_id=%s, ws_path=%s",
                    inherit_from, old_ws_path,
                )
            except Exception as e:
                logger.warning(
                    "[TaskSubmit] inherit_workspace_from 解析失败: %s", e,
                )
                return create_failure_result(
                    error=f"继承工作空间时出错: {e}。请去掉 inherit_workspace_from 参数重新提交。",
                )
        # 继承成功时回写 inputs，确保 _build_metadata 存储到任务元数据
        if _inherit_resolved:
            inputs["workspace"] = workspace

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

        # ── 2.5 目标 Agent 存在性与级别校验 ──
        if target_type == "agent":
            valid, err_msg, err_code = self._validate_target_agent(
                target_id, parent_agent_level,
            )
            if not valid:
                logger.warning(
                    "[TaskSubmit] 目标 Agent 校验失败 | target_id=%s | parent_level=L%d | error=%s",
                    target_id, parent_agent_level, err_msg,
                )
                return create_failure_result(error=err_msg, error_code=err_code)
            logger.info(
                "[TaskSubmit] 目标 Agent 校验通过 | target_id=%s | parent_level=L%d",
                target_id, parent_agent_level,
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
            # BUG-FIX-fix_20260512_async_compat: create_task 现在是 async
            task = await task_service.create_task(
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

        _t_create = _time.monotonic()
        logger.info("[TaskSubmit] PERF | create_task=%.1fms", (_t_create - _t0) * 1000)

        # ── 7. 提交到后台执行器 ──
        from infrastructure.service_provider import get_service_provider
        task_worker = get_service_provider().get("task_worker")
        if not task_worker:
            await task_service.hard_delete(task.id)
            return create_failure_result(
                error="后台执行器不可用，任务提交失败",
                error_code="SUBMIT_FAILED",
            )

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

        if _inherit_resolved:
            is_root = True

        task_data = {
            "task_id": task.id,
            "target_type": target_type,
            "target_id": target_id,
            "user_input": goal.get("title", ""),
            "description": description or goal.get("description", ""),
            "acceptance_criteria": acceptance_criteria,
            "workspace": workspace,
            "priority": inputs.get("priority", 5),
            "is_root": is_root,
            "_has_explicit_workspace": bool(workspace),
            "_inherit_workspace_resolved": _inherit_resolved,
            "_source_ws_meta": old_ws_meta if _inherit_resolved else None,
        }

        # BUG-FIX-fix_20260530_description_lost: 诊断日志
        logger.info(
            "[TaskSubmit] task_data description 追踪 | task_id=%s | desc_in_task_data=%s | desc_len=%d",
            task.id,
            bool(task_data.get("description")),
            len(task_data.get("description", "")),
        )
        if not task_worker.submit_task(task_data):
            await task_service.hard_delete(task.id)
            return create_failure_result(
                error="后台执行器未启动，任务提交失败",
                error_code="SUBMIT_FAILED",
            )

        _t_submit = _time.monotonic()
        logger.info("[TaskSubmit] PERF | submit_task=%.1fms | total=%.1fms", (_t_submit - _t_create) * 1000, (_t_submit - _t0) * 1000)

        logger.info("[TaskSubmit] 任务提交成功 | task_id=%s | title=%s", task.id, task.title)

        # BUG-FIX-fix_20260522_task_status_realtime:
        # 问题根因1: self._services 不存在，导致广播失败。
        # 问题根因2: task.user_id 未被设置（create_task 不接受 user_id），
        #            应用 inputs 中注入的 user_id。
        # 修复方案: 直接 import ws_interaction_notifier 单例 + 使用 inputs["user_id"]。
        try:
            _user_id = inputs.get("user_id", "") or ""
            from ws_handler import ws_interaction_notifier as _ws_notifier
            if _ws_notifier and _user_id and hasattr(_ws_notifier, "send_to_user"):
                await _ws_notifier.send_to_user(_user_id, {
                    "type": "task_status_update",
                    "data": {
                        "task_id": task.id,
                        "old_status": "",
                        "new_status": "pending",
                        "current_phase": "prepare",
                    },
                })
                logger.info(
                    "[TaskSubmit] task_status_update 已广播 | task_id=%s | user=%s | status=pending",
                    task.id, _user_id[:12],
                )
            else:
                logger.warning(
                    "[TaskSubmit] 跳过广播 | notifier=%s user=%s",
                    bool(_ws_notifier), _user_id[:12] if _user_id else "(empty)",
                )
        except Exception as _ws_exc:
            logger.warning(
                "[TaskSubmit] task_status_update 广播失败 | task_id=%s | error=%s",
                task.id, _ws_exc,
            )

        _t_ws = _time.monotonic()
        logger.info("[TaskSubmit] PERF | ws_broadcast=%.1fms | total=%.1fms", (_t_ws - _t_submit) * 1000, (_t_ws - _t0) * 1000)

        result_data = {
            "task_id": task.id,
            "title": task.title,
            "description": description,
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
        }

        return create_success_result(
            data=result_data,
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
        parent_agent_level = inputs.get("parent_agent_level")

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
            # BUG-FIX-fix_20260512_async_compat: create_task 现在是 async
            task = await task_service.create_task(
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
                await task_service.bind_pipeline_run(task.id, pipeline_id)
                logger.info(
                    "[TaskSubmit] 容器任务已绑定管道 | task_id=%s | pipeline_id=%s",
                    task.id, pipeline_id,
                )
                exec_storage = self._get_execution_record_storage()
                if exec_storage:
                    root_id = task_service.get_root_task_id(task.id)
                    if root_id:
                        exec_storage.register_pipeline(pipeline_id, root_id)

                # BUG-FIX-fix_20260603_api_store_pipeline_mapping:
                # 容器任务管道也注册到 api_store，保持 pipeline_ids 完整
                _session_id = inputs.get("session_id", "")
                if _session_id:
                    try:
                        from channels.api.memory_store import store as api_store
                        _session = api_store.get_session(_session_id)
                        if _session:
                            _session.register_pipeline(pipeline_id)
                            api_store.set_session(_session_id, _session)
                    except Exception as _reg_exc:
                        logger.warning(
                            "[TaskSubmit] 注册容器管道到 api_store 失败: %s", _reg_exc,
                        )
            except Exception as exc:
                logger.warning(
                    "[TaskSubmit] 容器任务绑定管道失败 | task_id=%s | error=%s",
                    task.id, exc,
                )

        logger.info("[TaskSubmit] 容器任务提交成功 | task_id=%s | title=%s", task.id, task.title)

        # BUG-FIX-fix_20260522_task_status_realtime:
        # 问题根因1: self._services 不存在，直接 import 单例。
        # 问题根因2: task.user_id 未被设置，使用 inputs 中注入的 user_id。
        try:
            _user_id = inputs.get("user_id", "") or ""
            from ws_handler import ws_interaction_notifier as _ws_notifier
            if _ws_notifier and _user_id and hasattr(_ws_notifier, "send_to_user"):
                await _ws_notifier.send_to_user(_user_id, {
                    "type": "task_status_update",
                    "data": {
                        "task_id": task.id,
                        "old_status": "",
                        "new_status": "pending",
                        "current_phase": "prepare",
                    },
                })
                logger.info(
                    "[TaskSubmit] 容器 task_status_update 已广播 | task_id=%s | user=%s | status=pending",
                    task.id, _user_id[:12],
                )
            else:
                logger.warning(
                    "[TaskSubmit] 容器跳过广播 | notifier=%s user=%s",
                    bool(_ws_notifier), _user_id[:12] if _user_id else "(empty)",
                )
        except Exception as _ws_exc:
            logger.warning(
                "[TaskSubmit] 容器 task_status_update 广播失败 | task_id=%s | error=%s",
                task.id, _ws_exc,
            )

        from infrastructure.service_provider import get_service_provider
        task_worker = get_service_provider().get("task_worker")
        if task_worker:
            task_worker.submit_task({
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

        # BUG-FIX-fix_20260519_container_workspace_path:
        # 路径计算逻辑集中在 isolation.workspace 模块。
        # 优先使用 LLM 传入的 isolation_level，没有才用配置文件。
        from isolation.workspace import resolve_container_workspace_path
        container_workspace_path = resolve_container_workspace_path(
            inputs.get("workspace"), task.id,
            isolation_mode=inputs.get("isolation_level"),
        )

        result_data = {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "task_scope": "container",
            "submit_status": "submitted",
        }

        if parent_agent_level == 1:
            result_data["workspace_path"] = container_workspace_path

        result_data["message"] = (
            f"容器任务 [{task.title}]（ID: {task.id}）已提交"
            + (f"，工作空间：{container_workspace_path}。" if parent_agent_level == 1 else "。")
            + "容器只是组织框架，不直接执行。你现在必须立即继续操作："
            f"下一步——使用 task_submit(parent_task_id='{task.id}', target_type='agent', "
            "target_id='solution_planning_agent') 提交方案规划子任务。"
            "请在同一轮对话中立即调用，不要等待。"
        )

        return create_success_result(
            data=result_data,
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

        # L2/L3 non-container: parent_task_id 必须已由自动注入填充
        if parent_agent_level >= 2 and parent_task_id is None:
            logger.warning(
                "[TaskSubmit] L%d Agent 无 parent_task_id（纵深防御拦截）",
                parent_agent_level,
            )
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

        # 存储 session_id（供任务树 API 按会话过滤使用）
        session_id = inputs.get("session_id")
        if session_id:
            metadata["session_id"] = session_id

        # 记录提交者层级（供权限校验：每个 Agent 只能管理自己提交的任务）
        parent_agent_level = inputs.get("parent_agent_level")
        if parent_agent_level:
            metadata["submitted_by_level"] = parent_agent_level

        # 存储执行相关参数
        # BUG-FIX-fix_20260422_workspace_nesting: 子任务不存储 LLM 传递的 workspace，
        # 子任务的 workspace 由祖先链自动解析，存储会导致路径双重嵌套
        # 例外：inherit_workspace_from 解析的 workspace 必须存储，因为这是显式指定的
        if inputs.get("workspace") and (not inputs.get("parent_task_id") or inputs.get("inherit_workspace_from")):
            metadata["workspace"] = inputs["workspace"]
        if inputs.get("max_retries"):
            metadata["max_retries"] = inputs["max_retries"]
        if inputs.get("needs_preparation"):
            metadata["needs_preparation"] = inputs["needs_preparation"]
        if inputs.get("task_scope"):
            metadata["task_scope"] = inputs["task_scope"]
        if inputs.get("isolation_level"):
            metadata["isolation_level"] = inputs["isolation_level"]

        # 存储执行者信息
        if inputs.get("target_id"):
            metadata["target_id"] = inputs["target_id"]

        # BUG-FIX-fix_20260526_missing_user_id:
        # task_notifier 通过 task.metadata["user_id"] 推送状态变更，
        # 不存 user_id 导致所有状态推送静默失败。
        if inputs.get("user_id"):
            metadata["user_id"] = inputs["user_id"]

        # 存储任务角色标记（用于容器完成条件判断）
        # 可选值：solution_planning、final_validation
        if inputs.get("task_role"):
            metadata["task_role"] = inputs["task_role"]

        return metadata

    def _validate_target_agent(
        self,
        target_id: str,
        parent_agent_level: int,
    ) -> tuple[bool, str, str]:
        """校验目标 Agent 是否存在且级别匹配。

        优先从 agent_registry（内存）查找，与 TaskWorker 使用同一数据源，
        确保校验通过时 TaskWorker 也一定能找到该 Agent。
        如果 registry 不可用，回退到磁盘 YAML 文件查找。

        规则：
        1. target_id 对应的 agent 配置必须存在于 registry 或磁盘
        2. 目标 agent 不能是 L1 级别（L1 是主调度层，不能作为子任务执行者）
        3. 目标 agent 的级别不能与提交者同级或更高（应向下委托）

        Args:
            target_id: 目标 Agent ID
            parent_agent_level: 提交者（父任务）的 agent 级别（1=L1, 2=L2, 3=L3）

        Returns:
            (通过, 错误信息, 错误码) 元组。通过时错误信息为空字符串。
        """
        agent_level_str = ""
        agent_level = 0

        agent_config = self._get_agent_config_from_registry(target_id)

        if agent_config is not None:
            level_value = agent_config.level.value if hasattr(agent_config.level, "value") else str(agent_config.level)
            level_map = {"L1": 1, "L2": 2, "L3": 3}
            agent_level = level_map.get(level_value, 0)
            agent_level_str = level_value
        else:
            logger.warning(
                "[TaskSubmit] Agent '%s' 未在 registry 中找到，回退到磁盘文件查找",
                target_id,
            )
            found, agent_level_str, agent_level = self._lookup_agent_from_disk(target_id)
            if not found:
                return (
                    False,
                    f"目标 Agent '{target_id}' 不存在。"
                    f"请检查 target_id 是否正确。如果系统提供了 Agent 映射表，请使用映射表中的 Agent ID。",
                    "TARGET_AGENT_NOT_FOUND",
                )

        if agent_level == 1:
            return (
                False,
                f"不能将任务提交给 L1 Agent（'{target_id}'）。"
                f"L1 是主调度层，只负责接收用户请求和派发任务，不执行具体工作。"
                f"请选择 L2 编排层或 L3 执行层的 Agent。",
                "TARGET_AGENT_IS_L1",
            )

        if agent_level > 0 and agent_level <= parent_agent_level:
            return (
                False,
                f"目标 Agent '{target_id}' 的级别为 {agent_level_str}，"
                f"不能作为 L{parent_agent_level} Agent 的下级执行者。"
                f"任务委托应向下流动：L1→L2→L3，请选择级别更低（L{parent_agent_level + 1}+）的 Agent。",
                "TARGET_AGENT_LEVEL_INVALID",
            )

        return (True, "", "")

    def _get_agent_config_from_registry(self, target_id: str) -> Any | None:
        """从 agent_registry 查找 Agent 配置。

        与 TaskWorker 使用完全相同的数据源，确保校验和执行的一致性。

        Args:
            target_id: 目标 Agent ID

        Returns:
            AgentConfig 实例，未找到返回 None
        """
        try:
            from infrastructure.service_provider import get_service_provider
            provider = get_service_provider()
            agent_registry = provider.get("agent_registry")
            if agent_registry is not None:
                return agent_registry.get(target_id)
        except Exception:
            pass
        return None

    @staticmethod
    def _lookup_agent_from_disk(target_id: str) -> tuple[bool, str, int]:
        """从磁盘 YAML 文件查找 Agent 配置（回退方案）。

        当 agent_registry 中找不到目标 Agent 时，尝试从磁盘文件查找。

        Args:
            target_id: 目标 Agent ID

        Returns:
            (是否找到, 级别字符串, 级别数字) 元组
        """
        import yaml
        from pathlib import Path

        _project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
        config_dir = _project_root / "config" / "agents"

        yaml_path = None
        for p in config_dir.rglob(f"{target_id}.yaml"):
            yaml_path = p
            break

        if not yaml_path or not yaml_path.exists():
            for p in config_dir.rglob("*.yaml"):
                try:
                    with open(p, encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    if data.get("config_id", "") == target_id:
                        yaml_path = p
                        break
                except Exception:
                    continue

        if not yaml_path or not yaml_path.exists():
            return (False, "", 0)

        try:
            with open(yaml_path, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            return (False, "", 0)

        agent_level_str = config.get("level", "")
        level_map = {"L1": 1, "L2": 2, "L3": 3}
        agent_level = level_map.get(agent_level_str, 0)
        return (True, agent_level_str, agent_level)

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
        # task_submit.py 位于 src/tools/builtin/task_submit/，向上 5 层即为项目根目录。
        _project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
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
            for p in config_dir.rglob("*.yaml"):
                try:
                    with open(p, encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    if data.get("config_id", "") == target_id:
                        yaml_path = p
                        break
                except Exception:
                    continue

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
