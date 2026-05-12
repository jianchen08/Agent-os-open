"""
任务提交工具

提供将任务提交给 Agent 或工作流执行的功能
工具只是触发器，核心逻辑委托给 TaskSubmitOrchestrator

事件驱动改造：
- 任务提交后发布 task.submitted 事件
- 由 TaskSchedulerService 订阅事件并触发执行
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.level_controller import LevelController
from src.core.results import ToolExecutionResult
from src.tasks.services.submission import TaskSubmissionService, TaskSubmitOrchestrator
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class TaskSubmitTool:
    """
    任务提交工具

    工具只是触发器，核心逻辑委托给 TaskSubmitOrchestrator

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
        self.orchestrator = TaskSubmitOrchestrator(
            session=session,
            submission_service=self.submission_service,
            level_controller=self.level_controller,
        )

    @staticmethod
    def get_tool_definition() -> Tool:
        """获取工具定义（标准 OpenAI Function Calling 格式）"""
        return Tool(
            name="task_submit",
            description="""
任务提交工具。将任务提交给指定的 Agent 或工作流执行。

【核心功能】
- 将复杂任务分配给合适的 Agent 或工作流执行
- 为任务配置验收标准，确保执行结果可验证
- 支持立即执行、排队等待、计划执行三种模式

【参数说明】
- target_type: 目标类型（agent 或 workflow）
- target_id: 目标 ID（Agent 名称或工作流 ID）
- goal: 任务目标（Agent 任务必填）
- acceptance_criteria: 验收标准（必填）

【示例】
{"target_type": "agent", "target_id": "general_assistant", "goal": {"title": "实现用户登录"}, "acceptance_criteria": {"file_check": {"input_params": {"path": "src/auth/login.py"}}}}
""".strip(),
            input_schema={
                "type": "object",
                "properties": {
                    "target_type": {
                        "type": "string",
                        "enum": ["agent", "workflow"],
                        "description": """
目标类型（必填）。

【可选值】
- agent: 将任务提交给 Agent 执行
- workflow: 将任务提交给工作流执行

【选择依据】
- 如果有合适的 Agent 处理该任务，使用 agent
- 如果需要按预定义流程执行，使用 workflow
""".strip(),
                    },
                    "target_id": {
                        "type": "string",
                        "description": """
目标 ID（必填）。

【Agent 任务】
- 可以是 Agent 名称（如 'general_assistant'）或 config_id
- 使用 resource_search(resource_type="agent", query="关键词") 搜索合适的 Agent

【Workflow 任务】
- 工作流的标识符（workflow id）
- 使用 resource_search(resource_type="workflow", query="关键词") 搜索合适的工作流
""".strip(),
                    },
                    "goal": {
                        "type": "object",
                        "description": """
任务目标对象（仅 Agent 任务必填）。用于描述要执行的任务。

【包含字段】
- title: 任务标题（必填），简短明确，例如：'创建用户登录 API'
- description: 详细描述（可选），补充说明任务的具体要求、验收标准、注意事项等
- document: 任务文档（可选），可以是文档路径或内联内容，提供额外背景信息
- context: 上下文数据（可选），传递给执行 Agent 的额外数据，如配置、变量等

【示例】
{"title": "创建用户登录 API", "description": "实现用户登录功能，返回 JWT token", "context": {"db_config": "mysql://localhost"}}
""".strip(),
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
                    "workflow_inputs": {
                        "type": "object",
                        "description": """
工作流输入参数（仅 Workflow 任务必填）。参数名和类型需匹配工作流定义的 inputs schema。

【查找工作流输入定义】
- 使用 resource_search 工具搜索工作流：resource_search(resource_type="workflow", query="工作流名称")
- 使用 file_read 工具查询工作流定义：file_read(path="config/workflows/xxx.yaml", fields=["id", "inputs"])

【示例】
{"initial_number": 10, "expected_result": 22}
""".strip(),
                        "additionalProperties": True,
                    },
                    "description": {
                        "type": "string",
                        "description": "任务描述（可选，用于日志/审计）",
                    },
                    "acceptance_criteria": {
                        "type": "object",
                        "description": """
验收标准字典（必填）。用于定义任务完成后需要验证的条件。

【常用评估指标（直接使用）】
- file_check: 验证文件存在。参数：path、check（exists/not_empty/contains/is_directory）
- format_valid: 验证文件格式。参数：path、format（json/yaml/schema）
- command_check: 验证命令执行。参数：command、check（success/time）、timeout
- quality_check: 验证代码质量。参数：path、type（code/doc）、criteria
- semantic_check: 验证语义正确。参数：output、check（intent/match/hallucination）
- human_review: 需要人工审核。参数：title、type（approval/review）

【其他评估指标查找步骤】
1. 搜索评估指标文件：
   enhanced_search(query="关键词", path="config/evaluation_metrics/", file_pattern="*.yaml")
   → 返回匹配的文件名（如 xxx_check.yaml）

2. 查询评估指标的具体输入参数：
   file_read(path="config/evaluation_metrics/xxx.yaml", fields=["id", "expected_input"])
   → 返回 expected_input 字段，包含该指标需要的所有输入参数定义
   → 根据返回的 params 填写 input_params

【配置对象说明】
- input_params: 传递给评估工具的参数（必填），根据 expected_input 中的 params 填写
- expected_output: 预期输出，用于验证（可选）
- pass_threshold: 通过阈值 0-100（可选）

【示例】
{"file_check": {"input_params": {"path": "src/main.py"}}, "format_valid": {"input_params": {"path": "src/main.py", "format": "schema"}}}
""".strip(),
                        "additionalProperties": {
                            "type": "object",
                            "description": "评估指标的配置对象",
                            "properties": {
                                "input_params": {
                                    "type": "object",
                                    "description": '传递给评估工具的参数。例如 file_check 需要 {"path": "...", "check": "exists"}',
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
                            "required": ["input_params"],
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
                        "enum": ["short_term", "long_term"],
                        "default": "short_term",
                        "description": "任务范围：short_term（短期任务）或 long_term（长期任务）",
                    },
                },
                "required": ["target_type", "target_id", "acceptance_criteria"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.L1_L2_ONLY,
            requires_approval=False,
            dangerous_operations=[],
            tags=["task", "submit"],
            injected_params=[
                "user_id",
                "session_id",
                "task_id",
                "parent_task_id",
                "dependencies",
                "tool_record_id",
            ],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行工具"""
        task_scope = inputs.get("task_scope", "short_term")
        target_type = inputs.get("target_type")
        target_id = inputs.get("target_id")
        goal = inputs.get("goal")
        workflow_inputs = inputs.get("workflow_inputs")
        description = inputs.get("description")
        acceptance_criteria = inputs.get("acceptance_criteria", {})

        logger.info(
            f"[TaskSubmit] 开始执行 | "
            f"task_scope={task_scope} | "
            f"target_type={target_type} | "
            f"target_id={target_id}"
        )
        logger.debug(
            f"[TaskSubmit] 任务详情 | "
            f"title={goal.get('title', 'N/A') if goal else 'N/A'} | "
            f"workflow_inputs={workflow_inputs} | "
            f"metric_count={len(acceptance_criteria)}"
        )

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
        if not acceptance_criteria:
            logger.error("[TaskSubmit] 参数验证失败 | 未提供评估指标")
            return create_failure_result(
                error="必须提供 acceptance_criteria",
                error_code="MISSING_METRICS",
            )

        dependencies = inputs.get("dependencies", [])
        if dependencies:
            missing_ids = await self._check_dependencies_exist(dependencies)
            if missing_ids:
                logger.error(f"[TaskSubmit] 依赖验证失败 | 不存在的任务: {missing_ids}")
                return create_failure_result(
                    error=f"依赖任务不存在: {missing_ids}",
                    error_code="DEPENDENCY_NOT_FOUND",
                )
            logger.info(f"[TaskSubmit] 依赖验证通过 | dependencies={dependencies}")

        orchestrator_kwargs = {
            k: v
            for k, v in inputs.items()
            if k
            not in (
                "goal",
                "acceptance_criteria",
                "target_type",
                "target_id",
                "workflow_inputs",
                "description",
            )
        }

        if target_type == "agent":
            if not goal or not goal.get("title"):
                logger.error("[TaskSubmit] 参数验证失败 | Agent 任务必须提供 goal")
                return create_failure_result(
                    error="Agent 任务必须提供 goal",
                    error_code="MISSING_GOAL",
                )
            return await self.orchestrator.submit_to_agent(
                goal=goal,
                acceptance_criteria=acceptance_criteria,
                target_id=target_id,
                description=description,
                **orchestrator_kwargs,
            )
        elif target_type == "workflow":
            if not workflow_inputs:
                logger.error(
                    "[TaskSubmit] 参数验证失败 | Workflow 任务必须提供 workflow_inputs"
                )
                return create_failure_result(
                    error="Workflow 任务必须提供 workflow_inputs",
                    error_code="MISSING_WORKFLOW_INPUTS",
                )
            return await self.orchestrator.submit_to_workflow(
                workflow_inputs=workflow_inputs,
                acceptance_criteria=acceptance_criteria,
                workflow_id=target_id,
                description=description,
                **orchestrator_kwargs,
            )
        else:
            logger.error(f"[TaskSubmit] 不支持的目标类型 | target_type={target_type}")
            return create_failure_result(
                error=f"不支持的目标类型: {target_type}", error_code="INVALID_TYPE"
            )

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
