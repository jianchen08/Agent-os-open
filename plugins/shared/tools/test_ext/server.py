#!/usr/bin/env python3
"""Playwright Test 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 将 0.1 源码目录加入 sys.path，使老代码的 from tools.* 导入可用
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_SRC_ROOT = os.path.join(_PROJECT_ROOT, 'src')
if os.path.isdir(_SRC_ROOT):
    sys.path.insert(0, _SRC_ROOT)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("test_tool")


@plugin.tool(
    name="playwright_test",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "url": {"type": "string"},
            "script": {"type": "string"},
            "browser": {"type": "string", "enum": ["chromium", "firefox", "webkit"], "default": "chromium"},
        },
    },
    description="Playwright 浏览器测试",
)
async def playwright_test(**kwargs):
    """执行 Playwright 测试。"""
    from tool import PlaywrightTestTool  # noqa: PLC0415
    tool = PlaywrightTestTool()
    result = await tool.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
