"""
Agent 模块异常定义
"""

from typing import Any

from core.exceptions.base import DomainException


class AgentException(DomainException):
    """Agent 异常基类

    Agent 模块相关异常的基类。
    """

    pass


class AgentNotFoundError(AgentException):
    """Agent 不存在异常

    当尝试访问不存在的 Agent 时抛出。

    Attributes:
        name: Agent 名称
    """

    def __init__(self, name: str, details: dict[str, Any] | None = None):
        """初始化 Agent 不存在异常

        Args:
            name: Agent 名称
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["agent_name"] = name
        super().__init__(
            f"Agent '{name}' 不存在",
            code="AGENT_NOT_FOUND",
            details=error_details,
        )
        self.name = name


class AgentAlreadyExistsError(AgentException):
    """Agent 已存在异常

    当尝试创建已存在的 Agent 时抛出。

    Attributes:
        name: Agent 名称
    """

    def __init__(self, name: str, details: dict[str, Any] | None = None):
        """初始化 Agent 已存在异常

        Args:
            name: Agent 名称
            details: 额外的错误详情（可选）
        """
        error_details = details.copy() if details else {}
        error_details["agent_name"] = name
        super().__init__(
            f"Agent '{name}' 已存在",
            code="AGENT_EXISTS",
            details=error_details,
        )
        self.name = name


class AgentExecutionError(AgentException):
    """Agent 执行错误异常

    当 Agent 执行过程中发生错误时抛出。

    Attributes:
        message: 错误消息
        cause: 原始异常
    """

    def __init__(
        self,
        message: str,
        cause: Exception | None = None,
        details: dict[str, Any] | None = None,
    ):
        """初始化 Agent 执行错误异常

        Args:
            message: 错误消息
            cause: 原始异常（可选）
            details: 额外的错误详情（可选）
        """
        super().__init__(message, code="AGENT_EXECUTION_ERROR", details=details)
        self.cause = cause


class SubAgentNestingError(AgentException):
    """SubAgent 嵌套错误异常

    当 SubAgent 尝试再创建 SubAgent 时抛出。
    """

    def __init__(self, details: dict[str, Any] | None = None):
        """初始化 SubAgent 嵌套错误异常

        Args:
            details: 额外的错误详情（可选）
        """
        super().__init__(
            "SubAgent 不能再启动 SubAgent",
            code="SUBAGENT_NESTING",
            details=details,
        )
