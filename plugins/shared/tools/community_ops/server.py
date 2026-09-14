#!/usr/bin/env python3
"""Community Ops 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("community_ops_tool")

_impl_cls: dict[str, Any] = {}


def _manifest_tools() -> dict[str, dict[str, Any]]:
    """读 plugin.json 的工具声明（input_schema/description 单源）。

    「插件即声明」：plugin.json 是唯一书写处，tools/list 上报必须与声明逐字
    一致——双写漂移会被内核 G2 净化剔除（BUG-5：手抄瘦 schema 曾致
    github_ops/feedback_ledger 被全量剔除）。读失败让异常传播（sidecar 起
    不来 → G2 观测失败 ≠ 判定失败，保留声明注册待复验），不做静默回退。
    """
    manifest = json.loads(
        (Path(__file__).resolve().parent / "plugin.json").read_text(encoding="utf-8")
    )
    return {t["name"]: t for t in manifest["capabilities"]["tools"]}


_MANIFEST_TOOLS = _manifest_tools()


def _load_tool_class(class_name: str) -> Any:
    """显式按文件路径加载本目录 tool.py 的工具类。

    合宿平铺 sys.path 下裸名 ``tool`` 被多个成员的同名模块争用，handler 内
    裸名 import 运行期会命中他人实现；加载期临时把本目录置顶 sys.path，
    结果按类名模块级缓存。
    """
    cached = _impl_cls.get(class_name)
    if cached is not None:
        return cached
    import importlib.util

    here = os.path.dirname(__file__)
    sys.path.insert(0, here)
    try:
        spec = importlib.util.spec_from_file_location(
            "community_ops_tool_impl", os.path.join(here, "tool.py")
        )
        assert spec is not None
        assert spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["community_ops_tool_impl"] = mod
        spec.loader.exec_module(mod)
    finally:
        while here in sys.path:
            sys.path.remove(here)
    cls = getattr(mod, class_name)
    _impl_cls[class_name] = cls
    return cls


@plugin.tool(
    name="github_ops",
    schema=_MANIFEST_TOOLS["github_ops"]["input_schema"],
    description=_MANIFEST_TOOLS["github_ops"]["description"],
)
async def github_ops(**kwargs: dict[str, Any]) -> dict[str, Any]:
    cls = _load_tool_class("GitHubOpsTool")
    result = await cls().execute(kwargs)
    return result.output if result.success else _failure(result)


@plugin.tool(
    name="feedback_ledger",
    schema=_MANIFEST_TOOLS["feedback_ledger"]["input_schema"],
    description=_MANIFEST_TOOLS["feedback_ledger"]["description"],
)
async def feedback_ledger(**kwargs: dict[str, Any]) -> dict[str, Any]:
    cls = _load_tool_class("FeedbackLedgerTool")
    result = await cls().execute(kwargs)
    return result.output if result.success else _failure(result)


def _failure(result: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"success": False, "error": result.error}
    if result.error_code:
        payload["error_code"] = result.error_code
    return payload


if __name__ == "__main__":
    plugin.run()
