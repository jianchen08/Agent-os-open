"""
Workflow 模块异常定义
"""

from typing import Any

from src.core.exceptions.base import DomainException


class WorkflowException(DomainException):
    """工作流异常基类

    工作流模块相关异常的基类。
    """

    pass


class WorkflowNotFoundError(WorkflowException):
    """工作流不存在异常

    Attributes:
        workflow_id: 工作流 ID
    """

    def __init__(self, workflow_id: str, details: dict[str, Any] | None = None):
        """初始化工作流不存在异常

        Args:
            workflow_id: 工作流 ID
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["workflow_id"] = workflow_id
        super().__init__(
            f"工作流不存在: {workflow_id}",
            code="WORKFLOW_NOT_FOUND",
            details=error_details,
        )
        self.workflow_id = workflow_id


class WorkflowValidationError(WorkflowException):
    """工作流验证失败异常

    Attributes:
        errors: 验证错误列表
    """

    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ):
        """初始化工作流验证失败异常

        Args:
            message: 错误消息
            errors: 验证错误列表（可选）
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        if errors:
            error_details["errors"] = errors
        super().__init__(message, code="WORKFLOW_VALIDATION_ERROR", details=error_details)
        self.errors = errors or []


class WorkflowExecutionError(WorkflowException):
    """工作流执行失败异常

    Attributes:
        workflow_id: 工作流 ID
        node_id: 节点 ID
        cause: 原始异常
    """

    def __init__(
        self,
        workflow_id: str,
        message: str,
        node_id: str | None = None,
        cause: Exception | None = None,
        details: dict[str, Any] | None = None,
    ):
        """初始化工作流执行失败异常

        Args:
            workflow_id: 工作流 ID
            message: 错误消息
            node_id: 节点 ID（可选）
            cause: 原始异常（可选）
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["workflow_id"] = workflow_id
        if node_id:
            error_details["node_id"] = node_id
        super().__init__(
            f"工作流 '{workflow_id}' 执行失败: {message}",
            code="WORKFLOW_EXECUTION_ERROR",
            details=error_details,
        )
        self.workflow_id = workflow_id
        self.node_id = node_id
        self.cause = cause


class NodeExecutionError(WorkflowException):
    """节点执行失败异常

    Attributes:
        node_id: 节点 ID
        cause: 原始异常
    """

    def __init__(
        self,
        node_id: str,
        message: str,
        cause: Exception | None = None,
        details: dict[str, Any] | None = None,
    ):
        """初始化节点执行失败异常

        Args:
            node_id: 节点 ID
            message: 错误消息
            cause: 原始异常（可选）
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["node_id"] = node_id
        super().__init__(
            f"节点 '{node_id}' 执行失败: {message}",
            code="NODE_EXECUTION_ERROR",
            details=error_details,
        )
        self.node_id = node_id
        self.cause = cause


class CycleDetectedError(WorkflowException):
    """检测到无效循环异常

    Attributes:
        cycle_path: 循环路径
    """

    def __init__(
        self,
        cycle_path: list[str],
        details: dict[str, Any] | None = None,
    ):
        """初始化检测到无效循环异常

        Args:
            cycle_path: 循环路径
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["cycle_path"] = cycle_path
        path_str = " -> ".join(cycle_path)
        super().__init__(
            f"检测到无效循环: {path_str}",
            code="CYCLE_DETECTED",
            details=error_details,
        )
        self.cycle_path = cycle_path


class AdapterError(WorkflowException):
    """适配器错误异常

    Attributes:
        adapter_name: 适配器名称
    """

    def __init__(
        self,
        adapter_name: str,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        """初始化适配器错误异常

        Args:
            adapter_name: 适配器名称
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["adapter_name"] = adapter_name
        super().__init__(
            f"适配器 '{adapter_name}' 错误: {message}",
            code="ADAPTER_ERROR",
            details=error_details,
        )
        self.adapter_name = adapter_name


class MaxIterationsExceededError(WorkflowException):
    """超过最大迭代次数异常

    Attributes:
        node_id: 节点 ID
        max_iterations: 最大迭代次数
    """

    def __init__(
        self,
        node_id: str,
        max_iterations: int,
        details: dict[str, Any] | None = None,
    ):
        """初始化超过最大迭代次数异常

        Args:
            node_id: 节点 ID
            max_iterations: 最大迭代次数
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["node_id"] = node_id
        error_details["max_iterations"] = max_iterations
        super().__init__(
            f"节点 '{node_id}' 超过最大迭代次数: {max_iterations}",
            code="MAX_ITERATIONS_EXCEEDED",
            details=error_details,
        )
        self.node_id = node_id
        self.max_iterations = max_iterations
