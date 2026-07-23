#!/usr/bin/env python3
"""Resource Merge 工具 MCP 服务端——接口适配层。"""
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

plugin = AgentOSPlugin("resource_merge_tool")


@plugin.tool(
    name="resource_merge",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "prepare", "merge", "rollback",
                    "git_status", "git_commit", "git_diff", "git_log",
                    "git_merge_abort", "cleanup",
                ],
            },
            "workspace": {"type": "string"},
            "target_files": {"type": "array", "items": {"type": "string"}},
            "target_dir": {"type": "string"},
            "message": {"type": "string"},
            "checkpoint_id": {"type": "string"},
            "merge_strategy": {
                "type": "string",
                "enum": ["copy", "git_merge", "git_merge_no_ff"],
                "default": "copy",
            },
        },
        "required": ["action", "workspace"],
    },
    description="基于 git worktree 的资源合并与回滚",
)
async def resource_merge(**kwargs):
    """资源合并与回滚。"""
    from tool import ResourceMergeTool  # noqa: PLC0415

    merge = ResourceMergeTool()
    result = await merge.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
