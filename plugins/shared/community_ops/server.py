#!/usr/bin/env python3
"""Community Ops 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

from _libs.tool_server_base import load_tool_class, tool_result_envelope  # noqa: E402

plugin = AgentOSPlugin("community_ops_tool")

_impl_cls: dict[str, Any] = {}


def _manifest_tools() -> dict[str, dict[str, Any]]:
    """读 plugin.json 的工具声明（input_schema/description 单源）。

    「插件即声明」：plugin.json 是唯一书写处，tools/list 上报必须与声明逐字
    一致——手抄瘦 schema 与声明面漂移即被内核 G2 净化全量剔除。
    读失败让异常传播（sidecar 起
    不来 → G2 观测失败 ≠ 判定失败，保留声明注册待复验），不做静默回退。
    """
    manifest = json.loads(
        (Path(__file__).resolve().parent / "plugin.json").read_text(encoding="utf-8")
    )
    return {t["name"]: t for t in manifest["capabilities"]["tools"]}


_MANIFEST_TOOLS = _manifest_tools()


def _load_tool_class(class_name: str) -> Any:
    """显式按文件路径加载本目录 tool.py 的工具类（合宿防遮蔽），按类名缓存。"""
    cached = _impl_cls.get(class_name)
    if cached is not None:
        return cached
    cls = load_tool_class(
        os.path.dirname(__file__), "community_ops_tool_impl", class_name
    )
    _impl_cls[class_name] = cls
    return cls


@plugin.tool(
    name="github_ops",
    schema=_MANIFEST_TOOLS["github_ops"]["input_schema"],
    description=_MANIFEST_TOOLS["github_ops"]["description"],
)
async def github_ops(**kwargs: dict[str, Any]) -> dict[str, Any]:
    cls = _load_tool_class("GitHubOpsTool")
    # 注入配置按 config_files[].id 命名空间嵌套（{"settings": {...}}）；
    # 未注入（get_config()={}）传 None，工具侧回落模块常量默认值
    settings = plugin.get_config().get("settings")
    result = await cls(settings).execute(kwargs)
    return tool_result_envelope(result)


@plugin.tool(
    name="feedback_ledger",
    schema=_MANIFEST_TOOLS["feedback_ledger"]["input_schema"],
    description=_MANIFEST_TOOLS["feedback_ledger"]["description"],
)
async def feedback_ledger(**kwargs: dict[str, Any]) -> dict[str, Any]:
    cls = _load_tool_class("FeedbackLedgerTool")
    result = await cls().execute(kwargs)
    return tool_result_envelope(result)


if __name__ == "__main__":
    plugin.run()
