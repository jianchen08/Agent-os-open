#!/usr/bin/env python3
"""Media Generation 工具 MCP 服务端——接口适配层。

合并 image_generate、music_generate、video_generate、tts_generate 四个媒体工具。
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 0.1 src/ 已归档为 reference/0.1_src/（参考文件，不参与运行时）。
# 守卫保留：src 存在（过渡期/调试）则注入，否则跳过——media 工具走自身平铺实现。
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_SRC_ROOT = os.path.join(_PROJECT_ROOT, 'src')
if os.path.isdir(_SRC_ROOT):
    sys.path.insert(0, _SRC_ROOT)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("media_tools")

@plugin.tool(
    name="image_generate",
    schema={"type": "object", "properties": {"prompt": {"type": "string"}, "size": {"type": "string"}, "n": {"type": "integer", "default": 1}}, "required": ["prompt"]},
    description="图片生成",
)
async def image_generate(**kwargs):
    from image_generate import ImageGenerateTool  # noqa: PLC0415
    t = ImageGenerateTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}

@plugin.tool(
    name="music_generate",
    schema={"type": "object", "properties": {"prompt": {"type": "string"}, "duration": {"type": "integer", "default": 30}}, "required": ["prompt"]},
    description="音乐生成",
)
async def music_generate(**kwargs):
    from music_generate import MusicGenerateTool  # noqa: PLC0415
    t = MusicGenerateTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}

@plugin.tool(
    name="video_generate",
    schema={"type": "object", "properties": {"prompt": {"type": "string"}, "duration": {"type": "integer", "default": 5}}, "required": ["prompt"]},
    description="视频生成",
)
async def video_generate(**kwargs):
    from video_generate import VideoGenerateTool  # noqa: PLC0415
    t = VideoGenerateTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}

@plugin.tool(
    name="tts_generate",
    schema={"type": "object", "properties": {"text": {"type": "string"}, "voice": {"type": "string"}, "speed": {"type": "number", "default": 1.0}}, "required": ["text"]},
    description="文本转语音",
)
async def tts_generate(**kwargs):
    from tts_generate import TTSTool  # noqa: PLC0415
    t = TTSTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}

if __name__ == "__main__":
    plugin.run()
