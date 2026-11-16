#!/usr/bin/env python3
"""Ecosystem Import 工具 MCP 服务端——接口适配层。

两个工具（声明面单源 = ``plugin.json`` 的 ``capabilities.tools``）：
- ``ecosystem_translate``：Codex 插件目录 → IR 摘要（只读，零副作用）
- ``ecosystem_apply``：翻译 + 治理闸门 + 技能落地用户根 + 溯源登记（显式调用才写）

合宿契约：server.py 暴露模块级 plugin 实例（host.py getattr(module, "plugin")），
注册在装载期完成。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

from _libs.tool_server_base import load_tool_class, tool_result_envelope  # noqa: E402

plugin = AgentOSPlugin("ecosystem_import_tool")

_impl_cls: dict[str, Any] = {}


def _manifest_tools() -> dict[str, dict[str, Any]]:
    """读 plugin.json 的工具声明（input_schema/description 单源）。

    「插件即声明」：plugin.json 是唯一书写处，tools/list 上报必须与声明逐字一致
    ——手抄 schema 与声明面漂移即被内核 G2 净化全量剔除。读失败让异常传播
    （sidecar 起不来 → G2 观测失败 ≠ 判定失败，保留声明注册待复验），不做静默回退。
    """
    manifest = json.loads((Path(__file__).resolve().parent / "plugin.json").read_text(encoding="utf-8"))
    return {tool["name"]: tool for tool in manifest["capabilities"]["tools"]}


_MANIFEST_TOOLS = _manifest_tools()


def _load_tool_class(class_name: str) -> Any:
    """显式按文件路径加载本目录 tool.py 的工具类（合宿防遮蔽），按类名缓存。"""
    cached = _impl_cls.get(class_name)
    if cached is not None:
        return cached
    cls = load_tool_class(os.path.dirname(__file__), "ecosystem_import_tool_impl", class_name)
    _impl_cls[class_name] = cls
    return cls


@plugin.tool(
    name="ecosystem_translate",
    schema=_MANIFEST_TOOLS["ecosystem_translate"]["input_schema"],
    description=_MANIFEST_TOOLS["ecosystem_translate"]["description"],
)
async def ecosystem_translate(**kwargs: Any) -> dict[str, Any]:
    result = await _load_tool_class("EcosystemTranslateTool")().execute(kwargs)
    return tool_result_envelope(result)


@plugin.tool(
    name="ecosystem_apply",
    schema=_MANIFEST_TOOLS["ecosystem_apply"]["input_schema"],
    description=_MANIFEST_TOOLS["ecosystem_apply"]["description"],
)
async def ecosystem_apply(**kwargs: Any) -> dict[str, Any]:
    result = await _load_tool_class("EcosystemApplyTool")().execute(kwargs)
    return tool_result_envelope(result)


if __name__ == "__main__":
    plugin.run()
