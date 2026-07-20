#!/usr/bin/env python3
"""Bash 工具 MCP 服务端——接口适配层。

老代码从 0.1 src/tools/builtin/bash/ 原封不动复制到本目录。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 将 0.1 源码目录加入 sys.path，使老代码的 from tools.* 导入可用
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_SRC_ROOT = os.path.join(_PROJECT_ROOT, 'src')
if os.path.isdir(_SRC_ROOT):
    sys.path.insert(0, _SRC_ROOT)

from lingxi_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("bash_tool")


@plugin.tool(
    name="bash_execute",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["execute", "continue", "terminate", "input", "read_log"],
                "default": "execute",
            },
            "command": {"type": "string"},
            "pid": {"type": "integer"},
            "timeout": {"type": "integer", "default": 30, "maximum": 290},
            "working_dir": {"type": "string"},
            "input_text": {"type": "string"},
            "force": {"type": "boolean", "default": False},
        },
        "required": [],
    },
    description="执行 Shell 命令",
)
async def bash_execute(**kwargs):
    """执行 Shell 命令。"""
    from tool import BashTool  # noqa: PLC0415

    bash = BashTool()
    result = await bash.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
