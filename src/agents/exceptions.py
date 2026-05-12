"""
Agent 模块异常定义
"""
from src.core.exceptions import DomainException


class AgentException(DomainException):
    pass


class AgentNotFoundError(AgentException):
    def __init__(self, name: str, details: dict = None):
        error_details = (details or {}).copy()
        error_details["agent_name"] = name
        super().__init__(f"Agent '{name}' 不存在", code="AGENT_NOT_FOUND", details=error_details)
        self.name = name


class AgentAlreadyExistsError(AgentException):
    def __init__(self, name: str, details: dict = None):
        error_details = (details or {}).copy()
        error_details["agent_name"] = name
        super().__init__(f"Agent '{name}' 已存在", code="AGENT_EXISTS", details=error_details)
        self.name = name


class AgentExecutionError(AgentException):
    def __init__(self, message: str, cause: Exception = None, details: dict = None):
        super().__init__(message, code="AGENT_EXECUTION_ERROR", details=details, cause=cause)
        self.cause = cause


class SubAgentNestingError(AgentException):
    def __init__(self, details: dict = None):
        super().__init__("SubAgent 不能再启动 SubAgent", code="SUBAGENT_NESTING", details=details)
