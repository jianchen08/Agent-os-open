#!/usr/bin/env python3
"""Download 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("download_tool")

_dl_tool_cls: Any = None


def _load_download_tool() -> Any:
    """显式按文件路径加载本目录 tool.py 的 DownloadTool。

    合宿平铺 sys.path 下裸名 ``tool`` 被多个成员的同名模块争用（部分成员在
    exec 期绑定，静息态槽位是异成员模块），handler 内裸名 import 运行期会
    命中他人实现。exec 期间临时把本目录置顶 sys.path，保证 tool.py 内部的
    裸名依赖同样命中本目录；加载结果模块级缓存。
    """
    global _dl_tool_cls
    if _dl_tool_cls is not None:
        return _dl_tool_cls
    import importlib.util

    here = os.path.dirname(__file__)
    sys.path.insert(0, here)
    try:
        spec = importlib.util.spec_from_file_location(
            "download_tool_impl", os.path.join(here, "tool.py")
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["download_tool_impl"] = mod
        spec.loader.exec_module(mod)
    finally:
        while here in sys.path:
            sys.path.remove(here)
    _dl_tool_cls = mod.DownloadTool
    return _dl_tool_cls


@plugin.tool(
    name="download",
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "下载地址（http/https）"},
            "save_path": {"type": "string", "description": "保存目录路径"},
            "filename": {"type": "string"},
            "max_connections": {"type": "integer", "default": 8},
            "max_retries": {"type": "integer", "default": 5},
            "timeout": {"type": "integer", "default": 300},
            "max_size": {"type": "integer", "default": 1073741824},
            "proxy": {"type": "string"},
            "allow_domains": {"type": "array", "items": {"type": "string"}},
            "expected_hash": {"type": "string"},
            # skip_ssrf_check 不暴露给 LLM：SSRF 旁路仅由服务端构造参数
            # allow_ssrf_skip 控制（FP-MIGR 安全随迁），防提示注入旁路内网探测。
        },
        "required": ["url", "save_path"],
    },
    description="通用文件下载工具",
)
async def download(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """下载文件。"""
    dl = _load_download_tool()()
    result = await dl.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
