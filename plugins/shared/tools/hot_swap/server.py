#!/usr/bin/env python3
"""Hot Swap 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 0.1 src/ 已归档为 reference/0.1_src/（参考文件，不参与运行时）。
# 守卫保留：src 存在（过渡期/调试）则注入，否则跳过——hot_swap 工具走自身平铺实现。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_SRC_ROOT = os.path.join(_PROJECT_ROOT, 'src')
if os.path.isdir(_SRC_ROOT):
    sys.path.insert(0, _SRC_ROOT)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("hot_swap_tool")

@plugin.tool(
    name="hot_swap",
    schema={"type": "object", "properties": {"action": {"type": "string"}, "plugin_id": {"type": "string"}, "config": {"type": "object"}}, "required": ["action"]},
    description="热替换工具",
)
async def hot_swap(**kwargs):
    from tool import HotSwapTool  # noqa: PLC0415
    t = HotSwapTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}

if __name__ == "__main__":
    plugin.run()
