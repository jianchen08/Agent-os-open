"""
任务提交工具

提供将任务提交给 Agent 或工作流执行的功能
工具只是触发器，核心逻辑在 src/tasks/services/submission_service.py

事件驱动改造：
- 任务提交后发布 task.submitted 事件
- 由 TaskSchedulerService 订阅事件并触发执行
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.level_controller import LevelController
from src.core.results import ToolExecutionResult
from src.db.models import AgentConfig
from src.evaluation.metric_loader import get_metric_loader
from src.tasks.services.submission_service import TaskSubmissionService
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class TaskSubmitTool:
    """
    任务提交工具

    工具只是触发器，核心逻辑委托给 TaskSubmissionService

    事件驱动改造：
    - 任务提交后发布 task.submitted 事件
    - 由 TaskSchedulerService 订阅事件并触发执行
    """

    def __init__(self, session: AsyncSession):
        """
        初始化任务提交工具

        Args:
            session: 数据库会话

        Note:
            任务提交后通过事件驱动执行：
            1. TaskSubmitTool 发布 task.submitted 事件
            2. TaskSchedulerService 订阅事件并检查依赖
            3. 满足条件后发布 task.execution_requested 事件
            4. TaskExecutor 订阅事件并执行任务
        """
        self.session = session
        self.submission_service = TaskSubmissionService(session)
        self.level_controller = LevelController()
        self.metric_loader = get_metric_loader()

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义（标准 OpenAI Function Calling 格式）"""
        from src.tools.types import ToolLevel

        return Tool(
            name="task_submit",
            description="""
将任务提交给 Agent 或工作流执行。仅限 L1/L2 Agent 使用，L3 Agent 不能使用此工具。

使用场景：
- L1 Agent 需要分解任务并分配给 L2 Agent 时
- L2 Agent 需要进一步分解任务给 L3 Agent 时
- 需要创建工作流任务时
- 需要指定任务依赖关系时
- 需要为任务配置验收标准时

重要限制：
- L3 Agent 不能使用此工具（只能执行任务，不能分配任务）
- goal 必须包含 title 字段
- L2 Agent 提交任务时必须指定 parent_task_id
- acceptance_criteria 是必填字段，用于任务评估
- 依赖任务完成后，当前任务才会开始执行

示例：
1. L1 提交主任务给 Agent:
   {"target_type": "agent", "target_id": "lingxi", "goal": {"title": "实现用户登录功能"}, "acceptance_criteria": {"file_check": {"input_params": {"path": "src/auth/login.py", "check": "exists"}}}}

2. L2 提交带依赖的子任务:
   {"target_type": "agent", "target_id": "backend_agent", "goal": {"title": "创建用户模型"}, "parent_task_id": "task-001", "dependencies": ["task-model-design"], "acceptance_criteria": {"file_check": {"input_params": {"path": "src/models/user.py", "check": "exists"}}}}
""".strip(),
            input_schema={
                "type": "object",
                "properties": {
                    "target_type": {
                        "type": "string",
                        "enum": ["agent", "workflow"],
                        "description": "目标类型：agent（提交给 Agent 执行）或 workflow（提交给工作流执行）",
                    },
                    "target_id": {
                        "type": "string",
                        "description": "目标 ID 或名称。对于 agent 类型，可以是 Agent 名称（如 'lingxi'）或 ID；对于 workflow 类型，是工作流标识符",
                    },
                    "goal": {
                        "type": "object",
                        "description": "任务目标对象，必须包含 title 字段",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "任务标题，简短明确，例如：'创建测试文件'、'实现用户登录功能'",
                            },
                            "description": {
                                "type": "string",
                                "description": "详细描述，补充说明任务的具体要求和预期结果（可选）",
                            },
                            "document": {
                                "type": "string",
                                "description": "任务文档路径或内容，提供额外的任务背景信息（可选）",
                            },
                            "context": {
                                "type": "object",
                                "description": "上下文信息，传递给执行 Agent 的额外数据（可选）",
                            },
                        },
                        "required": ["title"],
                    },
                    "evaluation_metric_ids": {
                        "type": "array",
                        "description": "评估指标 ID 列表（兼容旧版，推荐使用 acceptance_criteria 替代）",
                        "items": {"type": "string"},
                    },
                    "acceptance_criteria": {
                        "type": "object",
                        "description": "验收标准字典（必填）。键为评估指标ID或名称，值为该指标的配置对象。配置对象包含：input_params（传递给评估工具的参数）、pass_threshold（任务级别的通过阈值 0-100，可选）",
                        "additionalProperties": {
                            "type": "object",
                            "description": "评估指标的配置对象",
                            "properties": {
                                "input_params": {
                                    "type": "object",
                                    "description": "传递给评估工具的参数，根据指标的 input_schema 定义",
                                },
                                "pass_threshold": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 100,
                                    "description": "任务级别的通过阈值（0-100），优先级高于指标默认阈值",
                                },
                            },
                        },
                    },
                    "execution_mode": {
                        "type": "string",
                        "enum": ["immediate", "queued", "scheduled"],
                        "description": "执行模式：immediate（立即执行）、queued（排队等待）、scheduled（计划执行）",
                        "default": "immediate",
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
                    "user_id": {
                        "type": "string",
                        "description": "提交任务的用户 ID（可选）",
                    },
                    "parent_task_id": {
                        "type": "string",
                        "description": "父任务 ID。L2 Agent 提交子任务时必须指定，用于建立任务层级关系",
                    },
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "依赖的任务 ID 列表（可选）。当前任务会在所有依赖任务完成后才开始执行",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "元数据，存储额外的任务信息（可选）",
                    },
                    "needs_preparation": {
                        "type": "boolean",
                        "description": "是否需要准备阶段（调研、分解、规划）。true 表示需要准备（L2 会先提交准备任务给 L3），false 或不指定表示直接提交执行任务。建议复杂任务（涉及多个子功能、需要技术选型）设为 true",
                    },
                },
                "required": ["target_type", "target_id", "goal", "acceptance_criteria"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.L1_L2_ONLY,
            requires_approval=False,
            dangerous_operations=[],
            tags=["task", "submit", "L1", "L2"],
            # 注入参数：这些参数由系统在运行时注入，不暴露给 LLM 决策
            injected_params=["user_id", "session_id", "task_id"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行工具"""
        task_scope = inputs.get("task_scope", "short_term")  # 默认短期任务
        target_type = inputs.get("target_type")
        target_id = inputs.get("target_id")
        goal = inputs.get("goal")
        evaluation_metric_ids = inputs.get("evaluation_metric_ids", [])
        acceptance_criteria = inputs.get("acceptance_criteria", {})
        parent_agent_level = inputs.get("parent_agent_level", 1)  # 默认 L1

        logger.info(
            f"[TaskSubmit] 开始执行 | "
            f"task_scope={task_scope} | "
            f"target_type={target_type} | "
            f"target_id={target_id} | "
            f"parent_agent_level=L{parent_agent_level}"
        )
        logger.debug(
            f"[TaskSubmit] 任务详情 | "
            f"title={goal.get('title', 'N/A') if goal else 'N/A'} | "
            f"metric_count={len(evaluation_metric_ids) + len(acceptance_criteria)}"
        )

        # 参数验证
        if not target_type:
            logger.error("[TaskSubmit] 参数验证失败 | target_type 为空")
            return create_failure_result(
                error="目标类型不能为空", error_code="MISSING_TARGET_TYPE"
            )
        if not target_id:
            logger.error("[TaskSubmit] 参数验证失败 | target_id 为空")
            return create_failure_result(
                error="目标 ID 不能为空", error_code="MISSING_TARGET_ID"
            )
        if not goal or not goal.get("title"):
            logger.error("[TaskSubmit] 参数验证失败 | goal 或 goal.title 为空")
            return create_failure_result(
                error="任务目标不能为空", error_code="MISSING_GOAL"
            )
        if not acceptance_criteria:
            logger.error("[TaskSubmit] 参数验证失败 | 未提供评估指标")
            return create_failure_result(
                error="必须提供 acceptance_criteria",
                error_code="MISSING_METRICS",
            )

        # 层级权限验证：L1 和 L2 都能使用
        # L1：可以提交主任务（parent_task_id=None）或子任务
        # L2：只能提交子任务（必须指定 parent_task_id）
        if parent_agent_level == 2:
            parent_task_id = inputs.get("parent_task_id")
            if not parent_task_id:
                logger.warning("[TaskSubmit] 权限验证失败 | L2 必须指定 parent_task_id")
                return create_failure_result(
                    error="L2 Agent 提交任务时必须指定 parent_task_id",
                    error_code="MISSING_PARENT_TASK",
                )
            logger.info(
                f"[TaskSubmit] L2 权限验证通过 | parent_task_id={parent_task_id}"
            )
        elif parent_agent_level not in [1, 2]:
            logger.warning(
                f"[TaskSubmit] 权限验证失败 | "
                f"当前层级=L{parent_agent_level}，只支持 L1 和 L2"
            )
            return create_failure_result(
                error=f"只有L1和L2 Agent能使用task_submit工具，当前层级：L{parent_agent_level}",
                error_code="INSUFFICIENT_PERMISSION",
            )

        logger.debug("[TaskSubmit] 参数验证通过")

        # 依赖任务存在性验证
        dependencies = inputs.get("dependencies", [])
        if dependencies:
            missing_ids = await self._check_dependencies_exist(dependencies)
            if missing_ids:
                logger.error(
                    f"[TaskSubmit] 依赖验证失败 | 不存在的任务: {missing_ids}"
                )
                return create_failure_result(
                    error=f"依赖任务不存在: {missing_ids}",
                    error_code="DEPENDENCY_NOT_FOUND",
                )
            logger.info(
                f"[TaskSubmit] 依赖验证通过 | dependencies={dependencies}"
            )

        # 处理 acceptance_criteria
        # acceptance_criteria 可以是：
        # 1. 数据库中的评估指标（会被解析为 UUID）
        # 2. 自定义验证条件（保留原始 key，不解析）
        #
        # 最终转换为数组格式，避免 TaskManager 重复解析
        acceptance_criteria_list = []

        for metric_ref, config in acceptance_criteria.items():
            # 兼容新旧格式
            # 新格式：{"input_params": {...}, "expected_output": {...}, "pass_threshold": 85}
            # 旧格式：直接是 input_params: {"path": "..."}
            if isinstance(config, dict) and "input_params" in config:
                input_params = config.get("input_params", {})
                expected_output = config.get("expected_output")  # 新增：获取 expected_output
                pass_threshold = config.get("pass_threshold")
            else:
                input_params = config
                expected_output = None
                pass_threshold = None

            # 尝试按 ID 查找指标
            metric = await self.metric_loader.get_metric(metric_ref)
            if metric:
                # 验证 input_params 是否符合 input_schema
                validation_errors = self._validate_input_params(metric, input_params)
                if validation_errors:
                    logger.error(
                        f"[TaskSubmit] 参数验证失败 | metric={metric_ref} | errors={validation_errors}"
                    )
                    return create_failure_result(
                        error=f"评估指标 {metric_ref} 参数验证失败: {validation_errors}",
                        error_code="INVALID_INPUT_PARAMS",
                    )

                acceptance_criteria_list.append({
                    "metric_id": metric["id"],
                    "metric_name": metric.get("name", metric_ref),
                    "input_params": input_params,
                    "expected_output": expected_output,  # 新增：存储 expected_output
                    "pass_threshold": pass_threshold,
                    "status": "pending",
                    "retry_count": 0,
                    "evaluated_at": None,
                    "evaluation_result": None,
                    "is_custom": False,
                })
                logger.debug(
                    f"[TaskSubmit] 找到指标（按ID）: {metric_ref} -> {metric['id']}"
                )
                continue

            # 尝试按名称查找指标
            metric = await self.metric_loader.get_metric_by_name(metric_ref)
            if metric:
                # 验证 input_params 是否符合 input_schema
                validation_errors = self._validate_input_params(metric, input_params)
                if validation_errors:
                    logger.error(
                        f"[TaskSubmit] 参数验证失败 | metric={metric_ref} | errors={validation_errors}"
                    )
                    return create_failure_result(
                        error=f"评估指标 {metric_ref} 参数验证失败: {validation_errors}",
                        error_code="INVALID_INPUT_PARAMS",
                    )

                acceptance_criteria_list.append({
                    "metric_id": metric["id"],
                    "metric_name": metric.get("name", metric_ref),
                    "input_params": input_params,
                    "expected_output": expected_output,  # 新增：存储 expected_output
                    "pass_threshold": pass_threshold,
                    "status": "pending",
                    "retry_count": 0,
                    "evaluated_at": None,
                    "evaluation_result": None,
                    "is_custom": False,
                })
                logger.debug(
                    f"[TaskSubmit] 找到指标（按名称）: {metric_ref} -> {metric['id']}"
                )
            else:
                # 未找到数据库指标，作为自定义验证条件保留
                acceptance_criteria_list.append({
                    "metric_id": metric_ref,
                    "metric_name": metric_ref,
                    "input_params": input_params,
                    "expected_output": expected_output,  # 新增：存储 expected_output
                    "pass_threshold": pass_threshold,
                    "status": "pending",
                    "retry_count": 0,
                    "evaluated_at": None,
                    "evaluation_result": None,
                    "is_custom": True,
                })
                logger.info(f"[TaskSubmit] 使用自定义验证条件: {metric_ref}")

        # 更新 inputs，使用数组格式
        inputs["acceptance_criteria"] = acceptance_criteria_list
        # 同时设置 evaluation_metric_ids 供 TaskManager 使用
        inputs["evaluation_metric_ids"] = [
            ac["metric_id"] for ac in acceptance_criteria_list if not ac.get("is_custom")
        ]

        logger.info(
            f"[TaskSubmit] 解析后的验收标准 | "
            f"total_count={len(acceptance_criteria_list)} | "
            f"database_metrics={len(inputs['evaluation_metric_ids'])} | "
            f"custom_criteria={len(acceptance_criteria_list) - len(inputs['evaluation_metric_ids'])}"
        )

        if target_type == "agent":
            return await self._submit_to_agent(inputs, parent_agent_level)
        if target_type == "workflow":
            return await self._submit_to_workflow(inputs, parent_agent_level)

        logger.error(f"[TaskSubmit] 不支持的目标类型 | target_type={target_type}")
        return create_failure_result(
            error=f"不支持的目标类型: {target_type}", error_code="INVALID_TYPE"
        )

    async def _submit_to_agent(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """提交任务给 Agent

        Args:
            inputs: 输入参数
            parent_agent_level: 调用者的层级（1 或 2）
        """
        target_id = inputs.get("target_id")

        logger.info(
            f"[TaskSubmit._submit_to_agent] 提交任务给 Agent | target_id={target_id}"
        )

        try:
            # 验证 Agent 存在（优先使用名称查找，对 LLM 更友好）
            logger.debug(
                f"[TaskSubmit._submit_to_agent] 验证 Agent | target_id={target_id}"
            )

            # 优先尝试通过 config_id 查找（最常用，如 "lingxi"、"general_agent"）
            result = await self.session.execute(
                select(AgentConfig).where(AgentConfig.config_id == target_id).limit(1)
            )
            agent = result.scalar_one_or_none()

            # 如果通过 config_id 未找到，再尝试通过 name 查找（如 "灵汐"、"通用任务执行者"）
            if not agent:
                result = await self.session.execute(
                    select(AgentConfig).where(AgentConfig.name == target_id).limit(1)
                )
                agent = result.scalar_one_or_none()
                if agent:
                    logger.info(
                        f"[TaskSubmit._submit_to_agent] 通过 name 找到 Agent | "
                        f"name={target_id} -> config_id={agent.config_id}"
                    )

            # 最后尝试通过 ID 查找（兼容 UUID）
            if not agent:
                result = await self.session.execute(
                    select(AgentConfig).where(AgentConfig.id == target_id).limit(1)
                )
                agent = result.scalar_one_or_none()
                if agent:
                    logger.info(
                        f"[TaskSubmit._submit_to_agent] 通过 ID 找到 Agent | "
                        f"id={target_id} -> config_id={agent.config_id}"
                    )

            if not agent:
                logger.error(
                    f"[TaskSubmit._submit_to_agent] Agent 不存在 | target_id={target_id}"
                )
                available_agents = await self._get_available_agents(parent_agent_level)
                agents_list = "\n".join(
                    [f"  - {a['config_id']} (L{a['level']} - {a['name']})" for a in available_agents[:5]]
                )
                error_msg = (
                    f"Agent '{target_id}' 不存在。\n\n"
                    f"【可用的 Agent】\n{agents_list}"
                )
                return create_failure_result(
                    error=error_msg, error_code="AGENT_NOT_FOUND"
                )
            if not agent.is_active:
                logger.warning(
                    f"[TaskSubmit._submit_to_agent] Agent 未激活 | "
                    f"target_id={target_id} | name={agent.name}"
                )
                return create_failure_result(
                    error=f"Agent 未激活: {target_id}", error_code="AGENT_INACTIVE"
                )

            logger.info(
                f"[TaskSubmit._submit_to_agent] Agent 验证通过 | "
                f"name={agent.name} | "
                f"is_active={agent.is_active}"
            )

            # 记录 Agent 调用
            from src.services.agent_call_recorder import get_agent_call_recorder

            recorder = get_agent_call_recorder(self.session)
            goal = inputs.get("goal", {})
            instruction = goal.get("description", goal.get("title", ""))

            logger.debug(
                f"[TaskSubmit._submit_to_agent] 记录 Agent 调用 | "
                f"target_agent={agent.name} | "
                f"instruction={instruction[:100]}..."
            )

            execution_id = await recorder.start_call(
                caller_level=f"L{parent_agent_level}",
                target_agent_id=target_id,
                target_agent_name=agent.name,
                operation_type="task_submit",
                instruction=instruction,
                instruction_summary=goal.get("title", "")[:100],
                context={
                    "task_scope": inputs.get("task_scope"),
                    "evaluation_metric_count": len(
                        inputs.get("acceptance_criteria", {})
                    ),
                    "parent_task_id": inputs.get("parent_task_id"),
                },
                timeout=inputs.get("timeout", 300),
                priority=inputs.get("priority", "normal"),
            )

            logger.debug(
                f"[TaskSubmit._submit_to_agent] Agent 调用记录已创建 | "
                f"execution_id={execution_id}"
            )

            # 更新为运行中
            await recorder.update_running(execution_id)
            logger.debug(
                f"[TaskSubmit._submit_to_agent] Agent 调用状态更新为运行中 | "
                f"execution_id={execution_id}"
            )

            # 直接使用工具执行记录ID（由 execute_tools.py 创建）
            execution_record_id = inputs.get("tool_record_id") or inputs.get("metadata", {}).get("tool_record_id")

            # 降级方案：如果注入失败，生成新 ID
            if not execution_record_id:
                execution_record_id = f"exec-{uuid.uuid4().hex[:8]}"
                logger.warning(
                    f"[TaskSubmit._submit_to_agent] 注入的 tool_record_id 缺失，已生成新 ID: {execution_record_id}"
                )

            # 使用 TaskSubmissionService 提交任务
            logger.info(
                f"[TaskSubmit._submit_to_agent] 提交任务 | "
                f"agent={agent.name} | "
                f"task_type={inputs.get('task_scope', 'short_term')}"
            )

            submit_result = await self.submission_service.submit(
                goal=inputs.get("goal"),
                acceptance_criteria=inputs.get("acceptance_criteria"),
                target_type="agent",
                target_id=target_id,
                target_name=agent.name,
                user_id=inputs.get("user_id"),
                parent_task_id=inputs.get("parent_task_id"),
                session_id=inputs.get("session_id"),
                task_type=inputs.get("task_scope", "short_term"),
                priority=inputs.get("priority", 5),
                max_retries=inputs.get("max_retries", 3),
                metadata={
                    **inputs.get("metadata", {}),
                    "task_scope": inputs.get("task_scope"),
                    "execution_mode": inputs.get("execution_mode", "immediate"),
                    "agent_call_execution_id": execution_id,
                    "needs_preparation": inputs.get("needs_preparation"),
                    "use_todo": inputs.get("use_todo"),
                    "execution_record_id": execution_record_id,
                },
                dependencies=inputs.get("dependencies"),
            )

            logger.info(
                f"[TaskSubmit._submit_to_agent] 任务已提交 | "
                f"task_id={submit_result.get('task_id')} | "
                f"agent={agent.name}"
            )

            # 注意：事件发布由 TaskManager 统一处理，这里不再重复发布

            submit_result["execution_started"] = True

            # 记录调用完成
            await recorder.complete_call(
                execution_id=execution_id,
                success=True,
                result={"task_id": submit_result["task_id"]},
                result_summary=f"任务已提交: {submit_result['task_id']}",
            )

            logger.info(
                f"[TaskSubmit._submit_to_agent] 任务提交成功 | "
                f"task_id={submit_result.get('task_id')} | "
                f"agent={agent.name}"
            )

            # 构建返回数据（事件驱动改造：移除 execution_result 相关代码）
            result_data = {
                **submit_result,
                "target_name": agent.name,
            }

            return create_success_result(
                data=result_data,
                metadata={"action": "submit_to_agent"},
            )

        except Exception as e:
            # 记录调用失败
            if "execution_id" in locals():
                try:
                    await recorder.fail_call(
                        execution_id=execution_id,
                        error=str(e),
                    )
                except Exception:
                    pass  # 忽略记录失败的错误

            return create_failure_result(
                error=f"提交失败: {str(e)}", error_code="SUBMIT_FAILED"
            )

    async def _submit_to_workflow(
        self, inputs: dict[str, Any], parent_agent_level: int
    ) -> ToolExecutionResult:
        """提交任务给工作流

        Args:
            inputs: 输入参数
            parent_agent_level: 调用者的层级（1 或 2）
        """
        try:
            target_id = inputs.get("target_id")

            # 直接使用工具执行记录ID（由 execute_tools.py 创建）
            execution_record_id = inputs.get("tool_record_id") or inputs.get("metadata", {}).get("tool_record_id")

            # 降级方案：如果注入失败，生成新 ID
            if not execution_record_id:
                execution_record_id = f"exec-{uuid.uuid4().hex[:8]}"
                logger.warning(
                    f"[TaskSubmit._submit_to_workflow] 注入的 tool_record_id 缺失，已生成新 ID: {execution_record_id}"
                )

            # 使用 TaskSubmissionService 提交任务
            submit_result = await self.submission_service.submit(
                goal=inputs.get("goal"),
                acceptance_criteria=inputs.get("acceptance_criteria"),
                target_type="workflow",
                target_id=target_id,
                target_name=None,
                user_id=inputs.get("user_id"),
                parent_task_id=inputs.get("parent_task_id"),
                session_id=inputs.get("session_id"),
                task_type=inputs.get("task_scope", "short_term"),
                priority=inputs.get("priority", 5),
                max_retries=inputs.get("max_retries", 3),
                metadata={
                    **inputs.get("metadata", {}),
                    "task_scope": inputs.get("task_scope"),
                    "execution_mode": inputs.get("execution_mode", "immediate"),
                    "needs_preparation": inputs.get("needs_preparation"),
                    "use_todo": inputs.get("use_todo"),
                    "execution_record_id": execution_record_id,
                },
                dependencies=inputs.get("dependencies"),
            )

            # 注意：事件发布由 TaskSubmissionService 统一处理，这里不再重复发布

            submit_result["execution_started"] = True

            # 构建返回数据
            result_data = {**submit_result}

            return create_success_result(
                data=result_data,
                metadata={"action": "submit_to_workflow"},
            )

        except Exception as e:
            return create_failure_result(
                error=f"提交失败: {str(e)}", error_code="SUBMIT_FAILED"
            )

    async def _get_available_agents(self, parent_agent_level: int = 1) -> list:
        """获取可用的 Agent 列表（根据调用者层级过滤）

        Args:
            parent_agent_level: 调用者层级（1 或 2）

        Returns:
            可提交的目标 Agent 列表
        """
        try:
            allowed_targets = self.level_controller.get_allowed_targets(parent_agent_level)
            if not allowed_targets:
                return []

            result = await self.session.execute(
                select(AgentConfig).where(
                    AgentConfig.is_active,
                    AgentConfig.level.in_(allowed_targets)
                ).limit(10)
            )
            agents = result.scalars().all()
            return [{"config_id": a.config_id, "name": a.name, "level": a.level} for a in agents]
        except Exception:
            return []

    async def _check_dependencies_exist(self, dependencies: list[str]) -> list[str]:
        """
        检查依赖任务是否存在

        FEATURE-DEP-VALIDATION: 依赖任务存在性验证
        设计决策:
          - 批量查询避免 N+1 问题
          - 返回不存在的任务 ID 列表

        Args:
            dependencies: 依赖任务 ID 列表

        Returns:
            不存在的任务 ID 列表（空列表表示全部存在）
        """
        from src.db.models import Task

        if not dependencies:
            return []

        result = await self.session.execute(
            select(Task.id).where(Task.id.in_(dependencies))
        )
        existing_ids = {row[0] for row in result.fetchall()}
        missing_ids = list(set(dependencies) - existing_ids)

        return missing_ids

    def _validate_input_params(
        self,
        metric: dict[str, Any],
        input_params: dict[str, Any],
    ) -> list[str]:
        """
        验证输入参数是否符合 input_schema

        Args:
            metric: 评估指标配置
            input_params: 输入参数

        Returns:
            错误消息列表（空列表表示验证通过）
        """
        errors = []
        schema = metric.get("input_schema", {})
        required = schema.get("required", [])

        # 检查必填参数
        for field in required:
            if field not in input_params:
                errors.append(f"缺少必填参数: {field}")

        return errors
