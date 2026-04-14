"""核心插件接口。

从 pipeline.plugin 重新导出 ICorePlugin，
提供稳定的公共接口。
"""

from pipeline.plugin import ICorePlugin, PluginContext

__all__ = ["ICorePlugin", "PluginContext"]
