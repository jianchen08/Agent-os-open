#!/usr/bin/env python3
"""Download 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("download_tool")


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
    from tool import DownloadTool  # noqa: PLC0415

    dl = DownloadTool()
    result = await dl.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error}


if __name__ == "__main__":
    plugin.run()
