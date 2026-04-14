"""输入插件接口。

从 pipeline.plugin 重新导出 IInputPlugin，
提供稳定的公共接口。
"""

from pipeline.plugin import IInputPlugin, PluginContext, PluginResult

__all__ = ["IInputPlugin", "PluginContext", "PluginResult"]
