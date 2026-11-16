#!/usr/bin/env python3
"""Browser 工具 MCP 服务端——接口适配层。

六件浏览器工具经 MCP Bridge 网关操作宿主机 Playwright 浏览器。
工具实现见 tool.py（单 BuiltinTool 多 action），此处按工具名分发。
合宿平铺 sys.path 下裸名 ``tool`` 被成员争用——加载走 _libs 防遮蔽 helper
（tool_server_base，六件工具插件 server 共用）。

另有 http.handle REST 面：GET /ext/browser_tool/page/browser-live 出直播页 HTML，
GET /ext/browser_tool/live 换直播票（工作区「浏览器」页签经 Bridge 直连 MJPEG
投屏，ADR 2026-10-21）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin
from agentos_plugin_sdk.bridge_client import BridgeClient, BridgeClientError

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根（http_json/_libs）入 sys.path

from _libs.mode_server_base import html_file_response  # noqa: E402
from _libs.tool_server_base import load_tool_class, tool_result_envelope  # noqa: E402
from _root_api import (  # noqa: E402
    error as _error,  # noqa: E402
    json_response as _json_response,  # noqa: E402
    ok as _ok,  # noqa: E402
)

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("browser_tool")

_browser_tool_cls: Any = None

_LIVE_ROUTE = "/ext/browser_tool/live"
_LIVE_PAGE_ENDPOINT = "/ext/browser_tool/page/browser-live"
_LIVE_PAGE_HTML_PATH = str(Path(__file__).resolve().parent / "webview" / "browser_live.html")


def _load_browser_tool() -> Any:
    """显式按文件路径加载本目录 tool.py 的 BrowserTool（合宿防遮蔽）。"""
    global _browser_tool_cls
    if _browser_tool_cls is not None:
        return _browser_tool_cls
    _browser_tool_cls = load_tool_class(
        os.path.dirname(__file__), "browser_tool_impl", "BrowserTool"
    )
    return _browser_tool_cls


def _bridge_config() -> dict[str, Any]:
    """bridge 段配置（config_files 注入；缺省走 bridge_client 默认值）。

    内核注入按 manifest config_files[].id 命名空间嵌套
    （{"browser_tool": {"bridge": {...}}}，AGENTS.md 常用操作速查口径）；
    平铺形态兜底（旧内核/直跑）。dev 长期未炸纯因 BridgeClient 默认值恰等于
    配置值——嵌套读修复后装机侧 token 供给（用户空间副本）才真正生效。
    """
    try:
        cfg = plugin.get_config() or {}
    except Exception as exc:
        # 配置读取失败降级默认值不阻断主流程，但降级必须可见——静默走默认
        # 会把配置损坏伪装成"未配置"，用户改了配置不生效却无任何线索。
        logger.warning("[browser] bridge 配置读取失败，降级默认配置: %s", exc)
        cfg = {}
    if not isinstance(cfg, dict):
        return {}
    nested = cfg.get("browser_tool")
    if isinstance(nested, dict) and isinstance(nested.get("bridge"), dict):
        return nested["bridge"]
    bridge = cfg.get("bridge")
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
        # 失败信封显式 success=False（ADR 2026-09-28 收编契约）——裸 error
        # 载荷会被归一层包成 success 伪信封。
        return tool_result_envelope(result, status=500)

    return _handler


def _register() -> None:
    # open_page 与 plugin.json render 声明同源（工具卡「查看浏览器」→ 直播页签）。
    open_page = {"page": "browser_live", "label": "查看浏览器"}
    specs = [
        ("browser_navigate", _NAVIGATE_SCHEMA, "Navigate to a URL", {"card": "web", "open_page": open_page}),
        ("browser_snapshot", _SNAPSHOT_SCHEMA, "Get an accessibility snapshot of the current page", {"card": "generic", "open_page": open_page}),
        ("browser_click", _CLICK_SCHEMA, "Click an element on the page", {"card": "generic", "open_page": open_page}),
        ("browser_type", _TYPE_SCHEMA, "Type text into an editable element on the page", {"card": "generic", "open_page": open_page}),
        ("browser_take_screenshot", _SCREENSHOT_SCHEMA, "Take a screenshot of the current page", {"card": "image", "open_page": open_page}),
        ("browser_console_messages", _CONSOLE_SCHEMA, "Get console messages of the current page", {"card": "generic", "open_page": open_page}),
    ]
    for name, schema, desc, render in specs:
        plugin.tool(name=name, schema=schema, description=desc, output_schema=_MCP_CONTENT_OUTPUT, render=render)(
            _make_handler(name)
        )


_register()


# ── REST http.handle（/ext/browser_tool/** 入口）──────────────


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/browser_tool/** (浏览器直播换票面)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发 /ext/browser_tool/**（直播页 HTML + live 换票）。"""
    del plugin_id, headers, query  # 无 per-caller 差异（auth:user 由 dispatcher 把关）
    if path == _LIVE_PAGE_ENDPOINT and method == "GET":
        try:
            return _ok(html_file_response(_LIVE_PAGE_HTML_PATH))
        except OSError as exc:
            logger.exception("browser live page html read failed: %s", exc)
            return _error(f"browser_live html unavailable: {exc}", 500)
    if path == _LIVE_ROUTE and method == "GET":
        try:
            client = BridgeClient(_bridge_config())
            result = client.live_ticket()
        except BridgeClientError as e:
            # 构造失败（token 缺失等）→ 等待态语义（页面轮询重试，非终态错误）
            return _ok(_json_response(_live_unavailable("runtime_down", str(e)), 200))
        if not result.get("ok"):
            body = result.get("body") or {}
            status = int(result.get("status") or 0)
            message = str(body.get("error") or f"换票失败(status={status})")
            if status in (401, 403):
                # Bridge 治理面拒绝属配置错误，原样透传非 200（页面显示重试）。
                return _ok(_json_response({"error": message}, status))
            # 不可达/运行时未启用/无页面 → 等待态语义（200 + available=false，
            # 页面按 reason 分轮询节奏；HTTP 层不伪装成功——这是换票查询的
            # 正常"当前不可用"结果，非错误吞没）。
            return _ok(_json_response(_live_unavailable(_live_reason(status, message), message), 200))
        return _ok(_json_response(result.get("body") or {}, 200))
    return _error(f"browser_tool 未知端点: {path}", 404)


def _live_unavailable(reason: str, message: str) -> dict[str, Any]:
    """等待态载荷：available=false + reason（页面按 reason 分轮询节奏）。"""
    return {"available": False, "reason": reason, "error": message}


def _live_reason(status: int, message: str) -> str:
    """Bridge 侧不可用归因：404 = 运行时未启用（disabled，慢轮询）；其余 = 未就绪。"""
    if status == 404 or "未启用" in message:
        return "disabled"
    return "runtime_down"


if __name__ == "__main__":
    plugin.run()
