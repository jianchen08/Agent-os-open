#!/usr/bin/env python3
"""stream_control 工具 MCP 服务端。

配置来源（环境变量，不入仓）：AGENTOS_LIVESTREAM_RTMP（推流地址，含推流码）。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("stream_control")

_TOOL_CLS: Any = None


def _load_tool_cls() -> Any:
    """显式按文件路径加载本目录 tool.py（合宿裸名防争用）。"""
    global _TOOL_CLS
    if _TOOL_CLS is not None:
        return _TOOL_CLS
    here = str(Path(__file__).parent)
    sys.path.insert(0, here)
    try:
        spec = importlib.util.spec_from_file_location(
            "stream_control_tool_impl", os.path.join(here, "tool.py")
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["stream_control_tool_impl"] = mod
        spec.loader.exec_module(mod)
    finally:
        while here in sys.path:
            sys.path.remove(here)
    _TOOL_CLS = mod.StreamControlTool
    return _TOOL_CLS


@plugin.tool(
    name="stream.control",
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "stop", "status"]},
            "inputs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "播放列表（视频文件路径，start 必填）",
            },
            "overlay_text": {
                "type": "string",
                "description": "画面角标文本（AI 生成内容标识，start 时建议必带）",
            },
        },
        "required": ["action"],
    },
    description="FFmpeg 推流控制：start/stop/status；AI 角标烧录（D2）",
)
async def control(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """推流控制。"""
    tool = _load_tool_cls()(rtmp_url=os.environ.get("AGENTOS_LIVESTREAM_RTMP"))
    result = await tool.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error, "error_code": result.error_code}


if __name__ == "__main__":
    plugin.run()
