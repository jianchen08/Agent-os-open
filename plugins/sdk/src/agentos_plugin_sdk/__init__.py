"""灵汐 AgentOS 插件 SDK。

提供工具注册、资源注册、生命周期钩子与 MCP 服务端启动能力。

[来源: docs/tasks/task_08_python_sdk.md AC-07-1]
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from agentos_plugin_sdk.capability import CapabilityHandle, STANDARD_CAPABILITIES
from agentos_plugin_sdk.plugin import AgentOSPlugin
from agentos_plugin_sdk.server import McpServer
from agentos_plugin_sdk.tool import collect_tools, tool
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
    "CapabilityHandle",
    "CapabilityInjection",
    "LifecycleEvent",
    "McpServer",
    "ResourceDef",
    "STANDARD_CAPABILITIES",
    "ToolDef",
    "__version__",
    "collect_tools",
    "tool",
]
