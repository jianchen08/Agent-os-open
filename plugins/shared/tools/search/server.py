#!/usr/bin/env python3
"""Resource Search 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 跨插件共享类型走 SDK（agentos_plugin_sdk，pip 安装）。
# ToolLimits 为本工具自有的 constants.py，由上方 sys.path 注入解析。
# 不再依赖 0.1 兼容 shim。

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("resource_search_tool")

@plugin.tool(
    name="resource_search",
    schema={"type": "object", "properties": {"query": {"type": "string"}, "resource_type": {"type": "string"}, "mode": {"type": "string", "default": "simple"}, "limit": {"type": "integer", "default": 20}}, "required": ["resource_type"]},
    description="搜索系统内资源",
)
async def resource_search(**kwargs):
    from tool import ResourceSearchTool  # noqa: PLC0415
    t = ResourceSearchTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}

if __name__ == "__main__":
    plugin.run()
