"""输出插件接口与路由信号。

从 pipeline.plugin 和 pipeline.types 重新导出 IOutputPlugin、OutputResult 和 RouteSignal，
提供稳定的公共接口。
"""

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import RouteSignal

__all__ = ["IOutputPlugin", "OutputResult", "PluginContext", "RouteSignal"]
