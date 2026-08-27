#!/usr/bin/env python3
"""Media Generation 工具 MCP 服务端——接口适配层。

合并 image_generate、music_generate、video_generate、tts_generate 四个媒体工具。

F-MEDIA-2（provider 依赖迁移）：0.1 的 infrastructure.service_provider 全局
注册表已删，媒体 provider 调用改为经 tool-executor capability 调用后端服务
（与 hindsight_memory/wiring.py 同款模式）：
- ``_make_capability_caller``：从内核注入的能力句柄构造 capability_caller
  （tool-executor 优先，service-registry 回落；剥掉已含能力前缀——SDK
  CapabilityHandle.call 会拼接 ``f"{cap}.{method}"``，避免
  "tool-executor.tool-executor.invoke" 双命名空间）。
- ``_get_capability_caller``：懒缓存（能力在 MCP initialize 握手后注入，
  首次成功解析后才缓存）。
- on_load 时把 capability_caller 传入四工具构造（未注入时工具返回显式
  PROVIDER_UNAVAILABLE，不静默空转）。
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("media_tools")

# ── capability_caller 懒缓存 ──────────────────────────────
# 能力在 MCP initialize 握手后注入（见 SDK plugin._on_initialize），
# 因此 caller 在首次工具调用/on_load 时解析；仅成功结果入缓存（未注入时重试）。
_caller_cache: dict[str, Any] = {}


def _bind_caller(handle: Any, cap_name: str) -> Any:
    """绑定能力句柄与命名空间，构造 async caller `(method, params) -> Any`。

    闭包通过函数参数绑定，规避 B023（循环变量绑定）。

    Args:
        handle: CapabilityHandle 实例（其 call 会拼接 ``f"{cap}.{method}"``）
        cap_name: 能力命名空间（如 "tool-executor"）

    Returns:
        async caller：剥掉已含的能力前缀后转交 handle.call
    """
    prefix = f"{cap_name}."

    async def _call(method: str, params: dict[str, Any]) -> Any:
        stripped = method[len(prefix):] if method.startswith(prefix) else method
        return await handle.call(stripped, params)

    return _call


def _make_capability_caller(plugin_instance: Any) -> Any | None:
    """从内核注入的能力句柄构造 capability_caller。

    Args:
        plugin_instance: AgentOSPlugin 实例（含 get_capability）

    Returns:
        async caller `(method, params) -> Any`；能力未注入时返回 None
    """
    for cap_name in ("tool-executor", "service-registry"):
        try:
            handle = plugin_instance.get_capability(cap_name)
        except KeyError:
            continue
        return _bind_caller(handle, cap_name)
    logger.warning(
        "[media/server] 未注入 tool-executor/service-registry 能力，capability_caller 不可用"
    )
    return None


def _get_capability_caller() -> Any | None:
    """懒缓存获取 capability_caller（tool-executor 优先，service-registry 回落）。

    Returns:
        async caller；能力未注入时返回 None（不缓存，下次重试）
    """
    if "caller" in _caller_cache:
        return _caller_cache["caller"]
    caller = _make_capability_caller(plugin)
    if caller is not None:
        _caller_cache["caller"] = caller
    return caller


# ── 四工具实例（capability_caller 传入构造，F-MEDIA-2）──
from image_generate import ImageGenerateTool  # noqa: E402,PLC0415
from music_generate import MusicGenerateTool  # noqa: E402,PLC0415
from tts_generate import TtsGenerateTool  # noqa: E402,PLC0415
from video_generate import VideoGenerateTool  # noqa: E402,PLC0415

_TOOL_CLASSES: dict[str, Any] = {
    "image_generate": ImageGenerateTool,
    "music_generate": MusicGenerateTool,
    "video_generate": VideoGenerateTool,
    "tts_generate": TtsGenerateTool,
}
_tool_instances: dict[str, Any] = {}


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """内核注入能力后，构造四工具并传入 capability_caller（F-MEDIA-2）。"""
    caller = _get_capability_caller()
    logger.info("[media/server] on_load | capability_caller=%s", caller is not None)
    for name, cls in _TOOL_CLASSES.items():
        _tool_instances[name] = cls(capability_caller=caller)


def _get_tool(name: str) -> Any:
    """获取工具实例（on_load 已完成时用缓存实例；否则按需构造并传入 caller）。"""
    instance = _tool_instances.get(name)
    if instance is None:
        instance = _TOOL_CLASSES[name](capability_caller=_get_capability_caller())
        _tool_instances[name] = instance
    return instance


@plugin.tool(
    name="image_generate",
    schema={"type": "object", "properties": {"prompt": {"type": "string"}, "size": {"type": "string"}, "n": {"type": "integer", "default": 1}}, "required": ["prompt"]},
    description="图片生成",
)
async def image_generate(**kwargs: dict[str, Any]) -> dict[str, Any]:
    result = await _get_tool("image_generate").execute(kwargs)
    return result.output if result.success else {"error": result.error}

@plugin.tool(
    name="music_generate",
    schema={"type": "object", "properties": {"prompt": {"type": "string"}, "duration": {"type": "integer", "default": 30}}, "required": ["prompt"]},
    description="音乐生成",
)
async def music_generate(**kwargs: dict[str, Any]) -> dict[str, Any]:
    result = await _get_tool("music_generate").execute(kwargs)
    return result.output if result.success else {"error": result.error}

@plugin.tool(
    name="video_generate",
    schema={"type": "object", "properties": {"prompt": {"type": "string"}, "duration": {"type": "integer", "default": 5}}, "required": ["prompt"]},
    description="视频生成",
)
async def video_generate(**kwargs: dict[str, Any]) -> dict[str, Any]:
    result = await _get_tool("video_generate").execute(kwargs)
    return result.output if result.success else {"error": result.error}

@plugin.tool(
    name="tts_generate",
    schema={"type": "object", "properties": {"text": {"type": "string"}, "voice": {"type": "string"}, "speed": {"type": "number", "default": 1.0}}, "required": ["text"]},
    description="文本转语音",
)
async def tts_generate(**kwargs: dict[str, Any]) -> dict[str, Any]:
    result = await _get_tool("tts_generate").execute(kwargs)
    return result.output if result.success else {"error": result.error}

if __name__ == "__main__":
    plugin.run()
