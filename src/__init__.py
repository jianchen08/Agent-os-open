"""Agent OS 插件化管道架构。

提供完整的插件化管道引擎实现，包括类型定义、插件接口、
路由表、执行链、管道引擎、注册表和配置加载。
"""

from pipeline.types import (
    ErrorPolicy,
    RouteSignal,
    StateKeys,
    TargetType,
    create_initial_state,
)
from pipeline.plugin import (
    ICorePlugin,
    IInputPlugin,
    IOutputPlugin,
    IPlugin,
    OutputResult,
    PluginContext,
    PluginResult,
)

__all__ = [
    "ErrorPolicy",
    "RouteSignal",
    "StateKeys",
    "TargetType",
    "create_initial_state",
    "IPlugin",
    "IInputPlugin",
    "ICorePlugin",
    "IOutputPlugin",
    "PluginContext",
    "PluginResult",
    "OutputResult",
]
