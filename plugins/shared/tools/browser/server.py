#!/usr/bin/env python3
"""Browser 工具 MCP 服务端——接口适配层。

六件浏览器工具经 MCP Bridge 网关操作宿主机 Playwright 浏览器。
工具实现见 tool.py（单 BuiltinTool 多 action），此处按工具名分发。
合宿平铺 sys.path 下裸名 ``tool`` 被成员争用——按文件路径显式加载（web_ext 同款）。
"""
from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("browser_tool")

_browser_tool_cls: Any = None


def _load_browser_tool() -> Any:
    """显式按文件路径加载本目录 tool.py 的 BrowserTool（合宿防遮蔽）。"""
    global _browser_tool_cls
    if _browser_tool_cls is not None:
        return _browser_tool_cls
    import importlib.util

    here = os.path.dirname(__file__)
    sys.path.insert(0, here)
    try:
        spec = importlib.util.spec_from_file_location(
            "browser_tool_impl", os.path.join(here, "tool.py")
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["browser_tool_impl"] = mod
        spec.loader.exec_module(mod)
    finally:
        # 合宿平铺 sys.path：临时置顶仅覆盖加载期，加载完立即还原。
        try:
            sys.path.remove(here)
        except ValueError:  # pragma: no cover —— here 不在 sys.path 的防御样板
            pass
    _browser_tool_cls = mod.BrowserTool
    return _browser_tool_cls


def _bridge_config() -> dict[str, Any]:
    """bridge 段配置（config_files 注入；缺省走 bridge_client 默认值）。"""
    try:
        cfg = plugin.get_config() or {}
    except Exception:
        cfg = {}
    bridge = cfg.get("bridge") if isinstance(cfg, dict) else None
    return bridge if isinstance(bridge, dict) else {}


# ── 工具声明（名/参沿用 @playwright/mcp 真实契约）──────────────

_NAVIGATE_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "The URL to navigate to"},
    },
    "required": ["url"],
    "additionalProperties": False,
}

_CLICK_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "Exact target element reference from the page snapshot, or a unique element selector"},
        "element": {"type": "string", "description": "Human-readable element description used to obtain permission to interact with the element"},
        "doubleClick": {"type": "boolean", "description": "Whether to perform a double click instead of a single click"},
        "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Button to click, defaults to left"},
        "modifiers": {"type": "array", "items": {"type": "string"}, "description": "Modifier keys to press"},
    },
    "required": ["target"],
    "additionalProperties": False,
}

_TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {"type": "string", "description": "Exact target element reference from the page snapshot, or a unique element selector"},
        "element": {"type": "string", "description": "Human-readable element description used to obtain permission to interact with the element"},
        "text": {"type": "string", "description": "Text to type into the element"},
        "slowly": {"type": "boolean", "description": "Whether to press keys one by one, useful for triggering key handlers"},
        "submit": {"type": "boolean", "description": "Whether to press Enter after typing"},
    },
    "required": ["target", "text"],
    "additionalProperties": False,
}

_SNAPSHOT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

_SCREENSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["png", "jpeg", "webp"], "description": "Image format, defaults to png"},
        "filename": {"type": "string", "description": "File name to save the screenshot to. Prefer relative names."},
        "fullPage": {"type": "boolean", "description": "Take a screenshot of the full scrollable page"},
        "scale": {"type": "string", "enum": ["css", "device"], "description": "Image resolution scale, defaults to css"},
    },
    "required": ["scale"],
    "additionalProperties": False,
}

_CONSOLE_SCHEMA = {
    "type": "object",
    "properties": {
        "level": {"type": "string", "enum": ["error", "warning", "info", "debug"], "description": "Level of the console messages to return"},
        "all": {"type": "boolean", "description": "Return all console messages since the beginning of the session"},
        "filename": {"type": "string", "description": "Filename to save the console messages to"},
    },
    "required": ["level"],
    "additionalProperties": False,
}

_MCP_CONTENT_OUTPUT = {
    "type": "object",
    "description": "浏览器工具结果（Bridge 归一：快照文本/截图 file_path）",
    "properties": {
        "status": {"type": "integer"},
        "tool": {"type": "string"},
        "url": {"type": "string"},
        "snapshot_text": {"type": "string", "description": "页面快照（accessibility tree 文本）"},
        "file_path": {"type": "string", "description": "截图落盘路径（任务 workspace 内）"},
        "image_mime": {"type": "string"},
    },
    "required": ["status"],
}


def _make_handler(tool_name: str):
    async def _handler(**kwargs: dict[str, Any]) -> dict[str, Any]:
        cls = _load_browser_tool()
        tool = cls(_bridge_config())
        result = await tool.execute(dict(kwargs, _tool_name=tool_name))
        return result.output if result.success else {"error": result.error, "status": 500}

    return _handler


def _register() -> None:
    cfg = {"description": "Navigate to a URL", "output": _MCP_CONTENT_OUTPUT, "render": {"card": "web"}}
    specs = [
        ("browser_navigate", _NAVIGATE_SCHEMA, "Navigate to a URL", {"card": "web"}),
        ("browser_snapshot", _SNAPSHOT_SCHEMA, "Get an accessibility snapshot of the current page", {"card": "generic"}),
        ("browser_click", _CLICK_SCHEMA, "Click an element on the page", {"card": "generic"}),
        ("browser_type", _TYPE_SCHEMA, "Type text into an editable element on the page", {"card": "generic"}),
        ("browser_take_screenshot", _SCREENSHOT_SCHEMA, "Take a screenshot of the current page", {"card": "image"}),
        ("browser_console_messages", _CONSOLE_SCHEMA, "Get console messages of the current page", {"card": "generic"}),
    ]
    for name, schema, desc, render in specs:
        plugin.tool(name=name, schema=schema, description=desc, output_schema=_MCP_CONTENT_OUTPUT, render=render)(
            _make_handler(name)
        )


_register()

if __name__ == "__main__":
    plugin.run()
