"""
异常包装器

将模块特定的异常转换为统一的核心异常层次结构。
这允许各个模块保持其特定的异常类型，同时在 API 层统一处理。
"""

import logging

from src.core.exceptions import (
    BaseAppException,
    BusinessRuleException,
    ConfigurationException,
    ConflictException,
    ExternalServiceException,
    NotFoundException,
    PermissionException,
    TimeoutException,
    ValidationException,
)

logger = logging.getLogger(__name__)

# ============================================================================
# 异常映射配置
# ============================================================================

# 模块特定异常到核心异常的映射
EXCEPTION_MAPPING = {
    # Agent 模块异常
    "AgentNotFoundError": (NotFoundException, "AGENT_001"),
    "AgentAlreadyExistsError": (ConflictException, "AGENT_002"),
    "AgentExecutionError": (BusinessRuleException, "AGENT_003"),
    "SubAgentNestingError": (BusinessRuleException, "AGENT_004"),
    # 工作流模块异常
    "WorkflowNotFoundError": (NotFoundException, "WORKFLOW_001"),
    "WorkflowValidationError": (ValidationException, "WORKFLOW_002"),
    "WorkflowExecutionError": (BusinessRuleException, "WORKFLOW_003"),
    "NodeExecutionError": (BusinessRuleException, "WORKFLOW_004"),
    "CycleDetectedError": (ValidationException, "WORKFLOW_005"),
    "AdapterError": (ExternalServiceException, "WORKFLOW_006"),
    "MaxIterationsExceededError": (TimeoutException, "WORKFLOW_007"),
    # 工具模块异常
    "ToolNotFoundError": (NotFoundException, "TOOL_001"),
    "ToolAlreadyExistsError": (ConflictException, "TOOL_002"),
    "ToolValidationError": (ValidationException, "TOOL_003"),
    "ToolExecutionError": (ExternalServiceException, "TOOL_004"),
    "ApprovalRequiredError": (PermissionException, "TOOL_005"),
    "MCPConnectionError": (ExternalServiceException, "TOOL_006"),
    "MCPConfigError": (ConfigurationException, "TOOL_007"),
    # 认证模块异常
    "TokenExpiredError": (PermissionException, "AUTH_001"),
    "TokenInvalidError": (PermissionException, "AUTH_002"),
    "TokenRevokedError": (PermissionException, "AUTH_003"),
    "AuthenticationError": (PermissionException, "AUTH_004"),
    "InvalidCredentialsError": (PermissionException, "AUTH_005"),
    "UserNotFoundError": (NotFoundException, "AUTH_006"),
    "UserInactiveError": (PermissionException, "AUTH_007"),
    "UserExistsError": (ConflictException, "AUTH_008"),
    "PermissionDeniedError": (PermissionException, "AUTH_009"),
    "RateLimitExceededError": (BusinessRuleException, "AUTH_010"),
}


# ============================================================================
# 异常包装器类
# ============================================================================


class ExceptionWrapper:
    """异常包装器

    将模块特定的异常转换为统一的核心异常。
    """

    @classmethod
    def wrap_exception(
        cls,
        exc: Exception,
        default_error_code: str | None = None,
    ) -> BaseAppException:
        """包装异常为核心异常

        Args:
            exc: 原始异常
            default_error_code: 默认错误码（当无法识别异常类型时使用）

        Returns:
            包装后的核心异常实例

        Examples:
            >>> try:
            ...     raise AgentNotFoundError("test")
            ... except Exception as e:
            ...     wrapped = ExceptionWrapper.wrap_exception(e)
            ...     assert isinstance(wrapped, NotFoundException)
        """
        exc_class_name = exc.__class__.__name__

        # 查找映射
        if exc_class_name in EXCEPTION_MAPPING:
            core_exc_class, error_code = EXCEPTION_MAPPING[exc_class_name]
            return cls._create_core_exception(
                core_exc_class,
                exc,
                error_code,
            )

        # 检查是否已经是核心异常
        if isinstance(exc, BaseAppException):
            return exc

        # 检查是否是已知的异常基类
        if hasattr(exc, "message") and hasattr(exc, "code"):
            # 类似 BaseAppException 的结构
            return cls._create_generic_exception(exc, default_error_code)

        # 默认包装为业务规则异常
        logger.warning(
            f"未识别的异常类型: {exc_class_name}, 包装为 BusinessRuleException"
        )
        return BusinessRuleException(
            message=str(exc),
            code=default_error_code or "UNKNOWN_ERROR",
            details={"original_type": exc_class_name, "original_message": str(exc)},
            cause=exc,
        )

    @classmethod
    def _create_core_exception(
        cls,
        core_exc_class: type[BaseAppException],
        original_exc: Exception,
        error_code: str,
    ) -> BaseAppException:
        """创建核心异常实例

        Args:
            core_exc_class: 核心异常类
            original_exc: 原始异常
            error_code: 错误码

        Returns:
            核心异常实例
        """
        # 提取异常信息
        message = str(original_exc)

        # 尝试从原始异常获取额外信息
        kwargs = {"code": error_code, "cause": original_exc}

        # 根据不同的核心异常类型添加特定参数
        if core_exc_class == NotFoundException:
            if hasattr(original_exc, "name"):
                kwargs["resource_id"] = original_exc.name
            elif hasattr(original_exc, "workflow_id"):
                kwargs["resource_id"] = original_exc.workflow_id
            elif hasattr(original_exc, "tool_name"):
                kwargs["resource_id"] = original_exc.tool_name

        elif core_exc_class == ConflictException:
            if hasattr(original_exc, "name"):
                kwargs["conflict_type"] = f"duplicate_{original_exc.name}"

        elif core_exc_class == ValidationException:
            if hasattr(original_exc, "errors") and original_exc.errors:
                kwargs["details"] = {"errors": original_exc.errors}

        elif core_exc_class == ExternalServiceException:
            if hasattr(original_exc, "server_name"):
                kwargs["service_name"] = original_exc.server_name
            elif hasattr(original_exc, "tool_name"):
                kwargs["service_name"] = f"tool:{original_exc.tool_name}"
            elif hasattr(original_exc, "adapter_name"):
                kwargs["service_name"] = f"adapter:{original_exc.adapter_name}"

        elif core_exc_class == TimeoutException:
            if hasattr(original_exc, "max_iterations"):
                kwargs["details"] = {"max_iterations": original_exc.max_iterations}

        # 创建异常实例
        try:
            return core_exc_class(message=message, **kwargs)
        except TypeError:
            # 如果参数不匹配，使用基本创建方式
            return core_exc_class(
                message=message,
                code=error_code,
                cause=original_exc,
            )

    @classmethod
    def _create_generic_exception(
        cls,
        exc: Exception,
        default_error_code: str | None = None,
    ) -> BaseAppException:
        """创建通用异常

        Args:
            exc: 原始异常
            default_error_code: 默认错误码

        Returns:
            核心异常实例
        """
        # 尝试保留原始错误码
        error_code = getattr(exc, "code", default_error_code or "UNKNOWN_ERROR")
        message = getattr(exc, "message", str(exc))

        return BusinessRuleException(
            message=message,
            code=error_code,
            details={"original_type": exc.__class__.__name__},
            cause=exc,
        )


# ============================================================================
# 便捷函数
# ============================================================================


def wrap_exception(
    exc: Exception,
    default_error_code: str | None = None,
) -> BaseAppException:
    """包装异常为核心异常

    这是 ExceptionWrapper.wrap_exception 的便捷函数。

    Args:
        exc: 原始异常
        default_error_code: 默认错误码

    Returns:
        包装后的核心异常实例

    Examples:
        >>> from src.core.exceptions import AgentNotFoundError
        >>> try:
        ...     raise AgentNotFoundError("test_agent")
        ... except Exception as e:
        ...     wrapped = wrap_exception(e)
        ...     assert isinstance(wrapped, NotFoundException)
        ...     assert wrapped.code == "AGENT_001"
    """
    return ExceptionWrapper.wrap_exception(exc, default_error_code)


def wrap_and_raise(
    exc: Exception,
    default_error_code: str | None = None,
) -> None:
    """包装异常并抛出

    Args:
        exc: 原始异常
        default_error_code: 默认错误码

    Raises:
        BaseAppException: 包装后的核心异常

    Examples:
        >>> try:
        ...     some_operation()
        ... except AgentNotFoundError as e:
        ...     wrap_and_raise(e)  # 抛出 NotFoundException
    """
    wrapped = wrap_exception(exc, default_error_code)
    raise wrapped


def is_module_exception(exc: Exception, module_name: str) -> bool:
    """检查异常是否来自特定模块

    Args:
        exc: 异常实例
        module_name: 模块名称（如 "agents", "workflows", "tools"）

    Returns:
        是否为指定模块的异常
    """
    module_map = {
        "agents": ["Agent"],
        "workflows": ["Workflow", "Node", "Adapter"],
        "tools": ["Tool", "MCP", "Approval"],
        "auth": ["Token", "User", "Auth", "Permission"],
    }

    prefixes = module_map.get(module_name, [])
    exc_class_name = exc.__class__.__name__

    return any(exc_class_name.startswith(prefix) for prefix in prefixes)
