"""
隔离执行工具

提供在隔离环境中执行操作的工具
"""

from typing import Any

from src.core.results import ToolExecutionResult
from src.isolation.tools import execute_with_isolation
from src.isolation.types import IsolationLevel
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)


class IsolationExecuteTool:
    """隔离执行工具

    在隔离环境中执行命令、代码或文件操作
    """

    @staticmethod
    def get_tool_definition() -> Tool:
        """
        获取工具定义

        Returns:
            工具定义
        """
        return Tool(
            name="isolation_execute",
            description="在隔离环境（容器/沙箱）中执行命令、代码或文件操作。"
            "适用场景：执行不可信代码、需要隔离环境的文件操作、测试代码而不影响宿主系统、运行实验性代码。"
            "不适用场景：仅需读取文件内容（使用file_read）、简单系统配置（直接使用bash_execute）、不需要隔离的常规操作。"
            "注意事项：隔离级别根据任务类型和操作类型自动选择；如果Docker不可用会自动降级到宿主机模式；"
            "执行时间默认30秒超时；沙箱环境有资源限制。",
            input_schema={
                "type": "object",
                "properties": {
                    "task_type": {
                        "type": "string",
                        "enum": ["project", "module", "atomic"],
                        "description": "任务类型：project(项目级任务), module(模块级任务), atomic(原子任务)",
                    },
                    "operation_type": {
                        "type": "string",
                        "enum": [
                            "code_execution",
                            "untrusted_code",
                            "desktop_control",
                            "file_operation",
                            "complex_file_op",
                            "system_config",
                            "network_request",
                        ],
                        "description": "操作类型（可选，用于覆盖默认隔离策略）：code_execution(代码执行), untrusted_code(不可信代码), "
                        "desktop_control(桌面控制), file_operation(文件操作), complex_file_op(复杂文件操作), "
                        "system_config(系统配置), network_request(网络请求)",
                    },
                    "operation": {
                        "type": "object",
                        "description": "操作定义，包含具体的执行内容",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["command", "python_code", "file_operation"],
                                "description": "操作类型：command(执行命令), python_code(执行Python代码), file_operation(文件操作)",
                            },
                            "command": {
                                "type": "string",
                                "description": "要执行的Shell命令（当type=command时）",
                            },
                            "code": {
                                "type": "string",
                                "description": "要执行的Python代码（当type=python_code时）",
                            },
                            "timeout": {
                                "type": "integer",
                                "description": "执行超时时间（秒），默认30秒",
                                "default": 30,
                            },
                        },
                        "required": ["type"],
                    },
                },
                "required": ["task_type", "operation"],
            },
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
            level=ToolLevel.USER,
            requires_approval=False,
            tags=["isolation", "sandbox", "container", "execution"],
            isolation_required=True,
            isolation_level=IsolationLevel.CONTAINER,
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """
        执行工具

        Args:
            inputs: 输入参数

        Returns:
            执行结果
        """
        task_type = inputs.get("task_type")
        operation_type = inputs.get("operation_type")
        operation = inputs.get("operation")
        parent_env_id = inputs.get("parent_env_id")

        # 验证必需参数
        if not task_type:
            return create_failure_result(
                error="task_type 是必需参数",
                error_code="MISSING_TASK_TYPE",
            )

        if not operation:
            return create_failure_result(
                error="operation 是必需参数",
                error_code="MISSING_OPERATION",
            )

        if not isinstance(operation, dict):
            return create_failure_result(
                error="operation 必须是对象",
                error_code="INVALID_OPERATION",
            )

        op_type = operation.get("type")
        if not op_type:
            return create_failure_result(
                error="operation.type 是必需参数",
                error_code="MISSING_OPERATION_TYPE",
            )

        # 生成任务 ID
        import uuid

        task_id = f"isolation-{uuid.uuid4().hex[:8]}"

        # 执行隔离操作
        result = await execute_with_isolation(
            task_id=task_id,
            task_type=task_type,
            operation_type=operation_type,
            operation=operation,
            parent_env_id=parent_env_id,
        )

        if result["success"]:
            return create_success_result(
                data=result["output"],
                metadata={
                    "task_id": task_id,
                    "task_type": task_type,
                    "operation_type": operation_type,
                },
            )
        else:
            return create_failure_result(
                error=result.get("error", "执行失败"),
                error_code="EXECUTION_FAILED",
                metadata={
                    "task_id": task_id,
                    "task_type": task_type,
                    "operation_type": operation_type,
                },
            )
