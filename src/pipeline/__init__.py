"""管道核心框架模块。

提供插件化管道引擎的完整实现，包括：
- 类型定义（types）
- 插件接口与上下文（plugin）
- 路由表与路由信号（route）
- 插件执行链（chain）
- 管道引擎（engine）
- 插件与管道路由注册表（registry）
- 配置加载与构建（config）
"""

from pipeline.types import (
    TargetType,
    StateKeys,
    RouteSignal,
    ErrorPolicy,
    create_initial_state,
)
from pipeline.plugin import (
    IPlugin,
    IInputPlugin,
    ICorePlugin,
    IOutputPlugin,
    PluginContext,
    PluginResult,
    OutputResult,
)
from pipeline.route import (
    InputRouteEntry,
    OutputRouteEntry,
    InputRouteTable,
    OutputRouteTable,
)
from pipeline.chain import PluginChain
from pipeline.engine import PipelineEngine
from pipeline.registry import PluginRegistry, PipelineRegistry
from pipeline.config import PipelineConfig, load_pipeline_config, build_plugin_registry

__all__ = [
    # Types
    "TargetType",
    "StateKeys",
    "RouteSignal",
    "ErrorPolicy",
    "create_initial_state",
    # Plugin interfaces
    "IPlugin",
    "IInputPlugin",
    "ICorePlugin",
    "IOutputPlugin",
    "PluginContext",
    "PluginResult",
    "OutputResult",
    # Route
    "InputRouteEntry",
    "OutputRouteEntry",
    "InputRouteTable",
    "OutputRouteTable",
    # Chain
    "PluginChain",
    # Engine
    "PipelineEngine",
    # Registry
    "PluginRegistry",
    "PipelineRegistry",
    # Config
    "PipelineConfig",
    "load_pipeline_config",
    "build_plugin_registry",
]
