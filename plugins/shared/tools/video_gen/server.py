#!/usr/bin/env python3
"""video_gen 工具 MCP 服务端——接口适配层。

配置来源（环境变量，不入仓）：
- AGENTOS_COMFYUI_ENDPOINT：ComfyUI 基地址（未设置时工具显式报 ENDPOINT_UNSET）
- AGENTOS_COMFYUI_TOKEN：反代 Bearer token（可缺省）
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("video_gen")

_TOOL_CLS: Any = None


def _load_tool_cls() -> Any:
    """显式按文件路径加载本目录 tool.py 的 VideoGenTool（合宿裸名防争用）。"""
    global _TOOL_CLS
    if _TOOL_CLS is not None:
        return _TOOL_CLS
    here = str(Path(__file__).parent)
    sys.path.insert(0, here)
    try:
        spec = importlib.util.spec_from_file_location(
            "video_gen_tool_impl", os.path.join(here, "tool.py")
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["video_gen_tool_impl"] = mod
        spec.loader.exec_module(mod)
    finally:
        while here in sys.path:
            sys.path.remove(here)
    _TOOL_CLS = mod.VideoGenTool
    return _TOOL_CLS


@plugin.tool(
    name="video_gen.render_segment",
    schema={
        "type": "object",
        "properties": {
            "visual_prompts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "画面提示词列表（英文电影感描述，将合并生成单条片段）",
            },
            "transition": {
                "type": "object",
                "description": "衔接裁决 {type: continue|cut}",
            },
            "start_frame": {
                "type": "string",
                "description": "上一段末帧图片路径（continue 时必填）",
            },
            "sec": {"type": "number", "description": "目标时长秒，默认 5"},
            "width": {"type": "integer", "description": "宽，默认 1280"},
            "height": {"type": "integer", "description": "高，默认 720"},
            "seed": {"type": "integer", "description": "随机种子（重试自动换新）"},
            "output_dir": {"type": "string", "description": "成片输出目录"},
            "timeout_secs": {"type": "number", "description": "单次尝试超时秒，默认 300"},
        },
        "required": ["visual_prompts", "output_dir"],
    },
    description="生成一段直播视频片段（ComfyUI 适配层，衔接裁决驱动双模板）",
)
async def render_segment(**kwargs: dict[str, Any]) -> dict[str, Any]:
    """生成视频段。"""
    tool = _load_tool_cls()(
        endpoint=os.environ.get("AGENTOS_COMFYUI_ENDPOINT"),
        token=os.environ.get("AGENTOS_COMFYUI_TOKEN"),
    )
    result = await tool.execute(kwargs)
    if result.success:
        return result.output
    return {"error": result.error, "error_code": result.error_code}


if __name__ == "__main__":
    plugin.run()
