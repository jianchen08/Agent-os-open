"""插件注册表。

PluginRegistry 管理管道内的插件注册，
EngineRegistry 统一管理引擎实例注册与查找（替代三级查找）。

实现已拆分到子模块：
- pipeline_entry.py: PipelineEntry 数据类
- engine_registry.py: EngineRegistry 单例 + get_engine_registry

本文件保持所有公共 API 的导入路径不变，确保外部模块无需修改。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from pipeline.engine_registry import EngineRegistry, get_engine_registry  # noqa: F401

# Re-export from sub-modules
from pipeline.pipeline_entry import MAX_TAGS_PER_PIPELINE, PipelineEntry  # noqa: F401
from pipeline.plugin import (
    ICorePlugin,
    IOutputPlugin,
    IPlugin,
)

logger = logging.getLogger(__name__)


class PluginRegistry:
    """管道内插件注册表。

    管理插件实例的注册、查找和分类检索。

    Attributes:
        _plugins: 名称到插件实例的映射
        _core_plugins: core_type 到核心插件实例的映射
    """

    def __init__(self) -> None:
        self._plugins: dict[str, IPlugin] = {}
        self._core_plugins: dict[str, ICorePlugin] = {}
        self._output_plugins_cache: list[IOutputPlugin] | None = None

    def register(self, plugin: IPlugin) -> None:
        """注册一个插件实例。"""
        self._plugins[plugin.name] = plugin
        self._output_plugins_cache = None
        if isinstance(plugin, ICorePlugin):
            logger.warning(
                "Core plugin '%s' registered via register(), consider using register_core() for explicit core_type mapping",
                plugin.name,
            )
            self._core_plugins[plugin.name] = plugin
        logger.debug("Plugin registered: %s (type=%s)", plugin.name, type(plugin).__name__)

    def register_core(self, name: str, plugin: ICorePlugin) -> None:
        """注册核心插件。"""
        self._core_plugins[name] = plugin
        self._plugins[plugin.name] = plugin
        self._output_plugins_cache = None
        logger.debug("Core plugin registered: name=%s, plugin=%s", name, plugin.name)

    def get(self, name: str) -> IPlugin | None:
        """按名称获取插件实例。"""
        return self._plugins.get(name)

    def get_core(self, core_type: str) -> ICorePlugin | None:
        """按核心类型获取核心插件实例。"""
        return self._core_plugins.get(core_type)

    def get_output_plugins(self, core_type: str | None = None) -> list[IOutputPlugin]:
        """获取所有输出插件列表（带缓存）。"""
        if self._output_plugins_cache is not None:
            return self._output_plugins_cache
        output_plugins: list[IOutputPlugin] = []
        for plugin in self._plugins.values():
            if isinstance(plugin, IOutputPlugin):
                output_plugins.append(plugin)
        self._output_plugins_cache = sorted(output_plugins, key=lambda p: p.priority)
        return self._output_plugins_cache

    def fork(self) -> PluginRegistry:
        """创建插件注册表的深拷贝副本。"""
        new_registry = PluginRegistry()
        core_name_to_plugin_name: dict[str, str] = {}
        for core_name, plugin in self._core_plugins.items():
            core_name_to_plugin_name[core_name] = plugin.name

        for name, plugin in self._plugins.items():
            new_instance = plugin
            if hasattr(plugin, "_config"):
                try:
                    kwargs: dict[str, Any] = {"config": copy.deepcopy(plugin._config)}
                    if hasattr(plugin, "_adapter"):
                        kwargs["adapter"] = plugin._adapter
                    elif hasattr(plugin, "_router"):
                        kwargs["router"] = plugin._router
                    new_instance = type(plugin)(**kwargs)
                    for attr in ("_tools", "_tool_registry"):
                        if hasattr(plugin, attr):
                            setattr(new_instance, attr, getattr(plugin, attr))
                except Exception:
                    logger.debug("PluginRegistry.fork: 无法重建插件 %s, 复用原实例", name)
            new_registry._plugins[name] = new_instance

        for core_name, plugin_name in core_name_to_plugin_name.items():
            new_registry._core_plugins[core_name] = new_registry._plugins[plugin_name]
            orig = self._plugins.get(plugin_name)
            forked = new_registry._plugins.get(plugin_name)
            if hasattr(orig, "_tools") and hasattr(forked, "_tools"):
                forked._tools = dict(orig._tools)
            if hasattr(orig, "_tool_registry") and hasattr(forked, "_tool_registry"):
                forked._tool_registry = orig._tool_registry
        return new_registry

    def replace(self, name: str, new_plugin: IPlugin) -> IPlugin | None:
        """替换已注册的插件。"""
        old_plugin = self._plugins.pop(name, None)
        if old_plugin is not None and isinstance(old_plugin, ICorePlugin) and name in self._core_plugins:
            del self._core_plugins[name]
        self._plugins[name] = new_plugin
        self._output_plugins_cache = None
        if isinstance(new_plugin, ICorePlugin):
            self._core_plugins[name] = new_plugin
        try:
            new_name = new_plugin.name
        except Exception:
            logger.debug("Plugin replaced: %s (new name unavailable)", name)
            new_name = "<error>"
        logger.info("Plugin replaced: %s → %s", name, new_name)
        return old_plugin

    def list_plugins(self) -> list[str]:
        """列出所有已注册插件的名称。"""
        return list(self._plugins.keys())
