#!/usr/bin/env python3
"""Resource Merge 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("resource_merge_tool")

_rm_tool_cls: Any = None


def _load_resource_merge_tool() -> Any:
    """显式按文件路径加载本目录 tool.py 的 ResourceMergeTool。

    合宿平铺 sys.path 下裸名 ``tool`` 被多个成员的同名模块争用（部分成员在
    exec 期绑定，静息态槽位是异成员模块），handler 内裸名 import 运行期会
    命中他人实现。exec 期间临时把本目录置顶 sys.path，保证 tool.py 内部的
    裸名依赖同样命中本目录；加载结果模块级缓存。
    """
    global _rm_tool_cls
    if _rm_tool_cls is not None:
        return _rm_tool_cls
    import importlib.util

    here = os.path.dirname(__file__)
    sys.path.insert(0, here)
    try:
        spec = importlib.util.spec_from_file_location(
            "resource_merge_tool_impl", os.path.join(here, "tool.py")
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["resource_merge_tool_impl"] = mod
        spec.loader.exec_module(mod)
    finally:
        while here in sys.path:
            sys.path.remove(here)
    _rm_tool_cls = mod.ResourceMergeTool
    return _rm_tool_cls


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
async def resource_merge(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """资源合并与回滚。"""
    merge = _load_resource_merge_tool()()
    result = await merge.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
