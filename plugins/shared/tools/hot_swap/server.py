#!/usr/bin/env python3
"""Hot Swap 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 0.1 src/ 已归档为 reference/0.1_src/（参考文件，不参与运行时）。
# hot_swap 工具走自身平铺实现（tool.py），0.2 迁移后无 0.1 死依赖。

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("hot_swap_tool")

# MCP 暴露的 schema 与后端 HotSwapTool.get_tool_definition() 一致
# （复用 input_schema，避免残桩 schema 在分发层丢字段）。
from tool import HotSwapTool  # noqa: E402,PLC0415

_HOT_SWAP_SCHEMA = HotSwapTool.get_tool_definition().input_schema


@plugin.tool(
    name="hot_swap",
    schema=_HOT_SWAP_SCHEMA,
    description="热替换与回滚工具（插件热替换/回滚、配置版本管理/回滚）",
)
async def hot_swap(**kwargs):
    from tool import HotSwapTool  # noqa: PLC0415
    t = HotSwapTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error, "error_code": result.error_code}

if __name__ == "__main__":
    plugin.run()
