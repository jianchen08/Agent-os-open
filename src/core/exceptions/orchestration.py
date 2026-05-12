"""
Orchestration 模块异常定义
"""

from typing import Any

from src.core.exceptions.base import DomainException


class OrchestrationException(DomainException):
    """编排中心异常基类

    编排中心模块相关异常的基类。
    """

    pass


class TaskNotFoundError(OrchestrationException):
    """任务不存在异常

    当尝试访问不存在的任务时抛出。
    """

    def __init__(
        self,
        message: str = "任务不存在",
        details: dict[str, Any] | None = None,
    ):
        """初始化任务不存在异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="TASK_NOT_FOUND", details=details)


class ResourceExhaustedError(OrchestrationException):
    """资源耗尽异常

    当系统资源不足无法分配时抛出。
    """

    def __init__(
        self,
        message: str = "系统资源不足",
        details: dict[str, Any] | None = None,
    ):
        """初始化资源耗尽异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="RESOURCE_EXHAUSTED", details=details)


class TaskExecutionError(OrchestrationException):
    """任务执行异常

    当任务执行过程中发生错误时抛出。
    """

    def __init__(
        self,
        message: str = "任务执行失败",
        details: dict[str, Any] | None = None,
    ):
        """初始化任务执行异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="TASK_EXECUTION_ERROR", details=details)


class SchedulerError(OrchestrationException):
    """调度器异常

    当调度器运行过程中发生错误时抛出。
    """

    def __init__(
        self,
        message: str = "调度器错误",
        details: dict[str, Any] | None = None,
    ):
        """初始化调度器异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="SCHEDULER_ERROR", details=details)


class SubAgentNestingError(OrchestrationException):
    """子代理嵌套层数超限异常

    当子代理嵌套层数超过系统限制时抛出。
    """

    def __init__(
        self,
        message: str = "子代理嵌套层数超限",
        details: dict[str, Any] | None = None,
    ):
        """初始化子代理嵌套异常

        Args:
            message: 错误消息
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="SUB_AGENT_NESTING_ERROR", details=details)


OrchestrationError = OrchestrationException
