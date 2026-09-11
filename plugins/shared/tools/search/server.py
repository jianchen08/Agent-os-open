#!/usr/bin/env python3
"""Resource Search 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# 跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装）。
# ToolLimits 为本工具自有的 constants.py，由上方 sys.path 注入解析。
# 不再依赖 0.1 兼容 shim。

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("resource_search_tool")

_rs_tool_cls: Any = None


def _load_resource_search_tool() -> Any:
    """显式按文件路径加载本目录 tool.py 的 ResourceSearchTool。

    合宿平铺 sys.path 下裸名 ``tool`` / ``constants`` 会被其他成员的同名模块
    遮蔽（实测命中 triggers_ext/tool.py → import 错误连败）。exec 期间临时把
    本目录置顶 sys.path，并摘除占据本目录裸名槽位的异成员模块（sys.modules
    命中优先于 sys.path，仅置顶路径挡不住——cost_control 装载期 import
    constants 即占据静息态槽位，call-time exec 的 ``from constants import
    ToolLimits`` 不摘槽必命中异成员），exec 后按 host loader 同款语义恢复
    静息态；加载结果模块级缓存。
    """
    global _rs_tool_cls
    if _rs_tool_cls is not None:
        return _rs_tool_cls
    import importlib.util

    here = os.path.dirname(__file__)
    sys.path.insert(0, here)
    masked: dict[str, Any] = {}
    try:
        # 摘除异成员占据的本目录裸名槽位（host loader 同款手法）：本目录提供的
        # 顶层模块在 exec 窗口内一律本目录优先，异成员模块 exec 后原样恢复。
        here_prefix = os.path.normcase(os.path.abspath(here)) + os.sep
        for entry in Path(here).iterdir():
            if entry.suffix != ".py":
                continue
            name = entry.stem
            module = sys.modules.get(name)
            file = getattr(module, "__file__", None)
            if module is None or (
                file and os.path.normcase(os.path.abspath(file)).startswith(here_prefix)
            ):
                continue
            masked[name] = sys.modules.pop(name)
        spec = importlib.util.spec_from_file_location(
            "resource_search_tool_impl", os.path.join(here, "tool.py")
        )
        assert spec is not None
        assert spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        while here in sys.path:
            sys.path.remove(here)
        for name, module in masked.items():
            sys.modules[name] = module
    _rs_tool_cls = mod.ResourceSearchTool
    return _rs_tool_cls


@plugin.tool(
    name="resource_search",
    schema={"type": "object", "properties": {"query": {"type": "string"}, "resource_type": {"type": "string"}, "mode": {"type": "string", "default": "simple"}, "limit": {"type": "integer", "default": 20}}, "required": ["resource_type"]},
    description="搜索系统内资源",
)
async def resource_search(**kwargs: dict[str, Any]) -> dict[str, Any]:
    t = _load_resource_search_tool()()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}

if __name__ == "__main__":
    plugin.run()
