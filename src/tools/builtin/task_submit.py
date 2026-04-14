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

        获取优先级：
        1. 缓存的实例（已被外部注入）
        2. sys._agent_os_task_service（CLI 设置的全局共享实例）
        3. 创建新实例（降级兜底）

        Returns:
            TaskService 实例，创建失败时返回 None
        """
        if self._task_service is not None:
            return self._task_service
        try:
            import sys
            global_ts = getattr(sys, "_agent_os_task_service", None)
            if global_ts is not None:
                self._task_service = global_ts
                return self._task_service
            from tasks.service import TaskService
            self._task_service = TaskService()
            return self._task_service
        except Exception as e:
            logger.error("[TaskSubmit] TaskService 创建失败: %s", e)
            return None

    def _get_event_bus(self) -> Any:
        """获取共享的 EventBus 实例。

        获取优先级：
        1. 缓存的实例（已被外部注入）
        2. sys._agent_os_event_bus（CLI 设置的全局实例）
        3. 创建新实例（降级兜底）

        Returns:
            EventBus 实例，获取失败时返回 None
        """
        if self._event_bus is not None:
            return self._event_bus
        try:
            from pipeline.event_bus import EventBus
            import sys
            global_bus = getattr(sys, "_agent_os_event_bus", None)
            if global_bus is not None:
                self._event_bus = global_bus
                return self._event_bus
            self._event_bus = EventBus()
            return self._event_bus
        except Exception as e:
            logger.error("[TaskSubmit] EventBus 创建失败: %s", e)
            return None

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
                        "description": "目标类型，固定为 agent。短期任务必填，长期任务不需要",
                    },
                    "target_id": {
                        "type": "string",
                        "description": "目标 Agent ID。短期任务必填，长期任务不需要。可通过 resource_search 查找",
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
验收标准字典。短期任务必填，长期任务不需要。
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
                        "enum": ["short_term", "long_term"],
                        "default": "short_term",
                        "description": "任务范围：short_term（短期任务）或 long_term（长期任务）",
                    },
                    "parent_task_id": {
                        "type": "string",
                        "description": "父任务 ID。为长期任务创建子任务时需要指定此参数，将子任务关联到对应的长期任务。",
                    },
                    "workspace": {
                        "type": "string",
                        "description": """
工作目录。不指定则在 .ai_workspaces/{任务ID}/ 下自动创建。
- 相对路径（如 "my-app"）：在 .ai_workspaces/ 下创建
- 绝对路径（如 "D:/projects/app"）：直接使用该路径，必须是完整路径
- 子任务可指定子目录（如 "src/auth"）继承父任务目录
""".strip(),
                    },
                    "isolation_level": {
                        "type": "string",
                        "enum": ["container", "host"],
                        "default": "container",
                        "description": "隔离级别。container（默认，Docker容器执行）、host（宿主机执行，需人工审批）",
                    },
                },
                "required": ["goal"],
                "allOf": [
                    {
                        "if": {
                            "not": {
                                "required": ["task_scope"],
                                "properties": {"task_scope": {"const": "long_term"}}
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
                            "properties": {"task_scope": {"const": "long_term"}}
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
            requires_approval=False,
            dangerous_operations=[],
            tags=["task", "submit"],
            injected_params=[
                "user_id",
                "session_id",
                "task_id",
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
        task_scope = inputs.get("task_scope", "short_term")
        goal = inputs.get("goal")
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

        # 长期任务走独立分支
        if task_scope == "long_term":
            return await self._execute_long_term(inputs)

        target_type = inputs.get("target_type")
        target_id = inputs.get("target_id")
        description = inputs.get("description") or goal.get("description", "")
        acceptance_criteria = inputs.get("acceptance_criteria", {})
        parent_task_id = inputs.get("parent_task_id")
        workspace = inputs.get("workspace", "")

        logger.info(
            "[TaskSubmit] 短期任务 | target_type=%s | target_id=%s",
            target_type, target_id,
        )
        logger.debug(
            "[TaskSubmit] 任务详情 | title=%s | metric_count=%d",
            goal.get("title", "N/A"), len(acceptance_criteria),
        )

        # ── 2. 短期任务必填参数验证 ──
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
        try:
            task = task_service.create_task(
                title=goal["title"],
                description=description,
                parent_task_id=parent_task_id,
                target_type=target_type,
                dependencies=dependencies or None,
                priority=inputs.get("priority", 5),
                metadata=self._build_metadata(inputs, goal, acceptance_criteria),
            )
        except Exception as e:
            logger.error("[TaskSubmit] 任务创建失败: %s", e)
            return create_failure_result(
                error=f"任务创建失败: {e}",
                error_code="TASK_CREATE_FAILED",
            )

        # ── 7. 发布事件 ──
        event_bus = self._get_event_bus()
        if event_bus is not None:
            try:
                await event_bus.emit("task.submitted", {
                    "task_id": task.id,
                    "target_type": target_type,
                    "target_id": target_id,
                    "user_input": goal.get("title", ""),
                    "description": description or goal.get("description", ""),
                    "acceptance_criteria": acceptance_criteria,
                    "workspace": workspace,
                    "priority": inputs.get("priority", 5),
                })
                logger.info("[TaskSubmit] 事件已发布 | task_id=%s", task.id)
            except Exception as e:
                logger.warning("[TaskSubmit] 事件发布失败（任务已创建）: %s", e)
        else:
            logger.warning("[TaskSubmit] EventBus 不可用，跳过事件发布")

        logger.info("[TaskSubmit] 任务提交成功 | task_id=%s | title=%s", task.id, task.title)

        return create_success_result(
            data={
                "task_id": task.id,
                "title": task.title,
                "status": task.status.value,
                "target_type": target_type,
                "target_id": target_id,
                "submit_status": "submitted",
            },
            metadata={
                "action": "task_submit",
                "task_scope": task_scope,
            },
        )

    async def _execute_long_term(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """处理长期任务提交。

        长期任务不指定执行者，只创建一个 pending 状态的父任务框架。

        Args:
            inputs: 工具输入参数

        Returns:
            工具执行结果
        """
        goal = inputs.get("goal")
        parent_agent_level = inputs.get("parent_agent_level", 1)

        logger.info(
            "[TaskSubmit] 长期任务提交 | title=%s | parent_agent_level=%s",
            goal.get("title") if goal else "N/A", parent_agent_level,
        )

        if parent_agent_level != 1:
            return create_failure_result(
                error="长期任务只能由 L1 Agent 提交",
                error_code="L2_CANNOT_SUBMIT_LONG_TERM",
            )

        task_service = self._get_task_service()
        if task_service is None:
            return create_failure_result(
                error="任务服务不可用，请检查系统配置",
                error_code="SERVICE_UNAVAILABLE",
            )

        try:
            description = inputs.get("description") or goal.get("description", "")
            task = task_service.create_task(
                title=goal["title"],
                description=description,
                metadata=self._build_metadata(inputs, goal, {}),
            )
        except Exception as e:
            logger.error("[TaskSubmit] 长期任务创建失败: %s", e)
            return create_failure_result(
                error=f"长期任务创建失败: {e}",
                error_code="TASK_CREATE_FAILED",
            )

        logger.info("[TaskSubmit] 长期任务提交成功 | task_id=%s | title=%s", task.id, task.title)

        return create_success_result(
            data={
                "task_id": task.id,
                "title": task.title,
                "status": task.status.value,
                "task_scope": "long_term",
                "submit_status": "submitted",
            },
            metadata={"action": "task_submit_long_term"},
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
        if task_scope == "long_term":
            if parent_task_id is not None:
                logger.warning("[TaskSubmit] 长期任务不能有父任务 | parent_task_id=%s", parent_task_id)
                return False
            return True

        if parent_agent_level == 1:
            logger.debug("[TaskSubmit] L1 Agent 可以指定 parent_task_id | value=%s", parent_task_id)
            return True

        if parent_agent_level >= 2:
            if parent_task_id is not None:
                logger.debug(
                    "[TaskSubmit] L2+ Agent parent_task_id 已由系统注入 | level=%s | parent_task_id=%s",
                    parent_agent_level, parent_task_id,
                )
            return True

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
        if acceptance_criteria:
            metadata["acceptance_criteria"] = acceptance_criteria
            metadata["evaluation_metric_ids"] = list(acceptance_criteria.keys())

        # 存储 goal 中的上下文
        if goal.get("context"):
            metadata["goal_context"] = goal["context"]

        # 存储执行相关参数
        if inputs.get("workspace"):
            metadata["workspace"] = inputs["workspace"]
        if inputs.get("max_retries"):
            metadata["max_retries"] = inputs["max_retries"]
        if inputs.get("needs_preparation"):
            metadata["needs_preparation"] = inputs["needs_preparation"]
        if inputs.get("isolation_level"):
            metadata["isolation_level"] = inputs["isolation_level"]
        if inputs.get("task_scope"):
            metadata["task_scope"] = inputs["task_scope"]

        # 存储执行者信息
        if inputs.get("target_id"):
            metadata["target_id"] = inputs["target_id"]

        return metadata
