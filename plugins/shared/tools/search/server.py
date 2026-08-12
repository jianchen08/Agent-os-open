#!/usr/bin/env python3
"""Resource Search 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 将 0.1 兼容 shim 目录加入 sys.path，使老代码的 from core.* / tools.* / utils.* /
# triggers.* / tasks.* / agents.* 导入解析到 legacy_0_1_compat 下的精简副本。
#详见 plugins/shared/legacy_0_1_compat/__init__.py。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_COMPAT_ROOT = os.path.join(_PROJECT_ROOT, 'plugins', 'shared', 'legacy_0_1_compat')
if os.path.isdir(_COMPAT_ROOT):
    sys.path.insert(0, _COMPAT_ROOT)

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
