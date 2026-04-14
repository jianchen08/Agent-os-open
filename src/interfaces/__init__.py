"""插件接口模块。

从 pipeline.plugin 模块重新导出核心接口，
供外部模块引用，避免直接依赖内部实现。
"""

from pipeline.plugin import (
    ICorePlugin,
    IInputPlugin,
    IOutputPlugin,
    OutputResult,
    PluginContext,
    PluginResult,
)
from pipeline.types import ErrorPolicy, RouteSignal

__all__ = [
    "ICorePlugin",
    "IInputPlugin",
    "IOutputPlugin",
    "OutputResult",
    "PluginContext",
    "PluginResult",
    "ErrorPolicy",
    "RouteSignal",
]
