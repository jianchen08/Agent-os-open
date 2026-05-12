"""
编排模块异常定义
"""
from src.core.exceptions import DomainException


class OrchestrationException(DomainException):
    pass


class OrchestrationError(OrchestrationException):
    """通用编排错误"""

    def __init__(self, message: str = "编排执行错误", details: dict = None):
        super().__init__(message, code="ORCHESTRATION_ERROR", details=details)


class TaskNotFoundError(OrchestrationException):
    def __init__(self, message: str = "任务不存在", details: dict = None):
        super().__init__(message, code="TASK_NOT_FOUND", details=details)


class ResourceExhaustedError(OrchestrationException):
    def __init__(self, message: str = "系统资源不足", details: dict = None):
        super().__init__(message, code="RESOURCE_EXHAUSTED", details=details)


class TaskExecutionError(OrchestrationException):
    def __init__(self, message: str = "任务执行失败", details: dict = None):
        super().__init__(message, code="TASK_EXECUTION_ERROR", details=details)


class SubAgentNestingError(OrchestrationException):
    def __init__(self, message: str = "子代理嵌套层数超限", details: dict = None):
        super().__init__(message, code="SUB_AGENT_NESTING_ERROR", details=details)
