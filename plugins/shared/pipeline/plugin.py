"""pipeline.plugin 顶层 re-export——指向 _base/plugin.py。

保留此文件使老代码的 `from pipeline.plugin import ...` 能正常解析。
"""
from ._base.plugin import (  # noqa: F401
    ICorePlugin,
    IInputPlugin,
    IOutputPlugin,
    IPlugin,
    OutputResult,
    PluginContext,
    PluginResult,
    find_plugin_config,
)
