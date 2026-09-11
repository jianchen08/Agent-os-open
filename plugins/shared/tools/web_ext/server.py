#!/usr/bin/env python3
"""Web Operate 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("web_operate_tool")

_web_tool_cls: Any = None


def _load_web_tool() -> Any:
    """显式按文件路径加载本目录 tool.py 的 WebTool。

    合宿平铺 sys.path 下裸名 ``tool`` 被多个成员的同名模块争用（部分成员在
    exec 期绑定，静息态槽位是异成员模块），handler 内裸名 import 运行期会
    命中他人实现。exec 期间临时把本目录置顶 sys.path，保证 tool.py 内部的
    裸名依赖同样命中本目录；加载结果模块级缓存。
    """
    global _web_tool_cls
    if _web_tool_cls is not None:
        return _web_tool_cls
    import importlib.util

    here = os.path.dirname(__file__)
    sys.path.insert(0, here)
    try:
        spec = importlib.util.spec_from_file_location(
            "web_operate_tool_impl", os.path.join(here, "tool.py")
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["web_operate_tool_impl"] = mod
        spec.loader.exec_module(mod)
    finally:
        while here in sys.path:
            sys.path.remove(here)
    _web_tool_cls = mod.WebTool
    return _web_tool_cls


@plugin.tool(
    name="web_operate",
    schema={"type": "object", "properties": {"action": {"type": "string", "enum": ["get", "post", "fetch"], "default": "get"}, "url": {"type": "string"}, "headers": {"type": "object"}, "data": {"type": "object"}, "params": {"type": "object"}, "timeout": {"type": "integer", "default": 30}, "extract_text": {"type": "boolean", "default": True}}, "required": ["action", "url"]},
    description="Web 操作",
)
async def web_operate(**kwargs: dict[str, Any]) -> dict[str, Any]:
    t = _load_web_tool()()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}

if __name__ == "__main__":
    plugin.run()
