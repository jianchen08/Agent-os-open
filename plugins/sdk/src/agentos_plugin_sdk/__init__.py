"""灵汐 AgentOS 插件 SDK。

提供工具注册、资源注册、生命周期钩子与 MCP 服务端启动能力。

[来源: docs/tasks/task_08_python_sdk.md AC-07-1]
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from agentos_plugin_sdk.builtin_tool import BuiltinTool, register_builtin_tool
from agentos_plugin_sdk.capability import CapabilityHandle, STANDARD_CAPABILITIES
from agentos_plugin_sdk.enum_utils import safe_enum_value
from agentos_plugin_sdk.logging import (
    ContextFilter,
    JsonFormatter,
    LogContext,
    LoggingConfig,
    StructuredFormatter,
    get_logger,
    setup_logging,
)
from agentos_plugin_sdk.plugin import AgentOSPlugin
from agentos_plugin_sdk.results import (
    EXECUTION_TRANSITIONS,
    ExecutionResult,
    ExecutionStatus,
    ToolExecutionResult,
)
from agentos_plugin_sdk.server import McpServer
from agentos_plugin_sdk.settings import Settings, get_settings
from agentos_plugin_sdk.tool import collect_tools, tool
from agentos_plugin_sdk.tool_types import (
    InjectedArg,
    InjectedParam,
    Tool,
    ToolCategory,
    ToolExample,
    ToolLevel,
    ToolResult,
    ToolSource,
    ToolStatus,
    ToolUsageStats,
    create_failure_result,
    create_success_result,
)
from agentos_plugin_sdk.types import (
    CapabilityInjection,
    LifecycleEvent,
    ResourceDef,
    ToolDef,
)

try:
    __version__: str = _pkg_version("agentos-plugin-sdk")
except PackageNotFoundError:  # 未安装时（如源码直接运行）回退
    __version__ = "0.2.0"

__all__ = [
    "AgentOSPlugin",
    "BuiltinTool",
    "CapabilityHandle",
    "CapabilityInjection",
    "ContextFilter",
    "EXECUTION_TRANSITIONS",
    "ExecutionResult",
    "ExecutionStatus",
    "InjectedArg",
    "InjectedParam",
    "JsonFormatter",
    "LifecycleEvent",
    "LogContext",
    "LoggingConfig",
    "McpServer",
    "ResourceDef",
    "STANDARD_CAPABILITIES",
    "Settings",
    "StructuredFormatter",
    "Tool",
    "ToolCategory",
    "ToolDef",
    "ToolExample",
    "ToolExecutionResult",
    "ToolLevel",
    "ToolResult",
    "ToolSource",
    "ToolStatus",
    "ToolUsageStats",
    "__version__",
    "collect_tools",
    "create_failure_result",
    "create_success_result",
    "get_logger",
    "get_settings",
    "register_builtin_tool",
    "safe_enum_value",
    "setup_logging",
    "tool",
]
